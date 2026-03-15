package skills

import "testing"

func TestParseSkillContentWithFrontmatter(t *testing.T) {
	input := `---
name: "code-review"
description: "review code"
version: "1.0.0"
when_to_use: "when need review"
---
# Title

Body line`

	fm, body, err := parseSkillContent(input)
	if err != nil {
		t.Fatalf("parseSkillContent returned error: %v", err)
	}
	if fm.Name != "code-review" {
		t.Fatalf("expected name code-review, got %s", fm.Name)
	}
	if fm.Description != "review code" {
		t.Fatalf("expected description review code, got %s", fm.Description)
	}
	if fm.Version != "1.0.0" {
		t.Fatalf("expected version 1.0.0, got %s", fm.Version)
	}
	if fm.WhenToUse != "when need review" {
		t.Fatalf("expected when_to_use when need review, got %s", fm.WhenToUse)
	}
	if body != "# Title\n\nBody line" {
		t.Fatalf("unexpected body: %q", body)
	}
}

func TestParseSkillContentWithoutFrontmatter(t *testing.T) {
	input := "# Just Body\n\nhello"
	fm, body, err := parseSkillContent(input)
	if err != nil {
		t.Fatalf("parseSkillContent returned error: %v", err)
	}
	if fm.Name != "" || fm.Description != "" || fm.Version != "" || fm.WhenToUse != "" {
		t.Fatalf("expected empty frontmatter, got %+v", fm)
	}
	if body != input {
		t.Fatalf("unexpected body: %q", body)
	}
}

func TestParseSkillContentInvalidFrontmatter(t *testing.T) {
	input := `---
name: "x"`
	_, _, err := parseSkillContent(input)
	if err == nil {
		t.Fatalf("expected error for invalid frontmatter")
	}
}
