package skills

import (
	"crypto/sha1"
	"encoding/hex"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/fsnotify/fsnotify"
)

type usageEntry struct {
	Count    int
	LastUsed time.Time
}

type Manager struct {
	workspaceRoot string
	options       Options

	mu           sync.RWMutex
	skillsByID   map[string]Meta
	skillIDByKey map[string]string
	usage        map[string]usageEntry
	roots        []string

	watcher    *fsnotify.Watcher
	debounceMu sync.Mutex
	debounce   *time.Timer
}

func NewManager(workspaceRoot string, options Options) (*Manager, error) {
	rootAbs, err := filepath.Abs(filepath.Clean(workspaceRoot))
	if err != nil {
		return nil, err
	}
	opts := normalizeOptions(options)
	m := &Manager{
		workspaceRoot: rootAbs,
		options:       opts,
		skillsByID:    map[string]Meta{},
		skillIDByKey:  map[string]string{},
		usage:         map[string]usageEntry{},
		roots:         []string{},
	}
	if !opts.Enabled {
		return m, nil
	}
	if err := m.refresh(); err != nil {
		return nil, err
	}
	if opts.Watch {
		if err := m.startWatcher(); err != nil {
			return nil, err
		}
	}
	return m, nil
}

func (m *Manager) Search(query string, limit int) []Candidate {
	m.mu.RLock()
	metas := make([]Meta, 0, len(m.skillsByID))
	for _, meta := range m.skillsByID {
		metas = append(metas, meta)
	}
	usage := make(map[string]usageEntry, len(m.usage))
	for k, v := range m.usage {
		usage[k] = v
	}
	maxCandidates := m.options.MaxCandidates
	m.mu.RUnlock()

	if limit <= 0 {
		limit = maxCandidates
	}
	if limit <= 0 {
		limit = 8
	}

	query = strings.TrimSpace(strings.ToLower(query))
	candidates := make([]Candidate, 0, len(metas))
	for _, meta := range metas {
		score := scoreMeta(meta, query, usage[meta.ID])
		if query == "" || score > 0 {
			candidates = append(candidates, Candidate{Meta: meta, Score: score})
		}
	}

	sort.SliceStable(candidates, func(i, j int) bool {
		if candidates[i].Score == candidates[j].Score {
			if candidates[i].Meta.UpdatedAt.Equal(candidates[j].Meta.UpdatedAt) {
				return strings.ToLower(candidates[i].Meta.Name) < strings.ToLower(candidates[j].Meta.Name)
			}
			return candidates[i].Meta.UpdatedAt.After(candidates[j].Meta.UpdatedAt)
		}
		return candidates[i].Score > candidates[j].Score
	})

	if len(candidates) > limit {
		return candidates[:limit]
	}
	return candidates
}

func (m *Manager) Load(identifier string, includeResources bool) (Document, error) {
	meta, ok := m.findMeta(identifier)
	if !ok {
		return Document{}, fmt.Errorf("skill not found: %s", identifier)
	}

	contentBytes, err := os.ReadFile(meta.Path)
	if err != nil {
		return Document{}, fmt.Errorf("read skill failed: %w", err)
	}
	fm, body, err := parseSkillContent(string(contentBytes))
	if err != nil {
		return Document{}, fmt.Errorf("parse skill failed: %w", err)
	}
	if fm.Name != "" {
		meta.Name = fm.Name
	}
	if fm.Description != "" {
		meta.Description = fm.Description
	}

	doc := Document{
		Meta: meta,
		Body: body,
	}
	if includeResources {
		doc.Resources = listResources(filepath.Dir(meta.Path))
	}
	m.markUsed(meta.ID)
	return doc, nil
}

func (m *Manager) Stats() map[string]any {
	m.mu.RLock()
	defer m.mu.RUnlock()
	return map[string]any{
		"skills": len(m.skillsByID),
		"roots":  append([]string{}, m.roots...),
	}
}

