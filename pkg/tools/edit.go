package tools

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/example/demo-tools-bridge/pkg/lsp"
)

const EditToolName = "edit"

type EditParams struct {
	FilePath  string `json:"file_path"`
	OldString string `json:"old_string"`
	NewString string `json:"new_string"`
}

type EditResponseMetadata struct {
	FilePath  string `json:"file_path"`
	Additions int    `json:"additions"`
	Removals  int    `json:"removals"`
}

type editTool struct {
	root string
	lsps map[string]*lsp.Client
}

func NewEditTool(root string) BaseTool {
	return &editTool{root: root, lsps: map[string]*lsp.Client{}}
}

func (e *editTool) Info() ToolInfo {
	return ToolInfo{
		Name:        EditToolName,
		Description: "Edit file content by replacing one unique occurrence, creating a new file, or deleting matched content.",
		Parameters: map[string]any{
			"file_path": map[string]any{
				"type":        "string",
				"description": "Path to file to edit",
			},
			"old_string": map[string]any{
				"type":        "string",
				"description": "Text to replace; empty means create file",
			},
			"new_string": map[string]any{
				"type":        "string",
				"description": "Replacement text; empty means delete old_string",
			},
		},
		Required: []string{"file_path", "old_string", "new_string"},
	}
}

func (e *editTool) Run(ctx context.Context, call ToolCall) (ToolResponse, error) {
	var params EditParams
	if err := json.Unmarshal([]byte(call.Input), &params); err != nil {
		return NewTextErrorResponse(fmt.Sprintf("error parsing parameters: %s", err)), nil
	}
	if params.FilePath == "" {
		return NewTextErrorResponse("file_path is required"), nil
	}

	fileAbs, err := absClean(params.FilePath)
	if err != nil {
		return NewTextErrorResponse(err.Error()), nil
	}
	if e.root != "" && !isWithinRoot(e.root, fileAbs) {
		return NewTextErrorResponse("path is outside workspace root"), nil
	}

	if params.OldString == "" {
		return e.createNewFile(ctx, fileAbs, params.NewString)
	}
	if params.NewString == "" {
		return e.deleteContent(ctx, fileAbs, params.OldString)
	}
	return e.replaceContent(ctx, fileAbs, params.OldString, params.NewString)
}

func (e *editTool) createNewFile(ctx context.Context, filePath string, content string) (ToolResponse, error) {
	info, err := os.Stat(filePath)
	if err == nil {
		if info.IsDir() {
			return NewTextErrorResponse(fmt.Sprintf("path is a directory, not a file: %s", filePath)), nil
		}
		return NewTextErrorResponse(fmt.Sprintf("file already exists: %s", filePath)), nil
	}
	if !os.IsNotExist(err) {
		return ToolResponse{}, fmt.Errorf("failed to access file: %w", err)
	}
	if err := os.MkdirAll(filepath.Dir(filePath), 0o755); err != nil {
		return ToolResponse{}, fmt.Errorf("failed to create parent directories: %w", err)
	}
	if err := os.WriteFile(filePath, []byte(content), 0o644); err != nil {
		return ToolResponse{}, fmt.Errorf("failed to write file: %w", err)
	}
	recordFileWrite(filePath)
	recordFileRead(filePath)
	e.notifyLSP(ctx, filePath)
	return WithResponseMetadata(
		NewTextResponse("File created: "+filePath+"\n"+diagnosticsForFile(ctx, filePath, e.lsps)),
		EditResponseMetadata{FilePath: filePath, Additions: countLines(content), Removals: 0},
	), nil
}

