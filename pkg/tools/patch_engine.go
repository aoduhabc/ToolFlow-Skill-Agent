package tools

import (
	"fmt"
	"strings"
)

type ActionType string

const (
	ActionAdd    ActionType = "add"
	ActionDelete ActionType = "delete"
	ActionUpdate ActionType = "update"
)

type FileChange struct {
	Type       ActionType
	OldContent *string
	NewContent *string
	MovePath   *string
}

type Commit struct {
	Changes map[string]FileChange
}

type Chunk struct {
	OrigIndex int
	DelLines  []string
	InsLines  []string
}

type PatchAction struct {
	Type     ActionType
	NewFile  *string
	Chunks   []Chunk
	MovePath *string
}

type Patch struct {
	Actions map[string]PatchAction
}

type patchError struct {
	message string
}

func (e patchError) Error() string {
	return e.message
}

func newPatchError(message string) patchError {
	return patchError{message: message}
}

func fileError(action string, reason string, path string) patchError {
	return newPatchError(fmt.Sprintf("%s File Error: %s: %s", action, reason, path))
}

func contextError(index int, context string, isEOF bool) patchError {
	prefix := "Invalid Context"
	if isEOF {
		prefix = "Invalid EOF Context"
	}
	return newPatchError(fmt.Sprintf("%s %d:\n%s", prefix, index, context))
}

type parser struct {
	currentFiles map[string]string
	lines        []string
	index        int
	patch        Patch
	fuzz         int
}

func newParser(currentFiles map[string]string, lines []string) *parser {
	return &parser{
		currentFiles: currentFiles,
		lines:        lines,
		index:        0,
		patch:        Patch{Actions: make(map[string]PatchAction, len(currentFiles))},
		fuzz:         0,
	}
}

func (p *parser) isDone(prefixes []string) bool {
	if p.index >= len(p.lines) {
		return true
	}
	for _, prefix := range prefixes {
		if strings.HasPrefix(p.lines[p.index], prefix) {
			return true
		}
	}
	return false
}

func (p *parser) startsWith(prefix string) bool {
	return strings.HasPrefix(p.lines[p.index], prefix)
}

func (p *parser) readStr(prefix string, returnEverything bool) string {
	if p.index >= len(p.lines) {
		return ""
	}
	if strings.HasPrefix(p.lines[p.index], prefix) {
		text := p.lines[p.index]
		if !returnEverything {
			text = p.lines[p.index][len(prefix):]
		}
		p.index++
		return text
	}
	return ""
}

func (p *parser) Parse() error {
	endPatchPrefixes := []string{"*** End Patch"}
	for !p.isDone(endPatchPrefixes) {
		path := p.readStr("*** Update File: ", false)
		if path != "" {
			if _, exists := p.patch.Actions[path]; exists {
				return fileError("Update", "Duplicate Path", path)
			}
			moveTo := p.readStr("*** Move to: ", false)
			if _, exists := p.currentFiles[path]; !exists {
				return fileError("Update", "Missing File", path)
			}
			action, err := p.parseUpdateFile(p.currentFiles[path])
			if err != nil {
				return err
			}
			if moveTo != "" {
				action.MovePath = &moveTo
			}
			p.patch.Actions[path] = action
			continue
		}

		path = p.readStr("*** Delete File: ", false)
		if path != "" {
			if _, exists := p.patch.Actions[path]; exists {
				return fileError("Delete", "Duplicate Path", path)
			}
			if _, exists := p.currentFiles[path]; !exists {
				return fileError("Delete", "Missing File", path)
			}
			p.patch.Actions[path] = PatchAction{Type: ActionDelete, Chunks: []Chunk{}}
			continue
		}

		path = p.readStr("*** Add File: ", false)
		if path != "" {
			if _, exists := p.patch.Actions[path]; exists {
				return fileError("Add", "Duplicate Path", path)
			}
			if _, exists := p.currentFiles[path]; exists {
				return fileError("Add", "File already exists", path)
			}
			action, err := p.parseAddFile()
			if err != nil {
				return err
			}
			p.patch.Actions[path] = action
			continue
		}
		return newPatchError(fmt.Sprintf("Unknown Line: %s", p.lines[p.index]))
	}
	if !p.startsWith("*** End Patch") {
		return newPatchError("Missing End Patch")
	}
	p.index++
	return nil
}

