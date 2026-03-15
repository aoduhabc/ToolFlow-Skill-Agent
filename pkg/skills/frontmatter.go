package skills

import (
	"fmt"
	"strings"
)

func parseSkillContent(content string) (Frontmatter, string, error) {
	trimmed := strings.TrimSpace(content)
	if trimmed == "" {
		return Frontmatter{}, "", fmt.Errorf("empty skill content")
	}

	if !strings.HasPrefix(trimmed, "---") {
		return Frontmatter{}, strings.TrimSpace(content), nil
	}

	lines := strings.Split(trimmed, "\n")
	if len(lines) < 3 {
		return Frontmatter{}, "", fmt.Errorf("invalid frontmatter")
	}

	endIdx := -1
	for i := 1; i < len(lines); i++ {
		if strings.TrimSpace(lines[i]) == "---" {
			endIdx = i
			break
		}
	}
	if endIdx == -1 {
		return Frontmatter{}, "", fmt.Errorf("frontmatter closing delimiter not found")
	}

	fm := Frontmatter{}
	for i := 1; i < endIdx; i++ {
		line := strings.TrimSpace(lines[i])
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		key, val, ok := strings.Cut(line, ":")
		if !ok {
			continue
		}
		key = strings.TrimSpace(strings.ToLower(key))
		val = normalizeScalar(strings.TrimSpace(val))
		switch key {
		case "name":
			fm.Name = val
		case "description":
			fm.Description = val
		case "version":
			fm.Version = val
		case "when_to_use":
			fm.WhenToUse = val
		}
	}

	body := strings.Join(lines[endIdx+1:], "\n")
	return fm, strings.TrimSpace(body), nil
}

func normalizeScalar(raw string) string {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return ""
	}
	if strings.HasPrefix(raw, "\"") && strings.HasSuffix(raw, "\"") && len(raw) >= 2 {
		return strings.TrimSpace(raw[1 : len(raw)-1])
	}
	if strings.HasPrefix(raw, "'") && strings.HasSuffix(raw, "'") && len(raw) >= 2 {
		return strings.TrimSpace(raw[1 : len(raw)-1])
	}
	return raw
}
