package config

import (
	"os"
	"path/filepath"
	"testing"
)

func TestLoadFromFileDefaultsSkillsWhenMissing(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "cfg.json")
	data := `{"debugLsp":true,"lsp":{"gopls":{"disabled":false,"command":"gopls","args":[]}}}`
	if err := os.WriteFile(path, []byte(data), 0o644); err != nil {
		t.Fatalf("WriteFile failed: %v", err)
	}

	cfg, err := LoadFromFile(path)
	if err != nil {
		t.Fatalf("LoadFromFile returned error: %v", err)
	}
	if !cfg.Skills.Enabled {
		t.Fatalf("expected skills.enabled true by default")
	}
	if !cfg.Skills.IncludeUserHome {
		t.Fatalf("expected skills.includeUserHome true by default")
	}
	if !cfg.Skills.Watch {
		t.Fatalf("expected skills.watch true by default")
	}
	if cfg.Skills.MaxCandidates != 8 {
		t.Fatalf("expected skills.maxCandidates 8, got %d", cfg.Skills.MaxCandidates)
	}
	if len(cfg.Skills.Roots) != 1 || cfg.Skills.Roots[0] != ".trae/skills" {
		t.Fatalf("unexpected default roots: %+v", cfg.Skills.Roots)
	}
}

func TestLoadFromFileKeepsExplicitSkillsValues(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "cfg.json")
	data := `{
  "skills":{
    "enabled":false,
    "includeUserHome":false,
    "watch":false,
    "roots":["custom/skills"],
    "maxCandidates":3
  }
}`
	if err := os.WriteFile(path, []byte(data), 0o644); err != nil {
		t.Fatalf("WriteFile failed: %v", err)
	}

	cfg, err := LoadFromFile(path)
	if err != nil {
		t.Fatalf("LoadFromFile returned error: %v", err)
	}
	if cfg.Skills.Enabled {
		t.Fatalf("expected skills.enabled false")
	}
	if cfg.Skills.IncludeUserHome {
		t.Fatalf("expected skills.includeUserHome false")
	}
	if cfg.Skills.Watch {
		t.Fatalf("expected skills.watch false")
	}
	if cfg.Skills.MaxCandidates != 3 {
		t.Fatalf("expected skills.maxCandidates 3, got %d", cfg.Skills.MaxCandidates)
	}
	if len(cfg.Skills.Roots) != 1 || cfg.Skills.Roots[0] != "custom/skills" {
		t.Fatalf("unexpected skills roots: %+v", cfg.Skills.Roots)
	}
}
