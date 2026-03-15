package skills

import "time"

type Options struct {
	Enabled         bool
	Roots           []string
	IncludeUserHome bool
	Watch           bool
	MaxCandidates   int
}

type Frontmatter struct {
	Name        string
	Description string
	Version     string
	WhenToUse   string
}

type Meta struct {
	ID          string    `json:"id"`
	Name        string    `json:"name"`
	Description string    `json:"description"`
	Path        string    `json:"path"`
	Root        string    `json:"root"`
	Source      string    `json:"source"`
	UpdatedAt   time.Time `json:"updated_at"`
	Fingerprint string    `json:"fingerprint"`
}

type Candidate struct {
	Meta  Meta    `json:"meta"`
	Score float64 `json:"score"`
}

type Document struct {
	Meta      Meta     `json:"meta"`
	Body      string   `json:"body"`
	Resources []string `json:"resources,omitempty"`
}
