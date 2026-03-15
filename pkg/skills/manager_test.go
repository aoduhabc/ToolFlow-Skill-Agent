package skills

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestManagerSearchAndLoad(t *testing.T) {
	workspace := t.TempDir()
	mustWriteSkill(t, filepath.Join(workspace, ".trae", "skills", "code-review", "SKILL.md"), `---
name: "code-review"
description: "review code quality"
---
# Code Review Skill

Content A`)
	mustWriteSkill(t, filepath.Join(workspace, ".trae", "skills", "properties-generate", "SKILL.md"), `---
name: "properties-generate"
description: "generate security properties"
---
# Properties Skill

Content B`)

	mgr, err := NewManager(workspace, Options{
		Enabled:         true,
		Roots:           []string{".trae/skills"},
		IncludeUserHome: false,
		Watch:           false,
		MaxCandidates:   8,
	})
	if err != nil {
		t.Fatalf("NewManager returned error: %v", err)
	}

	candidates := mgr.Search("review", 10)
	if len(candidates) == 0 {
		t.Fatalf("expected at least one candidate")
	}
	if candidates[0].Meta.Name != "code-review" {
		t.Fatalf("expected top candidate code-review, got %s", candidates[0].Meta.Name)
	}

	doc, err := mgr.Load("code-review", false)
	if err != nil {
		t.Fatalf("Load returned error: %v", err)
	}
	if doc.Meta.Name != "code-review" {
		t.Fatalf("expected loaded name code-review, got %s", doc.Meta.Name)
	}
	if doc.Body == "" {
		t.Fatalf("expected non-empty body")
	}
}

func TestManagerWorkspaceOverridesNestedSameName(t *testing.T) {
	workspace := t.TempDir()
	workspacePath := filepath.Join(workspace, ".trae", "skills", "dup", "SKILL.md")
	nestedPath := filepath.Join(workspace, "moduleA", ".trae", "skills", "dup", "SKILL.md")

	mustWriteSkill(t, workspacePath, `---
name: "dup-skill"
description: "workspace"
---
workspace`)
	mustWriteSkill(t, nestedPath, `---
name: "dup-skill"
description: "nested"
---
nested`)

	now := time.Now()
	if err := os.Chtimes(workspacePath, now.Add(-2*time.Hour), now.Add(-2*time.Hour)); err != nil {
		t.Fatalf("failed to set workspace mtime: %v", err)
	}
	if err := os.Chtimes(nestedPath, now.Add(2*time.Hour), now.Add(2*time.Hour)); err != nil {
		t.Fatalf("failed to set nested mtime: %v", err)
	}

	mgr, err := NewManager(workspace, Options{
		Enabled:         true,
		Roots:           []string{".trae/skills"},
		IncludeUserHome: false,
		Watch:           false,
		MaxCandidates:   8,
	})
	if err != nil {
		t.Fatalf("NewManager returned error: %v", err)
	}

	doc, err := mgr.Load("dup-skill", false)
	if err != nil {
		t.Fatalf("Load returned error: %v", err)
	}
	expected := canonicalPath(workspacePath)
	actual := canonicalPath(doc.Meta.Path)
	if actual != expected {
		t.Fatalf("expected workspace path %s, got %s", expected, actual)
	}
}

func mustWriteSkill(t *testing.T, path string, content string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatalf("MkdirAll failed: %v", err)
	}
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatalf("WriteFile failed: %v", err)
	}
}
