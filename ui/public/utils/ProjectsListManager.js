/**
 * ProjectsListManager: gestiona la lista de proyectos en el Dashboard
 * - Renderiza proyectos
 * - Inserta/actualiza elementos
 * - Maneja eliminación y carga manual
 */
export class ProjectsListManager {
  constructor(dashboard) {
    this.dashboard = dashboard;
  }

  render(projects) {
    const root = document.getElementById('projects-list');
    const empty = document.getElementById('projects-empty');
    if (!root) return;
    root.innerHTML = '';
    (projects || []).forEach(p => this._appendProjectRow(p));
    if (empty) empty.style.display = (projects && projects.length > 0) ? 'none' : '';
  }

  upsert(project) {
    const root = document.getElementById('projects-list');
    const empty = document.getElementById('projects-empty');
    if (!root || !project) return;
    const rowId = `proj-row-${project.id}`;
    const existing = document.getElementById(rowId);
    if (existing) existing.remove();
    this._appendProjectRow(project);
    if (empty) empty.style.display = 'none';
  }

  remove(projectId) {
    const row = document.getElementById(`proj-row-${projectId}`);
    const root = document.getElementById('projects-list');
    const empty = document.getElementById('projects-empty');
    if (row) row.remove();
    if (empty && root && root.children.length === 0) empty.style.display = '';
  }

  _appendProjectRow(p) {
    const root = document.getElementById('projects-list');
    if (!root) return;
    const div = document.createElement('div');
    div.id = `proj-row-${p.id}`;
    div.className = 'flex items-center justify-between p-3 bg-card border rounded';

    const left = document.createElement('div');
    left.className = 'text-sm';
    const title = document.createElement('div');
    title.className = 'font-medium';
    title.textContent = `${p.name || 'Proyecto'} · ${p.mode || ''}`;
    const meta = document.createElement('div');
    meta.className = 'text-xs text-muted-foreground';
    const m = this._computeProjectMeta(p);
    meta.textContent = `${m.pixels != null ? (m.pixels.toLocaleString() + ' px') : 'px n/d'} · ${m.sizeText}`;
    left.appendChild(title);
    left.appendChild(meta);

    const right = document.createElement('div');
    right.className = 'flex items-center gap-2';

    const loadBtn = document.createElement('button');
    loadBtn.className = 'px-2 py-1 text-xs bg-blue-600 hover:bg-blue-500 text-white rounded';
    loadBtn.textContent = 'Cargar';
    loadBtn.addEventListener('click', () => this._loadProject(p));

    const delBtn = document.createElement('button');
    delBtn.className = 'px-2 py-1 text-xs bg-red-600 hover:bg-red-500 text-white rounded';
    delBtn.textContent = 'Eliminar';
    delBtn.addEventListener('click', async () => {
      try {
        const res = await fetch(`${this.dashboard.apiBase()}/api/projects/${p.id}`, { method: 'DELETE' });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        this.dashboard.log(`🗑️ Proyecto eliminado: ${p.id}`);
        this.remove(p.id);
      } catch (e) {
        this.dashboard.log('❌ Error eliminando proyecto: ' + (e?.message || e));
      }
    });

    right.appendChild(loadBtn);
    right.appendChild(delBtn);

    div.appendChild(left);
    div.appendChild(right);
    root.appendChild(div);
  }

