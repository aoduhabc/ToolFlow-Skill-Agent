package tools

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"time"

	"github.com/example/demo-tools-bridge/pkg/lsp"
)

const PatchToolName = "patch"

type PatchParams struct {
	PatchText string `json:"patch_text"`
}

type PatchResponseMetadata struct {
	FilesChanged []string `json:"files_changed"`
	Additions    int      `json:"additions"`
	Removals     int      `json:"removals"`
}

type patchTool struct {
	root string
	lsps map[string]*lsp.Client
}

func NewPatchTool(root string) BaseTool {
	return &patchTool{root: root, lsps: map[string]*lsp.Client{}}
}

func (p *patchTool) Info() ToolInfo {
	return ToolInfo{
		Name:        PatchToolName,
		Description: "Apply a multi-file patch in *** Begin Patch / *** End Patch format.",
		Parameters: map[string]any{
			"patch_text": map[string]any{
				"type":        "string",
				"description": "Patch text",
			},
		},
		Required: []string{"patch_text"},
	}
}

func (p *patchTool) Run(ctx context.Context, call ToolCall) (ToolResponse, error) {
	var params PatchParams
	if err := json.Unmarshal([]byte(call.Input), &params); err != nil {
		return NewTextErrorResponse(fmt.Sprintf("error parsing parameters: %s", err)), nil
	}
	if params.PatchText == "" {
		return NewTextErrorResponse("patch_text is required"), nil
	}

	filesToRead := IdentifyFilesNeeded(params.PatchText)
	for _, filePath := range filesToRead {
		absPath, err := absClean(filePath)
		if err != nil {
			return NewTextErrorResponse(err.Error()), nil
		}
		if p.root != "" && !isWithinRoot(p.root, absPath) {
			return NewTextErrorResponse(fmt.Sprintf("path is outside workspace root: %s", absPath)), nil
		}
		if getLastReadTime(absPath).IsZero() {
			return NewTextErrorResponse(fmt.Sprintf("you must read file before patching: %s", absPath)), nil
		}
		info, err := os.Stat(absPath)
		if err != nil {
			if os.IsNotExist(err) {
				return NewTextErrorResponse(fmt.Sprintf("file not found: %s", absPath)), nil
			}
			return ToolResponse{}, fmt.Errorf("failed to access file %s: %w", absPath, err)
		}
		lastRead := getLastReadTime(absPath)
		if info.ModTime().After(lastRead.Add(1 * time.Millisecond)) {
			return NewTextErrorResponse(fmt.Sprintf("file has changed since last read: %s", absPath)), nil
		}
	}

	filesToAdd := IdentifyFilesAdded(params.PatchText)
	for _, filePath := range filesToAdd {
		absPath, err := absClean(filePath)
		if err != nil {
			return NewTextErrorResponse(err.Error()), nil
		}
		if p.root != "" && !isWithinRoot(p.root, absPath) {
			return NewTextErrorResponse(fmt.Sprintf("path is outside workspace root: %s", absPath)), nil
		}
		_, err = os.Stat(absPath)
		if err == nil {
			return NewTextErrorResponse(fmt.Sprintf("file already exists and cannot be added: %s", absPath)), nil
		}
		if !os.IsNotExist(err) {
			return ToolResponse{}, fmt.Errorf("failed to check file %s: %w", absPath, err)
		}
	}

	currentFiles := map[string]string{}
	absByPatchPath := map[string]string{}
	for _, filePath := range filesToRead {
		absPath, err := absClean(filePath)
		if err != nil {
			return NewTextErrorResponse(err.Error()), nil
		}
		content, err := os.ReadFile(absPath)
		if err != nil {
			return ToolResponse{}, fmt.Errorf("failed to read file %s: %w", absPath, err)
		}
		currentFiles[filePath] = string(content)
		absByPatchPath[filePath] = absPath
	}
	for _, filePath := range filesToAdd {
		absPath, err := absClean(filePath)
		if err != nil {
			return NewTextErrorResponse(err.Error()), nil
		}
		absByPatchPath[filePath] = absPath
	}

	parsedPatch, fuzz, err := TextToPatch(params.PatchText, currentFiles)
	if err != nil {
		return NewTextErrorResponse(fmt.Sprintf("failed to parse patch: %s", err)), nil
	}
	if fuzz > 3 {
		return NewTextErrorResponse(fmt.Sprintf("patch contains fuzzy matches (fuzz level: %d)", fuzz)), nil
	}

	commit, err := PatchToCommit(parsedPatch, currentFiles)
	if err != nil {
		return NewTextErrorResponse(fmt.Sprintf("failed to create commit: %s", err)), nil
	}

	err = ApplyCommit(commit, func(path string, content string) error {
		absPath := absByPatchPath[path]
		if absPath == "" {
			resolved, err := absClean(path)
			if err != nil {
				return err
			}
			absPath = resolved
		}
		if err := os.MkdirAll(filepath.Dir(absPath), 0o755); err != nil {
			return err
		}
		return os.WriteFile(absPath, []byte(content), 0o644)
	}, func(path string) error {
		absPath := absByPatchPath[path]
		if absPath == "" {
			resolved, err := absClean(path)
			if err != nil {
				return err
			}
			absPath = resolved
		}
		return os.Remove(absPath)
	})
	if err != nil {
		return NewTextErrorResponse(fmt.Sprintf("failed to apply patch: %s", err)), nil
	}

	changedFiles := make([]string, 0, len(commit.Changes))
	totalAdditions := 0
	totalRemovals := 0
	for path, change := range commit.Changes {
		absPath := absByPatchPath[path]
		if absPath == "" {
			resolved, err := absClean(path)
			if err != nil {
				return NewTextErrorResponse(err.Error()), nil
			}
			absPath = resolved
		}
		changedFiles = append(changedFiles, absPath)
		recordFileWrite(absPath)
		recordFileRead(absPath)
		if change.NewContent != nil {
			totalAdditions += countLines(*change.NewContent)
		}
		if change.OldContent != nil {
			totalRemovals += countLines(*change.OldContent)
		}
		for _, client := range p.lsps {
			if client.IsFileOpen(absPath) {
				_ = client.NotifyChange(ctx, absPath)
			} else {
				_ = client.OpenFile(ctx, absPath)
				_ = client.NotifyChange(ctx, absPath)
			}
		}
	}

	result := fmt.Sprintf("Patch applied successfully. %d files changed", len(changedFiles))
	return WithResponseMetadata(
		NewTextResponse(result),
		PatchResponseMetadata{
			FilesChanged: changedFiles,
			Additions:    totalAdditions,
			Removals:     totalRemovals,
		},
	), nil
}
