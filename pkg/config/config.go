package config

import (
	"encoding/json"
	"os"
	"path/filepath"
	"sync"
)

type LSPConfig struct {
	Disabled bool     `json:"disabled"`
	Command  string   `json:"command"`
	Args     []string `json:"args"`
	Options  any      `json:"options"`
}

type SkillsConfig struct {
	Enabled         bool     `json:"enabled"`
	Roots           []string `json:"roots"`
	IncludeUserHome bool     `json:"includeUserHome"`
	Watch           bool     `json:"watch"`
	MaxCandidates   int      `json:"maxCandidates"`
}

type Config struct {
	DebugLSP bool                 `json:"debugLsp"`
	LSP      map[string]LSPConfig `json:"lsp"`
	Skills   SkillsConfig         `json:"skills"`
}

var (
	current = Config{
		LSP: map[string]LSPConfig{},
		Skills: SkillsConfig{
			Enabled:         true,
			Roots:           []string{".trae/skills"},
			IncludeUserHome: true,
			Watch:           true,
			MaxCandidates:   8,
		},
	}
	mu         sync.RWMutex
	workingDir string
)

func Get() *Config {
	mu.RLock()
	defer mu.RUnlock()
	cfg := current
	return &cfg
}

func Set(cfg Config) {
	mu.Lock()
	current = cfg
	if current.LSP == nil {
		current.LSP = map[string]LSPConfig{}
	}
	if len(current.Skills.Roots) == 0 {
		current.Skills.Roots = []string{".trae/skills"}
	}
	if current.Skills.MaxCandidates <= 0 {
		current.Skills.MaxCandidates = 8
	}
	mu.Unlock()
}

func WorkingDirectory() string {
	mu.RLock()
	defer mu.RUnlock()
	if workingDir != "" {
		return workingDir
	}
	cwd, err := os.Getwd()
	if err != nil {
		return "."
	}
	return cwd
}

func SetWorkingDirectory(dir string) {
	mu.Lock()
	workingDir = dir
	mu.Unlock()
}

func LoadFromFile(path string) (Config, error) {
	abs, err := filepath.Abs(path)
	if err != nil {
		return Config{}, err
	}
	data, err := os.ReadFile(abs)
	if err != nil {
		return Config{}, err
	}
	type diskConfig struct {
		DebugLSP bool                 `json:"debugLsp"`
		LSP      map[string]LSPConfig `json:"lsp"`
		Skills   *SkillsConfig        `json:"skills"`
	}
	var disk diskConfig
	if err := json.Unmarshal(data, &disk); err != nil {
		return Config{}, err
	}

	cfg := Config{
		DebugLSP: disk.DebugLSP,
		LSP:      disk.LSP,
		Skills: SkillsConfig{
			Enabled:         true,
			Roots:           []string{".trae/skills"},
			IncludeUserHome: true,
			Watch:           true,
			MaxCandidates:   8,
		},
	}
	if disk.Skills != nil {
		cfg.Skills = *disk.Skills
	}
	if cfg.LSP == nil {
		cfg.LSP = map[string]LSPConfig{}
	}
	if len(cfg.Skills.Roots) == 0 {
		cfg.Skills.Roots = []string{".trae/skills"}
	}
	if !cfg.Skills.Enabled && disk.Skills == nil {
		cfg.Skills.Enabled = true
	}
	if !cfg.Skills.IncludeUserHome && disk.Skills == nil {
		cfg.Skills.IncludeUserHome = true
	}
	if !cfg.Skills.Watch && disk.Skills == nil {
		cfg.Skills.Watch = true
	}
	if cfg.Skills.MaxCandidates <= 0 {
		cfg.Skills.MaxCandidates = 8
	}
	return cfg, nil
}
