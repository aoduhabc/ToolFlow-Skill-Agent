package tools

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	md "github.com/JohannesKaufmann/html-to-markdown"
	"github.com/PuerkitoBio/goquery"
)

const FetchToolName = "fetch"

type FetchParams struct {
	URL     string `json:"url"`
	Format  string `json:"format"`
	Timeout int    `json:"timeout,omitempty"`
}

type fetchTool struct {
	client *http.Client
}

func NewFetchTool() BaseTool {
	return &fetchTool{
		client: &http.Client{
			Timeout: 30 * time.Second,
		},
	}
}

func (t *fetchTool) Info() ToolInfo {
	return ToolInfo{
		Name:        FetchToolName,
		Description: "Fetch URL content and return as text, markdown, or html.",
		Parameters: map[string]any{
			"url": map[string]any{
				"type":        "string",
				"description": "URL to fetch",
			},
			"format": map[string]any{
				"type":        "string",
				"description": "text, markdown, or html",
				"enum":        []string{"text", "markdown", "html"},
			},
			"timeout": map[string]any{
				"type":        "number",
				"description": "Timeout seconds, max 120",
			},
		},
		Required: []string{"url", "format"},
	}
}

func (t *fetchTool) Run(ctx context.Context, call ToolCall) (ToolResponse, error) {
	var params FetchParams
	if err := json.Unmarshal([]byte(call.Input), &params); err != nil {
		return NewTextErrorResponse("Failed to parse fetch parameters: " + err.Error()), nil
	}
	if params.URL == "" {
		return NewTextErrorResponse("url parameter is required"), nil
	}
	format := strings.ToLower(params.Format)
	if format != "text" && format != "markdown" && format != "html" {
		return NewTextErrorResponse("format must be one of: text, markdown, html"), nil
	}
	if !strings.HasPrefix(params.URL, "http://") && !strings.HasPrefix(params.URL, "https://") {
		return NewTextErrorResponse("url must start with http:// or https://"), nil
	}

	client := t.client
	if params.Timeout > 0 {
		timeout := params.Timeout
		if timeout > 120 {
			timeout = 120
		}
		client = &http.Client{
			Timeout: time.Duration(timeout) * time.Second,
		}
	}

	req, err := http.NewRequestWithContext(ctx, "GET", params.URL, nil)
	if err != nil {
		return ToolResponse{}, fmt.Errorf("failed to create request: %w", err)
	}
	req.Header.Set("User-Agent", "auto-mvp/1.0")
	resp, err := client.Do(req)
	if err != nil {
		return ToolResponse{}, fmt.Errorf("failed to fetch url: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return NewTextErrorResponse(fmt.Sprintf("request failed with status code: %d", resp.StatusCode)), nil
	}

	body, err := io.ReadAll(io.LimitReader(resp.Body, 5*1024*1024))
	if err != nil {
		return NewTextErrorResponse("failed to read response body: " + err.Error()), nil
	}
	content := string(body)
	contentType := resp.Header.Get("Content-Type")

	switch format {
	case "text":
		if strings.Contains(contentType, "text/html") {
			text, err := extractTextFromHTML(content)
			if err != nil {
				return NewTextErrorResponse("failed to extract text from html: " + err.Error()), nil
			}
			return NewTextResponse(text), nil
		}
		return NewTextResponse(content), nil
	case "markdown":
		if strings.Contains(contentType, "text/html") {
			markdown, err := convertHTMLToMarkdown(content)
			if err != nil {
				return NewTextErrorResponse("failed to convert html to markdown: " + err.Error()), nil
			}
			return NewTextResponse(markdown), nil
		}
		return NewTextResponse("```\n" + content + "\n```"), nil
	case "html":
		return NewTextResponse(content), nil
	default:
		return NewTextResponse(content), nil
	}
}

func extractTextFromHTML(html string) (string, error) {
	doc, err := goquery.NewDocumentFromReader(strings.NewReader(html))
	if err != nil {
		return "", err
	}
	text := doc.Text()
	return strings.Join(strings.Fields(text), " "), nil
}

func convertHTMLToMarkdown(html string) (string, error) {
	converter := md.NewConverter("", true, nil)
	return converter.ConvertString(html)
}