func (m *Manager) findMeta(identifier string) (Meta, bool) {
	key := strings.TrimSpace(identifier)
	if key == "" {
		return Meta{}, false
	}

	m.mu.RLock()
	defer m.mu.RUnlock()
	if meta, ok := m.skillsByID[key]; ok {
		return meta, true
	}
	id := m.skillIDByKey[strings.ToLower(key)]
	if id == "" {
		return Meta{}, false
	}
	meta, ok := m.skillsByID[id]
	return meta, ok
}

func (m *Manager) markUsed(skillID string) {
	if skillID == "" {
		return
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	entry := m.usage[skillID]
	entry.Count++
	entry.LastUsed = time.Now()
	m.usage[skillID] = entry
}

func (m *Manager) refresh() error {
	roots := m.discoverRoots()
	indexed := m.indexSkills(roots)

	byID := map[string]Meta{}
	byKey := map[string]string{}
	for _, meta := range indexed {
		byID[meta.ID] = meta
		byKey[strings.ToLower(meta.Name)] = meta.ID
		byKey[strings.ToLower(meta.Path)] = meta.ID
	}

	m.mu.Lock()
	m.roots = roots
	m.skillsByID = byID
	m.skillIDByKey = byKey
	m.mu.Unlock()
	return nil
}

func (m *Manager) indexSkills(roots []string) []Meta {
	type pick struct {
		meta Meta
		rank int
	}
	chosenByName := map[string]pick{}
	chosenByPath := map[string]Meta{}

	for _, root := range roots {
		source := detectSource(m.workspaceRoot, root)
		rootRank := sourceRank(source)
		_ = filepath.WalkDir(root, func(path string, d fs.DirEntry, err error) error {
			if err != nil {
				return nil
			}
			if d.IsDir() {
				if shouldSkipDir(path, d.Name()) {
					return filepath.SkipDir
				}
				return nil
			}
			if d.Name() != "SKILL.md" {
				return nil
			}

			pathAbs, err := filepath.Abs(filepath.Clean(path))
			if err != nil {
				return nil
			}
			contentBytes, err := os.ReadFile(pathAbs)
			if err != nil {
				return nil
			}
			fm, _, err := parseSkillContent(string(contentBytes))
			if err != nil {
				return nil
			}
			info, err := os.Stat(pathAbs)
			if err != nil {
				return nil
			}

			name := strings.TrimSpace(fm.Name)
			if name == "" {
				name = filepath.Base(filepath.Dir(pathAbs))
			}
			description := strings.TrimSpace(fm.Description)
			fp := fingerprint(pathAbs, info.ModTime(), info.Size(), name, description)
			meta := Meta{
				ID:          idFromPath(pathAbs),
				Name:        name,
				Description: description,
				Path:        pathAbs,
				Root:        root,
				Source:      source,
				UpdatedAt:   info.ModTime(),
				Fingerprint: fp,
			}

			pathKey := canonicalPath(pathAbs)
			if _, ok := chosenByPath[pathKey]; ok {
				return nil
			}
			chosenByPath[pathKey] = meta

			nameKey := strings.ToLower(name)
			prev, ok := chosenByName[nameKey]
			if !ok {
				chosenByName[nameKey] = pick{meta: meta, rank: rootRank}
				return nil
			}
			if rootRank > prev.rank {
				chosenByName[nameKey] = pick{meta: meta, rank: rootRank}
				return nil
			}
			if rootRank == prev.rank && meta.UpdatedAt.After(prev.meta.UpdatedAt) {
				chosenByName[nameKey] = pick{meta: meta, rank: rootRank}
			}
			return nil
		})
	}

	metas := make([]Meta, 0, len(chosenByName))
	for _, p := range chosenByName {
		metas = append(metas, p.meta)
	}
	sort.SliceStable(metas, func(i, j int) bool {
		return strings.ToLower(metas[i].Name) < strings.ToLower(metas[j].Name)
	})
	return metas
}

func (m *Manager) discoverRoots() []string {
	roots := make([]string, 0, 8)
	seen := map[string]bool{}

	add := func(path string) {
		if path == "" {
			return
		}
		abs, err := filepath.Abs(filepath.Clean(path))
		if err != nil {
			return
		}
		info, err := os.Stat(abs)
		if err != nil || !info.IsDir() {
			return
		}
		key := canonicalPath(abs)
		if seen[key] {
			return
		}
		seen[key] = true
		roots = append(roots, abs)
	}

	for _, r := range m.options.Roots {
		if r == "" {
			continue
		}
		path := r
		if !filepath.IsAbs(path) {
			path = filepath.Join(m.workspaceRoot, path)
		}
		add(path)
	}

	if m.options.IncludeUserHome {
		home, err := os.UserHomeDir()
		if err == nil && home != "" {
			add(filepath.Join(home, ".trae", "skills"))
		}
	}

	for _, nested := range discoverNestedRoots(m.workspaceRoot) {
		add(nested)
	}

	return roots
}

func (m *Manager) startWatcher() error {
	w, err := fsnotify.NewWatcher()
	if err != nil {
		return err
	}
	m.watcher = w

	m.mu.RLock()
	roots := append([]string{}, m.roots...)
	m.mu.RUnlock()

	for _, root := range roots {
		_ = addWatcherRecursive(w, root)
	}

	go m.watchLoop()
	return nil
}

func (m *Manager) watchLoop() {
	for {
		select {
		case event, ok := <-m.watcher.Events:
			if !ok {
				return
			}
			if event.Op&fsnotify.Create == fsnotify.Create {
				if fi, err := os.Stat(event.Name); err == nil && fi.IsDir() {
					_ = addWatcherRecursive(m.watcher, event.Name)
				}
			}
			if strings.Contains(canonicalPath(event.Name), canonicalPath(string(filepath.Separator)+".trae"+string(filepath.Separator)+"skills")) {
				m.scheduleRefresh()
				continue
			}
			base := strings.ToLower(filepath.Base(event.Name))
			if base == "skill.md" {
				m.scheduleRefresh()
			}
		case _, ok := <-m.watcher.Errors:
			if !ok {
				return
			}
		}
	}
}

func (m *Manager) scheduleRefresh() {
	m.debounceMu.Lock()
	defer m.debounceMu.Unlock()
	if m.debounce != nil {
		m.debounce.Stop()
	}
	m.debounce = time.AfterFunc(250*time.Millisecond, func() {
		_ = m.refresh()
		if m.watcher != nil {
			m.mu.RLock()
			roots := append([]string{}, m.roots...)
			m.mu.RUnlock()
			for _, root := range roots {
				_ = addWatcherRecursive(m.watcher, root)
			}
		}
	})
}

func normalizeOptions(options Options) Options {
	if len(options.Roots) == 0 {
		options.Roots = []string{".trae/skills"}
	}
	if options.MaxCandidates <= 0 {
		options.MaxCandidates = 8
	}
	return options
}

func scoreMeta(meta Meta, query string, usage usageEntry) float64 {
	base := 0.0
	name := strings.ToLower(meta.Name)
	description := strings.ToLower(meta.Description)
	if query == "" {
		base = 1
	} else {
		if name == query {
			base += 120
		}
		if strings.Contains(name, query) {
			base += 70
		}
		if strings.Contains(description, query) {
			base += 45
		}
		for _, token := range splitTokens(query) {
			if token == "" {
				continue
			}
			if strings.Contains(name, token) {
				base += 10
			}
			if strings.Contains(description, token) {
				base += 6
			}
		}
	}
	base += float64(usage.Count) * 1.5
	if !usage.LastUsed.IsZero() {
		hours := time.Since(usage.LastUsed).Hours()
		if hours < 24 {
			base += 8
		} else if hours < 24*7 {
			base += 3
		}
	}
	return base
}

func splitTokens(query string) []string {
	query = strings.TrimSpace(query)
	if query == "" {
		return nil
	}
	seps := []string{" ", "\t", "\n", ",", "，", ";", "；", "|", "/"}
	for _, s := range seps {
		query = strings.ReplaceAll(query, s, " ")
	}
	raw := strings.Split(query, " ")
	out := make([]string, 0, len(raw))
	for _, p := range raw {
		p = strings.TrimSpace(p)
		if p != "" {
			out = append(out, p)
		}
	}
	return out
}

func listResources(skillDir string) []string {
	out := make([]string, 0, 64)
	targetDirs := map[string]bool{
		"references": true,
		"examples":   true,
		"scripts":    true,
	}
	_ = filepath.WalkDir(skillDir, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return nil
		}
		if d.IsDir() {
			return nil
		}
		if strings.EqualFold(d.Name(), "SKILL.md") {
			return nil
		}
		rel, err := filepath.Rel(skillDir, path)
		if err != nil {
			return nil
		}
		parts := strings.Split(filepath.ToSlash(rel), "/")
		if len(parts) < 2 {
			return nil
		}
		if targetDirs[parts[0]] {
			out = append(out, path)
		}
		return nil
	})
	sort.Strings(out)
	return out
}