func (e *editTool) deleteContent(ctx context.Context, filePath string, oldString string) (ToolResponse, error) {
	info, err := os.Stat(filePath)
	if err != nil {
		if os.IsNotExist(err) {
			return NewTextErrorResponse(fmt.Sprintf("file not found: %s", filePath)), nil
		}
		return ToolResponse{}, fmt.Errorf("failed to access file: %w", err)
	}
	if info.IsDir() {
		return NewTextErrorResponse(fmt.Sprintf("path is a directory, not a file: %s", filePath)), nil
	}
	lastRead := getLastReadTime(filePath)
	if lastRead.IsZero() {
		return NewTextErrorResponse("you must read the file before editing it. Use the view tool first"), nil
	}
	if info.ModTime().After(lastRead) {
		return NewTextErrorResponse(fmt.Sprintf("file %s has been modified since last read", filePath)), nil
	}
	contentBytes, err := os.ReadFile(filePath)
	if err != nil {
		return ToolResponse{}, fmt.Errorf("failed to read file: %w", err)
	}
	content := string(contentBytes)
	index := strings.Index(content, oldString)
	if index < 0 {
		return NewTextErrorResponse("old_string not found in file. Make sure it matches exactly"), nil
	}
	if strings.LastIndex(content, oldString) != index {
		return NewTextErrorResponse("old_string appears multiple times in file. provide more context"), nil
	}
	newContent := content[:index] + content[index+len(oldString):]
	if err := os.WriteFile(filePath, []byte(newContent), 0o644); err != nil {
		return ToolResponse{}, fmt.Errorf("failed to write file: %w", err)
	}
	recordFileWrite(filePath)
	recordFileRead(filePath)
	e.notifyLSP(ctx, filePath)
	return WithResponseMetadata(
		NewTextResponse("Content deleted from file: "+filePath+"\n"+diagnosticsForFile(ctx, filePath, e.lsps)),
		EditResponseMetadata{FilePath: filePath, Additions: 0, Removals: countLines(oldString)},
	), nil
}

func (e *editTool) replaceContent(ctx context.Context, filePath string, oldString string, newString string) (ToolResponse, error) {
	info, err := os.Stat(filePath)
	if err != nil {
		if os.IsNotExist(err) {
			return NewTextErrorResponse(fmt.Sprintf("file not found: %s", filePath)), nil
		}
		return ToolResponse{}, fmt.Errorf("failed to access file: %w", err)
	}
	if info.IsDir() {
		return NewTextErrorResponse(fmt.Sprintf("path is a directory, not a file: %s", filePath)), nil
	}
	lastRead := getLastReadTime(filePath)
	if lastRead.IsZero() {
		return NewTextErrorResponse("you must read the file before editing it. Use the view tool first"), nil
	}
	if info.ModTime().After(lastRead) {
		return NewTextErrorResponse(fmt.Sprintf("file %s has been modified since last read", filePath)), nil
	}
	contentBytes, err := os.ReadFile(filePath)
	if err != nil {
		return ToolResponse{}, fmt.Errorf("failed to read file: %w", err)
	}
	content := string(contentBytes)
	index := strings.Index(content, oldString)
	if index < 0 {
		return NewTextErrorResponse("old_string not found in file. Make sure it matches exactly"), nil
	}
	if strings.LastIndex(content, oldString) != index {
		return NewTextErrorResponse("old_string appears multiple times in file. provide more context"), nil
	}
	newContent := content[:index] + newString + content[index+len(oldString):]
	if newContent == content {
		return NewTextErrorResponse("new content is the same as old content"), nil
	}
	if err := os.WriteFile(filePath, []byte(newContent), 0o644); err != nil {
		return ToolResponse{}, fmt.Errorf("failed to write file: %w", err)
	}
	recordFileWrite(filePath)
	recordFileRead(filePath)
	e.notifyLSP(ctx, filePath)
	return WithResponseMetadata(
		NewTextResponse("Content replaced in file: "+filePath+"\n"+diagnosticsForFile(ctx, filePath, e.lsps)),
		EditResponseMetadata{
			FilePath:  filePath,
			Additions: countLines(newString),
			Removals:  countLines(oldString),
		},
	), nil
}

func (e *editTool) notifyLSP(ctx context.Context, filePath string) {
	for _, client := range e.lsps {
		if client.IsFileOpen(filePath) {
			_ = client.NotifyChange(ctx, filePath)
		} else {
			_ = client.OpenFile(ctx, filePath)
			_ = client.NotifyChange(ctx, filePath)
		}
	}
}

func countLines(text string) int {
	if text == "" {
		return 0
	}
	return len(strings.Split(text, "\n"))
}