func (p *parser) parseUpdateFile(text string) (PatchAction, error) {
	action := PatchAction{Type: ActionUpdate, Chunks: []Chunk{}}
	fileLines := strings.Split(text, "\n")
	index := 0
	endPrefixes := []string{
		"*** End Patch",
		"*** Update File:",
		"*** Delete File:",
		"*** Add File:",
		"*** End of File",
	}
	for !p.isDone(endPrefixes) {
		defStr := p.readStr("@@ ", false)
		sectionStr := ""
		if defStr == "" && p.index < len(p.lines) && p.lines[p.index] == "@@" {
			sectionStr = p.lines[p.index]
			p.index++
		}
		if defStr == "" && sectionStr == "" && index != 0 {
			return action, newPatchError(fmt.Sprintf("Invalid Line:\n%s", p.lines[p.index]))
		}
		if strings.TrimSpace(defStr) != "" {
			found := false
			for i := range fileLines[:index] {
				if fileLines[i] == defStr {
					found = true
					break
				}
			}
			if !found {
				for i := index; i < len(fileLines); i++ {
					if fileLines[i] == defStr {
						index = i + 1
						found = true
						break
					}
				}
			}
			if !found {
				for i := range fileLines[:index] {
					if strings.TrimSpace(fileLines[i]) == strings.TrimSpace(defStr) {
						found = true
						break
					}
				}
			}
			if !found {
				for i := index; i < len(fileLines); i++ {
					if strings.TrimSpace(fileLines[i]) == strings.TrimSpace(defStr) {
						index = i + 1
						p.fuzz++
						found = true
						break
					}
				}
			}
		}
		nextChunkContext, chunks, endPatchIndex, eof := peekNextSection(p.lines, p.index)
		newIndex, fuzz := findContext(fileLines, nextChunkContext, index, eof)
		if newIndex == -1 {
			ctxText := strings.Join(nextChunkContext, "\n")
			return action, contextError(index, ctxText, eof)
		}
		p.fuzz += fuzz
		for _, ch := range chunks {
			ch.OrigIndex += newIndex
			action.Chunks = append(action.Chunks, ch)
		}
		index = newIndex + len(nextChunkContext)
		p.index = endPatchIndex
	}
	return action, nil
}

func (p *parser) parseAddFile() (PatchAction, error) {
	lines := make([]string, 0, 16)
	endPrefixes := []string{
		"*** End Patch",
		"*** Update File:",
		"*** Delete File:",
		"*** Add File:",
	}
	for !p.isDone(endPrefixes) {
		s := p.readStr("", true)
		if !strings.HasPrefix(s, "+") {
			return PatchAction{}, newPatchError(fmt.Sprintf("Invalid Add File Line: %s", s))
		}
		lines = append(lines, s[1:])
	}
	newFile := strings.Join(lines, "\n")
	return PatchAction{Type: ActionAdd, NewFile: &newFile, Chunks: []Chunk{}}, nil
}

func tryFindMatch(lines []string, context []string, start int, compareFunc func(string, string) bool) (int, int) {
	for i := start; i < len(lines); i++ {
		if i+len(context) <= len(lines) {
			match := true
			for j := range context {
				if !compareFunc(lines[i+j], context[j]) {
					match = false
					break
				}
			}
			if match {
				var fuzz int
				if compareFunc("a ", "a") && !compareFunc("a", "b") {
					fuzz = 1
				} else if compareFunc("a  ", "a") {
					fuzz = 100
				}
				return i, fuzz
			}
		}
	}
	return -1, 0
}

