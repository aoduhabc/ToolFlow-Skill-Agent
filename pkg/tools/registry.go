package tools

import (
	"path/filepath"

	"github.com/example/demo-tools-bridge/pkg/lsp"
	"github.com/example/demo-tools-bridge/pkg/skills"
)

type Registry struct {
	RootAbs       string
	Tools         map[string]BaseTool
	LSPClients    map[string]*lsp.Client
	SkillsManager *skills.Manager
}

func NewRegistry(root string) (*Registry, error) {
	rootAbs, err := filepath.Abs(filepath.Clean(root))
	if err != nil {
		return nil, err
	}

	r := &Registry{
		RootAbs:       rootAbs,
		Tools:         map[string]BaseTool{},
		LSPClients:    map[string]*lsp.Client{},
		SkillsManager: nil,
	}

	r.Tools[GlobToolName] = NewGlobTool(rootAbs)
	r.Tools[GrepToolName] = NewGrepTool(rootAbs)
	r.Tools[LSToolName] = NewLsTool(rootAbs)
	r.Tools[ViewToolName] = NewViewTool(rootAbs)
	r.Tools[WriteToolName] = NewWriteTool(rootAbs)
	r.Tools[EditToolName] = NewEditTool(rootAbs)
	r.Tools[PatchToolName] = NewPatchTool(rootAbs)
	r.Tools[FetchToolName] = NewFetchTool()
	r.Tools[BashToolName] = NewBashTool()
	r.Tools[DiagnosticsToolName] = NewDiagnosticsTool(rootAbs)
	r.Tools[SkillSearchToolName] = NewSkillSearchTool(rootAbs)
	r.Tools[SkillLoadToolName] = NewSkillLoadTool(rootAbs)

	return r, nil
}

func (r *Registry) SetLSPClients(clients map[string]*lsp.Client) {
	r.LSPClients = clients
	// Attach to tools that can use LSP
	if vt, ok := r.Tools[ViewToolName].(*viewTool); ok {
		vt.lsps = clients
	}
	if wt, ok := r.Tools[WriteToolName].(*writeTool); ok {
		wt.lsps = clients
	}
	if et, ok := r.Tools[EditToolName].(*editTool); ok {
		et.lsps = clients
	}
	if pt, ok := r.Tools[PatchToolName].(*patchTool); ok {
		pt.lsps = clients
	}
	if dt, ok := r.Tools[DiagnosticsToolName].(*diagnosticsTool); ok {
		dt.lsps = clients
	}
}

func (r *Registry) SetSkillsManager(manager *skills.Manager) {
	r.SkillsManager = manager
	if st, ok := r.Tools[SkillSearchToolName].(*skillSearchTool); ok {
		st.SetManager(manager)
	}
	if st, ok := r.Tools[SkillLoadToolName].(*skillLoadTool); ok {
		st.SetManager(manager)
	}
}

func (r *Registry) List() []ToolInfo {
	out := make([]ToolInfo, 0, len(r.Tools))
	for _, t := range r.Tools {
		out = append(out, t.Info())
	}
	return out
}
