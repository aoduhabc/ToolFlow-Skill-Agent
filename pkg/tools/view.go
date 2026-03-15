package tools

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"

	"github.com/example/demo-tools-bridge/pkg/lsp"
	"github.com/example/demo-tools-bridge/pkg/lsp/protocol"
)

const (
	ViewToolName     = "view"
	MaxReadSize      = 250 * 1024
	DefaultReadLimit = 2000
	MaxLineLength    = 2000
	viewDescription  = `File viewing tool that reads and displays the contents of files with line numbers, allowing you to examine code, logs, or text data.

WHEN TO USE THIS TOOL:
- Use when you need to read the contents of a specific file
- Helpful for examining source code, configuration files, or log files
- Perfect for looking at text-based file formats

HOW TO USE:
- Provide the path to the file you want to view
- Optionally specify an offset to start reading from a specific line
- Optionally specify a limit to control how many lines are read

FEATURES:
- Displays file contents with line numbers for easy reference
- Can read from any position in a file using the offset parameter
- Handles large files by limiting the number of lines read
- Automatically truncates very long lines for better display
- Suggests similar file names when the requested file isn't found

LIMITATIONS:
- Maximum file size is 250KB
- Default reading limit is 2000 lines
- Lines longer than 2000 characters are truncated
- Cannot display binary files or images
- Images can be identified but not displayed

TIPS:
- Use with Glob tool to first find files you want to view
- For code exploration, first use Grep to find relevant files, then View to examine them
- When viewing large files, use the offset parameter to read specific sections`
)

type ViewParams struct {
	FilePath string `json:"file_path"`
	Offset   int    `json:"offset"`
	Limit    int    `json:"limit"`
}

type ViewResponseMetadata struct {
	FilePath string `json:"file_path"`
	Content  string `json:"content"`
}

type viewTool struct {
	root string
	lsps map[string]*lsp.Client
}

func NewViewTool(root string) BaseTool {
	return &viewTool{root: root, lsps: map[string]*lsp.Client{}}
}

func (v *viewTool) Info() ToolInfo {
	return ToolInfo{
		Name:        ViewToolName,
		Description: viewDescription,
		Parameters: map[string]any{
			"file_path": map[string]any{
				"type":        "string",
				"description": "The path to the file to read",
			},
			"offset": map[string]any{
				"type":        "integer",
				"description": "The line number to start reading from (0-based)",
			},
			"limit": map[string]any{
				"type":        "integer",
				"description": "The number of lines to read (defaults to 2000)",
			},
		},
		Required: []string{"file_path"},
	}
}

func (v *viewTool) Run(ctx context.Context, call ToolCall) (ToolResponse, error) {
	var params ViewParams
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
	if v.root != "" && !isWithinRoot(v.root, fileAbs) {
		return NewTextErrorResponse("path is outside workspace root"), nil
	}

	fileInfo, err := os.Stat(fileAbs)
	if err != nil {
		if os.IsNotExist(err) {
			dir := filepath.Dir(fileAbs)
			base := filepath.Base(fileAbs)

			dirEntries, dirErr := os.ReadDir(dir)
			if dirErr == nil {
				var suggestions []string
				baseLower := strings.ToLower(base)
				for _, entry := range dirEntries {
					nameLower := strings.ToLower(entry.Name())
					if strings.Contains(nameLower, baseLower) || strings.Contains(baseLower, nameLower) {
						suggestions = append(suggestions, filepath.Join(dir, entry.Name()))
						if len(suggestions) >= 3 {
							break
						}
					}
				}
				if len(suggestions) > 0 {
					return NewTextErrorResponse(fmt.Sprintf("File not found: %s\n\nDid you mean one of these?\n%s",
						fileAbs, strings.Join(suggestions, "\n"))), nil
				}
			}

			return NewTextErrorResponse(fmt.Sprintf("File not found: %s", fileAbs)), nil
		}
		return ToolResponse{}, fmt.Errorf("error accessing file: %w", err)
	}
	if fileInfo.IsDir() {
		return NewTextErrorResponse(fmt.Sprintf("Path is a directory, not a file: %s", fileAbs)), nil
	}
	if fileInfo.Size() > MaxReadSize {
		return NewTextErrorResponse(fmt.Sprintf("File is too large (%d bytes). Maximum size is %d bytes", fileInfo.Size(), MaxReadSize)), nil
	}
	if isImage, imageType := isImageFile(fileAbs); isImage {
		return NewTextErrorResponse(fmt.Sprintf("This is an image file of type: %s", imageType)), nil
	}
	if params.Limit <= 0 {
		params.Limit = DefaultReadLimit
	}

	content, lineCount, err := readTextFile(fileAbs, params.Offset, params.Limit)
	if err != nil {
		return ToolResponse{}, fmt.Errorf("error reading file: %w", err)
	}
	recordFileRead(fileAbs)

	out := "<file>\n"
	out += addLineNumbers(content, params.Offset+1)
	if lineCount > params.Offset+len(strings.Split(content, "\n")) {
		out += fmt.Sprintf("\n\n(File has more lines. Use 'offset' parameter to read beyond line %d)", params.Offset+len(strings.Split(content, "\n")))
	}
	out += "\n</file>\n"

	diag := v.collectDiagnostics(ctx, fileAbs)
	if diag != "" {
		out += "\n<file_diagnostics>\n" + diag + "\n</file_diagnostics>\n"
	}

	return WithResponseMetadata(
		NewTextResponse(out),
		ViewResponseMetadata{FilePath: fileAbs, Content: content},
	), nil
}