func findContextCore(lines []string, context []string, start int) (int, int) {
	if len(context) == 0 {
		return start, 0
	}
	if idx, fuzz := tryFindMatch(lines, context, start, func(a, b string) bool { return a == b }); idx >= 0 {
		return idx, fuzz
	}
	if idx, fuzz := tryFindMatch(lines, context, start, func(a, b string) bool {
		return strings.TrimRight(a, " \t") == strings.TrimRight(b, " \t")
	}); idx >= 0 {
		return idx, fuzz
	}
	if idx, fuzz := tryFindMatch(lines, context, start, func(a, b string) bool {
		return strings.TrimSpace(a) == strings.TrimSpace(b)
	}); idx >= 0 {
		return idx, fuzz
	}
	return -1, 0
}

func findContext(lines []string, context []string, start int, eof bool) (int, int) {
	if eof {
		newIndex, fuzz := findContextCore(lines, context, len(lines)-len(context))
		if newIndex != -1 {
			return newIndex, fuzz
		}
		newIndex, fuzz = findContextCore(lines, context, start)
		return newIndex, fuzz + 10000
	}
	return findContextCore(lines, context, start)
}

func peekNextSection(lines []string, initialIndex int) ([]string, []Chunk, int, bool) {
	index := initialIndex
	old := make([]string, 0, 32)
	delLines := make([]string, 0, 8)
	insLines := make([]string, 0, 8)
	chunks := make([]Chunk, 0, 4)
	mode := "keep"
	endSection := func(s string) bool {
		return strings.HasPrefix(s, "@@") ||
			strings.HasPrefix(s, "*** End Patch") ||
			strings.HasPrefix(s, "*** Update File:") ||
			strings.HasPrefix(s, "*** Delete File:") ||
			strings.HasPrefix(s, "*** Add File:") ||
			strings.HasPrefix(s, "*** End of File") ||
			s == "***" ||
			strings.HasPrefix(s, "***")
	}
	for index < len(lines) {
		s := lines[index]
		if endSection(s) {
			break
		}
		index++
		lastMode := mode
		line := s
		if len(line) > 0 {
			switch line[0] {
			case '+':
				mode = "add"
			case '-':
				mode = "delete"
			case ' ':
				mode = "keep"
			default:
				mode = "keep"
				line = " " + line
			}
		} else {
			mode = "keep"
			line = " "
		}
		line = line[1:]
		if mode == "keep" && lastMode != mode {
			if len(insLines) > 0 || len(delLines) > 0 {
				chunks = append(chunks, Chunk{
					OrigIndex: len(old) - len(delLines),
					DelLines:  delLines,
					InsLines:  insLines,
				})
			}
			delLines = make([]string, 0, 8)
			insLines = make([]string, 0, 8)
		}
		switch mode {
		case "delete":
			delLines = append(delLines, line)
			old = append(old, line)
		case "add":
			insLines = append(insLines, line)
		default:
			old = append(old, line)
		}
	}
	if len(insLines) > 0 || len(delLines) > 0 {
		chunks = append(chunks, Chunk{
			OrigIndex: len(old) - len(delLines),
			DelLines:  delLines,
			InsLines:  insLines,
		})
	}
	if index < len(lines) && lines[index] == "*** End of File" {
		index++
		return old, chunks, index, true
	}
	return old, chunks, index, false
}

func TextToPatch(text string, orig map[string]string) (Patch, int, error) {
	text = strings.TrimSpace(text)
	lines := strings.Split(text, "\n")
	if len(lines) < 2 || !strings.HasPrefix(lines[0], "*** Begin Patch") || lines[len(lines)-1] != "*** End Patch" {
		return Patch{}, 0, newPatchError("Invalid patch text")
	}
	p := newParser(orig, lines)
	p.index = 1
	if err := p.Parse(); err != nil {
		return Patch{}, 0, err
	}
	return p.patch, p.fuzz, nil
}