  _loadProject(proj) {
    try {
      // Determinar modo a partir del proyecto/config
      const m = (proj.mode || '').toLowerCase();
      const looksImage = !!(proj?.config?.imageData);
      this.dashboard.detectedBotMode = m.startsWith('guard') ? 'Guard' : (looksImage ? 'Image' : 'Guard');
      this.dashboard.projectConfig = proj.config || null;

      // Resetear estado de preview para evitar que se quede el anterior
      try {
        this.dashboard.previewManager.lastPreviewData = null;
        this.dashboard.previewManager.guardPreview = {
          analysis: null,
          togglesInitialized: false,
          show: { correct: true, incorrect: true, missing: true },
          area: null
        };
      } catch {}
      const detectedEl = document.getElementById('detected-mode');
      if (detectedEl) detectedEl.textContent = this.dashboard.detectedBotMode ? `Detected mode: ${this.dashboard.detectedBotMode}` : 'No file loaded - mode will be auto-detected';
      const statusEl = document.getElementById('file-status');
      if (statusEl) statusEl.textContent = `Loaded from server: ${proj.name || '(sin nombre)'}`;
      const panel = document.getElementById('preview-panel');
      if (panel) panel.style.display = 'block';

      // Render inmediato según modo, y en Guard además subir config al favorito
      if (this.dashboard.detectedBotMode === 'Image') {
        try { this.dashboard.previewManager.showPreviewFromProject?.(proj.config || {}); } catch {}
      } else {
        const cfg = proj.config || {};
        const hasGuardData = !!(cfg?.protectionData?.area || cfg?.protectionArea || Array.isArray(cfg?.originalPixels));
        if (hasGuardData) {
          try { this.dashboard.previewManager.showGuardPreviewFromProject?.(cfg); } catch {}
        }

        // Asegurar favorito y subir el guard al slave para que mande la preview real
        try {
          const favId = this.dashboard.slaveManager?.getFavoriteSlaveId?.();
          if (!favId) {
            try { this.dashboard.autoAssignFavoriteIfNeeded?.(); } catch {}
          }
        } catch {}

        try {
          const filename = proj.name || 'project_guard.json';
          fetch(`${this.dashboard.apiBase()}/api/guard/upload?persist=false`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ filename, data: cfg })
          }).then(r => r.json()).then(resp => {
            this.dashboard.log(`📤 Guard upload sent → fav=${resp.sent_to || 'n/a'} size=${resp.originalLength || 'n/a'}`);
            // Solicitar explícitamente refresh para acelerar la llegada de preview
            try { this.dashboard.previewManager.requestPreviewRefreshThrottle(); } catch {}
          }).catch(err => {
            this.dashboard.log('❌ Guard upload error: ' + (err?.message || err));
          });
        } catch {}
      }
      this.dashboard.updateControlButtons();
      this.dashboard.log(`📦 Proyecto cargado: ${proj.name || proj.id}`);
    } catch (e) {
      this.dashboard.log('⚠️ Error cargando proyecto: ' + (e?.message || e));
    }
  }

  _computeProjectMeta(p) {
    const cfg = p?.config || {};
    let pixels = null;
    const mode = (p?.mode || '').toLowerCase();
    if (mode.startsWith('guard')) {
      pixels = (cfg?.protectionData?.protectedPixels) ?? (Array.isArray(cfg?.originalPixels) ? cfg.originalPixels.length : null);
    } else if (mode.startsWith('image')) {
      if (cfg?.imageData?.width && cfg?.imageData?.height) pixels = cfg.imageData.width * cfg.imageData.height;
      else if (Array.isArray(cfg?.imageData?.fullPixelData)) pixels = cfg.imageData.fullPixelData.length;
    }
    let sizeBytes = 0;
    try {
      const enc = new TextEncoder();
      sizeBytes = enc.encode(JSON.stringify(cfg)).length;
    } catch { sizeBytes = (JSON.stringify(cfg) || '').length; }
    return { pixels, sizeBytes, sizeText: this._formatBytes(sizeBytes) };
  }

  _formatBytes(bytes) {
    try {
      if (bytes === 0) return '0 B';
      const k = 1024;
      const dm = 1;
      const sizes = ['B', 'KB', 'MB', 'GB'];
      const i = Math.floor(Math.log(bytes) / Math.log(k));
      return `${parseFloat((bytes / Math.pow(k, i)).toFixed(dm))} ${sizes[i]}`;
    } catch { return `${bytes} B`; }
  }
}
