package tools

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"

	"github.com/example/demo-tools-bridge/pkg/skills"
)

const (
	SkillSearchToolName = "skill_search"
)

type SkillSearchParams struct {
	Query string `json:"query"`
	Limit int    `json:"limit"`
}

type SkillSearchMetadata struct {
	Query      string             `json:"query"`
	Candidates []skills.Candidate `json:"candidates"`
}

type skillSearchTool struct {
	root    string
	manager *skills.Manager
}

func NewSkillSearchTool(root string) BaseTool {
	return &skillSearchTool{root: root}
}

func (s *skillSearchTool) SetManager(manager *skills.Manager) {
	s.manager = manager
}

func (s *skillSearchTool) Info() ToolInfo {
	return ToolInfo{
		Name:        SkillSearchToolName,
		Description: "Search available skills by query and return ranked candidates using metadata-only matching.",
		Parameters: map[string]any{
			"query": map[string]any{
				"type":        "string",
				"description": "The user task description or keywords used to match relevant skills.",
			},
			"limit": map[string]any{
				"type":        "integer",
				"description": "Maximum number of candidates to return.",
			},
		},
		Required: []string{},
	}
}

func (s *skillSearchTool) Run(ctx context.Context, call ToolCall) (ToolResponse, error) {
	var params SkillSearchParams
	if err := json.Unmarshal([]byte(call.Input), &params); err != nil {
		return NewTextErrorResponse(fmt.Sprintf("error parsing parameters: %s", err)), nil
	}
	if s.manager == nil {
		return NewTextErrorResponse("skill manager not initialized"), nil
	}

	candidates := s.manager.Search(params.Query, params.Limit)
	if len(candidates) == 0 {
		return WithResponseMetadata(
			NewTextResponse("No skills found"),
			SkillSearchMetadata{Query: params.Query, Candidates: []skills.Candidate{}},
		), nil
	}

	var b strings.Builder
	b.WriteString(fmt.Sprintf("Found %d skills:\n", len(candidates)))
	for i, c := range candidates {
		b.WriteString(fmt.Sprintf("%d. %s (score=%.1f)\n", i+1, c.Meta.Name, c.Score))
		if c.Meta.Description != "" {
			b.WriteString(fmt.Sprintf("   %s\n", c.Meta.Description))
		}
		b.WriteString(fmt.Sprintf("   path: %s\n", c.Meta.Path))
	}

	return WithResponseMetadata(
		NewTextResponse(strings.TrimSpace(b.String())),
		SkillSearchMetadata{
			Query:      params.Query,
			Candidates: candidates,
		},
	), nil
}