func IdentifyFilesNeeded(text string) []string {
	text = strings.TrimSpace(text)
	lines := strings.Split(text, "\n")
	result := map[string]bool{}
	for _, line := range lines {
		if strings.HasPrefix(line, "*** Update File: ") {
			result[line[len("*** Update File: "):]] = true
		}
		if strings.HasPrefix(line, "*** Delete File: ") {
			result[line[len("*** Delete File: "):]] = true
		}
	}
	files := make([]string, 0, len(result))
	for file := range result {
		files = append(files, file)
	}
	return files
}

func IdentifyFilesAdded(text string) []string {
	text = strings.TrimSpace(text)
	lines := strings.Split(text, "\n")
	result := map[string]bool{}
	for _, line := range lines {
		if strings.HasPrefix(line, "*** Add File: ") {
			result[line[len("*** Add File: "):]] = true
		}
	}
	files := make([]string, 0, len(result))
	for file := range result {
		files = append(files, file)
	}
	return files
}

func getUpdatedFile(text string, action PatchAction, path string) (string, error) {
	if action.Type != ActionUpdate {
		return "", fmt.Errorf("expected update action")
	}
	origLines := strings.Split(text, "\n")
	destLines := make([]string, 0, len(origLines))
	origIndex := 0
	for _, chunk := range action.Chunks {
		if chunk.OrigIndex > len(origLines) {
			return "", newPatchError(fmt.Sprintf("%s: chunk.orig_index %d > len(lines) %d", path, chunk.OrigIndex, len(origLines)))
		}
		if origIndex > chunk.OrigIndex {
			return "", newPatchError(fmt.Sprintf("%s: orig_index %d > chunk.orig_index %d", path, origIndex, chunk.OrigIndex))
		}
		destLines = append(destLines, origLines[origIndex:chunk.OrigIndex]...)
		origIndex += chunk.OrigIndex - origIndex
		if len(chunk.InsLines) > 0 {
			destLines = append(destLines, chunk.InsLines...)
		}
		origIndex += len(chunk.DelLines)
	}
	destLines = append(destLines, origLines[origIndex:]...)
	return strings.Join(destLines, "\n"), nil
}

func PatchToCommit(patch Patch, orig map[string]string) (Commit, error) {
	commit := Commit{Changes: make(map[string]FileChange, len(patch.Actions))}
	for pathKey, action := range patch.Actions {
		switch action.Type {
		case ActionDelete:
			oldContent := orig[pathKey]
			commit.Changes[pathKey] = FileChange{Type: ActionDelete, OldContent: &oldContent}
		case ActionAdd:
			commit.Changes[pathKey] = FileChange{Type: ActionAdd, NewContent: action.NewFile}
		case ActionUpdate:
			newContent, err := getUpdatedFile(orig[pathKey], action, pathKey)
			if err != nil {
				return Commit{}, err
			}
			oldContent := orig[pathKey]
			change := FileChange{
				Type:       ActionUpdate,
				OldContent: &oldContent,
				NewContent: &newContent,
			}
			if action.MovePath != nil {
				change.MovePath = action.MovePath
			}
			commit.Changes[pathKey] = change
		}
	}
	return commit, nil
}

func ApplyCommit(commit Commit, writeFn func(string, string) error, removeFn func(string) error) error {
	for p, change := range commit.Changes {
		switch change.Type {
		case ActionDelete:
			if err := removeFn(p); err != nil {
				return err
			}
		case ActionAdd:
			if change.NewContent == nil {
				return newPatchError(fmt.Sprintf("add action for %s has nil new_content", p))
			}
			if err := writeFn(p, *change.NewContent); err != nil {
				return err
			}
		case ActionUpdate:
			if change.NewContent == nil {
				return newPatchError(fmt.Sprintf("update action for %s has nil new_content", p))
			}
			if change.MovePath != nil {
				if err := writeFn(*change.MovePath, *change.NewContent); err != nil {
					return err
				}
				if err := removeFn(p); err != nil {
					return err
				}
			} else {
				if err := writeFn(p, *change.NewContent); err != nil {
					return err
				}
			}
		}
	}
	return nil
}