func idFromPath(path string) string {
	h := sha1.Sum([]byte(canonicalPath(path)))
	return hex.EncodeToString(h[:])
}

func fingerprint(path string, updatedAt time.Time, size int64, name string, description string) string {
	raw := fmt.Sprintf("%s|%d|%d|%s|%s", canonicalPath(path), updatedAt.UnixNano(), size, name, description)
	sum := sha1.Sum([]byte(raw))
	return hex.EncodeToString(sum[:])
}

func sourceRank(source string) int {
	switch source {
	case "workspace":
		return 3
	case "nested":
		return 2
	case "home":
		return 1
	default:
		return 0
	}
}

func detectSource(workspaceRoot string, root string) string {
	workspaceKey := canonicalPath(workspaceRoot)
	rootKey := canonicalPath(root)
	if strings.HasPrefix(rootKey, workspaceKey) {
		if rootKey == canonicalPath(filepath.Join(workspaceRoot, ".trae", "skills")) {
			return "workspace"
		}
		return "nested"
	}
	return "home"
}

func shouldSkipDir(path string, base string) bool {
	if strings.HasPrefix(base, ".") && !strings.EqualFold(base, ".trae") {
		return true
	}
	switch strings.ToLower(base) {
	case "node_modules", "vendor", ".git", ".idea", ".vscode", "dist", "build", "target", "bin", "obj", "out", "coverage", "tmp", "temp":
		return true
	}
	return false
}

func discoverNestedRoots(workspaceRoot string) []string {
	out := make([]string, 0, 8)
	seen := map[string]bool{}
	_ = filepath.WalkDir(workspaceRoot, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return nil
		}
		if d.IsDir() {
			base := d.Name()
			if shouldSkipDir(path, base) {
				return filepath.SkipDir
			}
			if strings.EqualFold(base, "skills") && strings.EqualFold(filepath.Base(filepath.Dir(path)), ".trae") {
				key := canonicalPath(path)
				if !seen[key] {
					seen[key] = true
					out = append(out, path)
				}
				return filepath.SkipDir
			}
		}
		return nil
	})
	sort.Strings(out)
	return out
}

func addWatcherRecursive(w *fsnotify.Watcher, root string) error {
	return filepath.WalkDir(root, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return nil
		}
		if !d.IsDir() {
			return nil
		}
		if shouldSkipDir(path, d.Name()) {
			return filepath.SkipDir
		}
		_ = w.Add(path)
		return nil
	})
}

func canonicalPath(path string) string {
	clean := filepath.Clean(path)
	if runtime.GOOS == "windows" {
		return strings.ToLower(clean)
	}
	return clean
}
