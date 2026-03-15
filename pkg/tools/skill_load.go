package tools

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"

	"github.com/example/demo-tools-bridge/pkg/skills"
)

const (
	SkillLoadToolName = "skill_load"
)

type SkillLoadParams struct {
	Name             string `json:"name"`
	ID               string `json:"id"`
	IncludeResources bool   `json:"include_resources"`
}

type SkillLoadMetadata struct {
	SkillID         string   `json:"skill_id"`
	Name            string   `json:"name"`
	Path            string   `json:"path"`
	Resources       []string `json:"resources,omitempty"`
	ResourceCount   int      `json:"resource_count"`
	BodyLengthBytes int      `json:"body_length_bytes"`
}

type skillLoadTool struct {
	root    string
	manager *skills.Manager
}

func NewSkillLoadTool(root string) BaseTool {
	return &skillLoadTool{root: root}
}

func (s *skillLoadTool) SetManager(manager *skills.Manager) {
	s.manager = manager
}

func (s *skillLoadTool) Info() ToolInfo {
	return ToolInfo{
		Name:        SkillLoadToolName,
		Description: "Load a skill body on demand by id or name, optionally returning references/examples/scripts resources.",
		Parameters: map[string]any{
			"name": map[string]any{
				"type":        "string",
				"description": "Skill name to load.",
			},
			"id": map[string]any{
				"type":        "string",
				"description": "Skill ID to load. Takes precedence over name when both are present.",
			},
			"include_resources": map[string]any{
				"type":        "boolean",
				"description": "Whether to include resource file paths from references/examples/scripts.",
			},
		},
		Required: []string{},
	}
}

func (s *skillLoadTool) Run(ctx context.Context, call ToolCall) (ToolResponse, error) {
	var params SkillLoadParams
	if err := json.Unmarshal([]byte(call.Input), &params); err != nil {
		return NewTextErrorResponse(fmt.Sprintf("error parsing parameters: %s", err)), nil
	}
	if s.manager == nil {
		return NewTextErrorResponse("skill manager not initialized"), nil
	}

	identifier := strings.TrimSpace(params.ID)
	if identifier == "" {
		identifier = strings.TrimSpace(params.Name)
	}
	if identifier == "" {
		return NewTextErrorResponse("id or name is required"), nil
	}

	doc, err := s.manager.Load(identifier, params.IncludeResources)
	if err != nil {
		return NewTextErrorResponse(err.Error()), nil
	}

	var b strings.Builder
	b.WriteString(fmt.Sprintf("Skill: %s\n", doc.Meta.Name))
	b.WriteString(fmt.Sprintf("Path: %s\n\n", doc.Meta.Path))
	b.WriteString(doc.Body)
	if params.IncludeResources && len(doc.Resources) > 0 {
		b.WriteString("\n\nResources:\n")
		for _, r := range doc.Resources {
			b.WriteString("- ")
			b.WriteString(r)
			b.WriteString("\n")
		}
	}

	return WithResponseMetadata(
		NewTextResponse(strings.TrimSpace(b.String())),
		SkillLoadMetadata{
			SkillID:         doc.Meta.ID,
			Name:            doc.Meta.Name,
			Path:            doc.Meta.Path,
			Resources:       doc.Resources,
			ResourceCount:   len(doc.Resources),
			BodyLengthBytes: len(doc.Body),
		},
	), nil
}