func addLineNumbers(content string, startLine int) string {
	if content == "" {
		return ""
	}
	lines := strings.Split(content, "\n")
	var result []string
	for i, line := range lines {
		line = strings.TrimSuffix(line, "\r")
		lineNum := i + startLine
		numStr := fmt.Sprintf("%d", lineNum)
		if len(numStr) >= 6 {
			result = append(result, fmt.Sprintf("%s|%s", numStr, line))
		} else {
			paddedNum := fmt.Sprintf("%6s", numStr)
			result = append(result, fmt.Sprintf("%s|%s", paddedNum, line))
		}
	}
	return strings.Join(result, "\n")
}

func readTextFile(filePath string, offset, limit int) (string, int, error) {
	file, err := os.Open(filePath)
	if err != nil {
		return "", 0, err
	}
	defer file.Close()

	lineCount := 0
	scanner := bufio.NewScanner(file)
	scanner.Buffer(make([]byte, 64*1024), 1024*1024)

	if offset > 0 {
		for lineCount < offset && scanner.Scan() {
			lineCount++
		}
		if err = scanner.Err(); err != nil {
			return "", 0, err
		}
	}

	if offset == 0 {
		_, err = file.Seek(0, io.SeekStart)
		if err != nil {
			return "", 0, err
		}
		scanner = bufio.NewScanner(file)
		scanner.Buffer(make([]byte, 64*1024), 1024*1024)
	}

	var lines []string
	lineCount = offset
	for scanner.Scan() && len(lines) < limit {
		lineCount++
		lineText := scanner.Text()
		if len(lineText) > MaxLineLength {
			lineText = lineText[:MaxLineLength] + "..."
		}
		lines = append(lines, lineText)
	}

	for scanner.Scan() {
		lineCount++
	}
	if err := scanner.Err(); err != nil {
		return "", 0, err
	}

	return strings.Join(lines, "\n"), lineCount, nil
}

func isImageFile(filePath string) (bool, string) {
	ext := strings.ToLower(filepath.Ext(filePath))
	switch ext {
	case ".jpg", ".jpeg":
		return true, "JPEG"
	case ".png":
		return true, "PNG"
	case ".gif":
		return true, "GIF"
	case ".bmp":
		return true, "BMP"
	case ".svg":
		return true, "SVG"
	case ".webp":
		return true, "WebP"
	default:
		return false, ""
	}
}

func (v *viewTool) collectDiagnostics(ctx context.Context, filePath string) string {
	if len(v.lsps) == 0 {
		return ""
	}
	var lines []string
	for name, client := range v.lsps {
		_ = client.OpenFile(ctx, filePath)
		ds, err := client.GetDiagnosticsForFile(ctx, filePath)
		if err != nil {
			continue
		}
		for _, d := range ds {
			severity := "Info"
			switch d.Severity {
			case protocol.SeverityError:
				severity = "Error"
			case protocol.SeverityWarning:
				severity = "Warn"
			case protocol.SeverityHint:
				severity = "Hint"
			case protocol.SeverityInformation:
				severity = "Info"
			}
			source := d.Source
			if source == "" {
				source = name
			}
			loc := fmt.Sprintf("%s:%d:%d", filePath, d.Range.Start.Line+1, d.Range.Start.Character+1)
			code := ""
			if d.Code != nil {
				code = fmt.Sprintf("[%v]", d.Code)
			}
			lines = append(lines, fmt.Sprintf("%s: %s [%s]%s %s", severity, loc, source, code, d.Message))
		}
	}
	return strings.Join(lines, "\n")
}
