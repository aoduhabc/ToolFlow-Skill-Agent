package tools

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

func absClean(path string) (string, error) {
	if path == "" {
		return "", fmt.Errorf("empty path")
	}
	if !filepath.IsAbs(path) {
		path = filepath.Join(WorkingDir(), path)
	}
	path = filepath.Clean(path)
	abs, err := filepath.Abs(path)
	if err != nil {
		return "", err
	}
	return abs, nil
}

func isWithinRoot(rootAbs string, targetAbs string) bool {
	rootAbs = filepath.Clean(rootAbs)
	targetAbs = filepath.Clean(targetAbs)

	if rootAbs == targetAbs {
		return true
	}
	if strings.HasPrefix(strings.ToLower(targetAbs), strings.ToLower(rootAbs)+string(os.PathSeparator)) {
		return true
	}
	return false
}
