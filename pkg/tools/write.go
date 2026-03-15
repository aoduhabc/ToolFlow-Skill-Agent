package tools

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"

	"github.com/example/demo-tools-bridge/pkg/lsp"
)

const WriteToolName = "write"

type WriteParams struct {
	FilePath string `json:"file_path"`
	Content  string `json:"content"`
}

type WriteResponseMetadata struct {
	FilePath string `json:"file_path"`
	Bytes    int    `json:"bytes"`
}

type writeTool struct {
	root string
	lsps map[string]*lsp.Client
}

func NewWriteTool(root string) BaseTool {
	return &writeTool{root: root, lsps: map[string]*lsp.Client{}}
}

func (w *writeTool) Info() ToolInfo {
	return ToolInfo{
		Name:        WriteToolName,
		Description: "Create or overwrite a file with the provided content.",
		Parameters: map[string]any{
			"file_path": map[string]any{
				"type":        "string",
				"description": "The path to the file to write",
			},
			"content": map[string]any{
				"type":        "string",
				"description": "The content to write to the file",
			},
		},
		Required: []string{"file_path", "content"},
	}
}

func (w *writeTool) Run(ctx context.Context, call ToolCall) (ToolResponse, error) {
	var params WriteParams
	if err := json.Unmarshal([]byte(call.Input), &params); err != nil {
		return NewTextErrorResponse(fmt.Sprintf("error parsing parameters: %s", err)), nil
	}
	if params.FilePath == "" {
		return NewTextErrorResponse("file_path is required"), nil
	}

	filePath := params.FilePath
	if !filepath.IsAbs(filePath) {
		filePath = filepath.Join(WorkingDir(), filePath)
	}
	absPath, err := absClean(filePath)
	if err != nil {
		return NewTextErrorResponse(err.Error()), nil
	}
	if w.root != "" && !isWithinRoot(w.root, absPath) {
		return NewTextErrorResponse("path is outside workspace root"), nil
	}

	info, err := os.Stat(absPath)
	if err == nil && info.IsDir() {
		return NewTextErrorResponse(fmt.Sprintf("path is a directory, not a file: %s", absPath)), nil
	} else if err != nil && !os.IsNotExist(err) {
		return ToolResponse{}, fmt.Errorf("error checking file: %w", err)
	}

	dir := filepath.Dir(absPath)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return ToolResponse{}, fmt.Errorf("error creating directory: %w", err)
	}

	if err := os.WriteFile(absPath, []byte(params.Content), 0o644); err != nil {
		return ToolResponse{}, fmt.Errorf("error writing file: %w", err)
	}
	recordFileWrite(absPath)
	recordFileRead(absPath)

	for _, client := range w.lsps {
		if client.IsFileOpen(absPath) {
			_ = client.NotifyChange(ctx, absPath)
		} else {
			_ = client.OpenFile(ctx, absPath)
			_ = client.NotifyChange(ctx, absPath)
		}
	}

	result := fmt.Sprintf("File successfully written: %s", absPath)
	return WithResponseMetadata(
		NewTextResponse(result),
		WriteResponseMetadata{
			FilePath: absPath,
			Bytes:    len(params.Content),
		},
	), nil
}
