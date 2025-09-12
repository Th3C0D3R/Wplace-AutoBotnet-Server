// Componente de Preview para Guard Analysis
// Separado del index.astro para mejor mantenibilidad

export class GuardPreview {
  constructor() {
    this.guardPreview = { 
      analysis: null, 
      togglesInitialized: false, 
      show: { correct: true, incorrect: true, missing: true }, 
      area: null 
    };
  }

  log(message) {
    const timestamp = new Date().toLocaleTimeString();
    console.log(`[${timestamp}] ${message}`);
  }

  // Crear toggle CSS personalizado estilo iOS
  createToggle(id, labelText, checked = false) {
    return `
      <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; padding: 4px 0;">
        <span style="color: #e5e7eb; font-size: 13px; flex: 1;">${labelText}</span>
        <label class="toggle-switch" style="position: relative; display: inline-block; width: 44px; height: 24px; margin-left: 10px;">
          <input type="checkbox" id="${id}" ${checked ? 'checked' : ''} style="opacity: 0; width: 0; height: 0;">
          <span class="toggle-slider" style="
            position: absolute; cursor: pointer; top: 0; left: 0; right: 0; bottom: 0;
            background-color: ${checked ? '#22c55e' : '#ef4444'};
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
            border-radius: 12px; border: 1px solid ${checked ? '#16a34a' : '#dc2626'};
          "></span>
          <span class="toggle-knob" style="
            position: absolute; height: 18px; width: 18px;
            left: ${checked ? '23px' : '3px'}; top: 2px;
            background-color: white; transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
            border-radius: 50%; box-shadow: 0 2px 4px rgba(0,0,0,0.3);
          "></span>
        </label>
      </div>
    `;
  }

  // Actualizar el estado visual del toggle
  updateToggleState(toggleId, checked) {
    const toggle = document.getElementById(toggleId);
    if (!toggle) return;
    
    const slider = toggle.parentElement.querySelector('.toggle-slider');
    const knob = toggle.parentElement.querySelector('.toggle-knob');
    
    if (slider && knob) {
      slider.style.backgroundColor = checked ? '#22c55e' : '#ef4444';
      slider.style.borderColor = checked ? '#16a34a' : '#dc2626';
      knob.style.left = checked ? '23px' : '3px';
    }
  }

  // Inicializar controles de toggle para guard preview
  initGuardPreviewToggles() {
    if (this.guardPreview.togglesInitialized) return;
    
    const controlsContainer = document.getElementById('guard-preview-controls');
    if (!controlsContainer) return;
    
    controlsContainer.innerHTML = `
      <div style="background: #1f2937; border-radius: 8px; padding: 12px; margin-bottom: 12px;">
        <div style="color: #f3f4f6; font-size: 14px; font-weight: 600; margin-bottom: 10px;">🎛️ Controles de Visualización</div>
        ${this.createToggle('gp-show-correct', '✅ Mostrar Correctos', this.guardPreview.show.correct)}
        ${this.createToggle('gp-show-incorrect', '❌ Mostrar Incorrectos', this.guardPreview.show.incorrect)}
        ${this.createToggle('gp-show-missing', '⚪ Mostrar Faltantes', this.guardPreview.show.missing)}
      </div>
    `;
    
    this.setupGuardPreviewControls();
    this.guardPreview.togglesInitialized = true;
  }

  // Configurar eventos de los controles
  setupGuardPreviewControls() {
    ['gp-show-correct', 'gp-show-incorrect', 'gp-show-missing'].forEach(id => {
      const toggle = document.getElementById(id);
      if (!toggle) return;
      
      const updateShow = () => {
        const checked = toggle.checked;
        this.updateToggleState(id, checked);
        
        const key = id.replace('gp-show-', '');
        this.guardPreview.show[key] = checked;
        
        // Renderizar canvas si hay datos disponibles
        if (this.guardPreview.analysis && this.guardPreview.area) {
          this.renderGuardPreviewCanvas({
            analysis: this.guardPreview.analysis, 
            area: this.guardPreview.area
          });
        }
      };
      
      toggle.addEventListener('change', updateShow);
      toggle.addEventListener('click', (e) => {
        setTimeout(updateShow, 10);
      });
    });
  }

  // Convertir datos de arrays a Maps para compatibilidad con analysis-window.js
  convertPreviewDataToMaps(previewData) {
    const analysis = {
      originalPixels: new Map(),
      correct: new Map(), 
      incorrect: new Map(),
      missing: new Map()
    };

    try {
      // Convertir originalPixels
      if (Array.isArray(previewData.originalPixels)) {
        previewData.originalPixels.forEach(pixel => {
          const key = `${pixel.x},${pixel.y}`;
          analysis.originalPixels.set(key, { r: pixel.r, g: pixel.g, b: pixel.b });
        });
      }

      // Convertir correctos
      if (Array.isArray(previewData.correctPixelsList)) {
        previewData.correctPixelsList.forEach(pixel => {
          const key = `${pixel.x},${pixel.y}`;
          analysis.correct.set(key, { r: pixel.r, g: pixel.g, b: pixel.b });
        });
      }

      // Convertir incorrectos
      if (Array.isArray(previewData.incorrectPixelsList)) {
        previewData.incorrectPixelsList.forEach(pixel => {
          const key = `${pixel.x},${pixel.y}`;
          analysis.incorrect.set(key, { 
            r: pixel.r, g: pixel.g, b: pixel.b,
            originalR: pixel.originalR, originalG: pixel.originalG, originalB: pixel.originalB
          });
        });
      }

      // Convertir faltantes
      if (Array.isArray(previewData.missingPixelsList)) {
        previewData.missingPixelsList.forEach(pixel => {
          const key = `${pixel.x},${pixel.y}`;
          analysis.missing.set(key, { r: pixel.r, g: pixel.g, b: pixel.b });
        });
      }

    } catch (err) {
      this.log(`⚠️ Error converting preview data: ${err.message}`);
    }

    return analysis;
  }

  // Procesar preview data del slave
  processPreviewData(previewData) {
    if (!previewData || !previewData.protectedArea) {
      this.log('⚠️ Invalid preview data received');
      return;
    }

    // Convertir datos de arrays a Maps
    const analysis = this.convertPreviewDataToMaps(previewData);
    const area = previewData.protectedArea;

    this.log(`🔄 Processing preview: Original:${analysis.originalPixels.size}, Correct:${analysis.correct.size}, Incorrect:${analysis.incorrect.size}, Missing:${analysis.missing.size}`);

    // Almacenar para uso posterior
    this.guardPreview.analysis = analysis;
    this.guardPreview.area = area;

    // Renderizar
    this.renderGuardPreviewCanvas({ analysis, area });
  }

  // Renderizar canvas con datos de análisis
  renderGuardPreviewCanvas(payload) {
    try {
      if (!payload || !payload.analysis || !payload.area) return;
      
      this.initGuardPreviewToggles();
      const panel = document.getElementById('preview-panel');
      if (panel) panel.style.display = 'block';
      
      const info = document.getElementById('project-info');
      const area = payload.area;
      
      // Calcular totales reales
      const correctCount = payload.analysis.correct?.size || 0;
      const incorrectCount = payload.analysis.incorrect?.size || 0;
      const missingCount = payload.analysis.missing?.size || 0;
      const originalCount = payload.analysis.originalPixels?.size || 0;
      const total = Math.max(originalCount, correctCount + incorrectCount + missingCount);
      
      if (info) {
        info.innerHTML = `
          <div>Mode: Guard</div>
          <div>Area: (${area.x1},${area.y1})→(${area.x2},${area.y2})</div>
          <div>Correctos: ${correctCount}</div>
          <div>Incorrectos: ${incorrectCount}</div>
          <div>Faltantes: ${missingCount}</div>
          <div>Total: ${total}</div>
        `;
      }
      
      const canvas = document.getElementById('preview-canvas');
      if (!canvas) return;
      
      const width = area.x2 - area.x1 + 1;
      const height = area.y2 - area.y1 + 1;
      
      this.log(`🎨 Rendering ${total} pixels in ${width}x${height} canvas (Correct:${correctCount}, Incorrect:${incorrectCount}, Missing:${missingCount})`);
      
      // Configurar canvas
      canvas.width = width;
      canvas.height = height;
      
      // Calcular escala de visualización
      const canvasContainer = canvas.parentElement;
      if (canvasContainer) {
        const containerRect = canvasContainer.getBoundingClientRect();
        const availableWidth = containerRect.width - 20;
        const availableHeight = containerRect.height - 20;
        
        const scaleX = availableWidth / width;
        const scaleY = availableHeight / height;
        const scale = Math.min(scaleX, scaleY, 3);
        
        canvas.style.width = `${width * scale}px`;
        canvas.style.height = `${height * scale}px`;
      }
      
      const ctx = canvas.getContext('2d');
      if (!ctx) return;
      
      const imageData = ctx.createImageData(width, height);
      
      // Fondo gris claro
      for (let i = 0; i < imageData.data.length; i += 4) {
        imageData.data[i] = 240;     // R
        imageData.data[i + 1] = 240; // G
        imageData.data[i + 2] = 240; // B
        imageData.data[i + 3] = 60;  // Alpha bajo
      }
      
      // Obtener estado de los toggles
      const showCorrectEl = document.getElementById('gp-show-correct');
      const showIncorrectEl = document.getElementById('gp-show-incorrect');
      const showMissingEl = document.getElementById('gp-show-missing');
      
      const showCorrect = showCorrectEl ? showCorrectEl.checked : this.guardPreview.show.correct;
      const showIncorrect = showIncorrectEl ? showIncorrectEl.checked : this.guardPreview.show.incorrect;
      const showMissing = showMissingEl ? showMissingEl.checked : this.guardPreview.show.missing;
      
      this.log(`🎛️ Display states - Correct: ${showCorrect}, Incorrect: ${showIncorrect}, Missing: ${showMissing}`);
      
      // Función para dibujar píxel
      const drawPixel = (x, y, r, g, b, a = 255) => {
        const index = ((y - area.y1) * width + (x - area.x1)) * 4;
        if (index >= 0 && index < imageData.data.length - 3) {
          imageData.data[index] = r;
          imageData.data[index + 1] = g;
          imageData.data[index + 2] = b;
          imageData.data[index + 3] = a;
        }
      };
      
      // Dibujar píxeles originales como fondo
      if (payload.analysis.originalPixels) {
        for (const [key, pixel] of payload.analysis.originalPixels) {
          const [x, y] = key.split(',').map(Number);
          drawPixel(x, y, pixel.r || 128, pixel.g || 128, pixel.b || 128, 255);
        }
      }
      
      // Dibujar píxeles correctos (verde) si está habilitado
      if (showCorrect && payload.analysis.correct) {
        for (const [key, _data] of payload.analysis.correct) {
          const [x, y] = key.split(',').map(Number);
          drawPixel(x, y, 0, 255, 0, 180);
        }
      }
      
      // Dibujar píxeles incorrectos (rojo) si está habilitado
      if (showIncorrect && payload.analysis.incorrect) {
        for (const [key, _data] of payload.analysis.incorrect) {
          const [x, y] = key.split(',').map(Number);
          drawPixel(x, y, 255, 0, 0, 220);
        }
      }
      
      // Dibujar píxeles faltantes (amarillo) si está habilitado
      if (showMissing && payload.analysis.missing) {
        for (const [key, _pixel] of payload.analysis.missing) {
          const [x, y] = key.split(',').map(Number);
          drawPixel(x, y, 255, 255, 0, 200);
        }
      }
      
      // CRÍTICO: Dibujar en el canvas
      ctx.putImageData(imageData, 0, 0);
      this.log(`🎨 Canvas rendered successfully with ${correctCount + incorrectCount + missingCount} visible pixels`);
      
      // Actualizar estadísticas
      this.updateGuardStatistics(payload.analysis);
      
      // Estadísticas del canvas
      let stats = document.getElementById('guard-preview-stats');
      if (!stats) {
        stats = document.createElement('div');
        stats.id = 'guard-preview-stats';
        canvas.parentElement.appendChild(stats);
      }
      
      const c = correctCount, i = incorrectCount, m = missingCount, tot = total;
      const acc = tot ? ((c / tot) * 100).toFixed(1) : '0.0';
      
      stats.innerHTML = `
        <div style="margin-top:8px;padding:8px;background:#374151;border-radius:6px;font-size:12px;color:#e5e7eb;">
          <div style="display:flex;justify-content:space-between;margin-bottom:4px;">
            <span style="color:#10b981;">✅ Correctos:</span><strong>${c}</strong>
          </div>
          <div style="display:flex;justify-content:space-between;margin-bottom:4px;">
            <span style="color:#ef4444;">❌ Incorrectos:</span><strong>${i}</strong>
          </div>
          <div style="display:flex;justify-content:space-between;margin-bottom:4px;">
            <span style="color:#f59e0b;">⚪ Faltantes:</span><strong>${m}</strong>
          </div>
          <div style="display:flex;justify-content:space-between;border-top:1px solid #4b5563;padding-top:4px;margin-top:4px;">
            <span style="color:#8b5cf6;">🎯 Precisión:</span><strong>${acc}%</strong>
          </div>
          <div style="font-size:10px;color:#9ca3af;text-align:center;margin-top:4px;">
            Canvas: ${width}x${height} | Total: ${tot}
          </div>
        </div>
      `;
      
    } catch (err) {
      this.log('⚠️ renderGuardPreviewCanvas error: ' + err.message);
      console.error('Canvas render error:', err, payload);
    }
  }

  // Actualizar estadísticas en el panel principal
  updateGuardStatistics(analysis) {
    const correctCount = analysis.correct?.size || 0;
    const incorrectCount = analysis.incorrect?.size || 0;
    const missingCount = analysis.missing?.size || 0;
    const totalCount = analysis.originalPixels?.size || (correctCount + incorrectCount + missingCount);
    const accuracy = totalCount > 0 ? ((correctCount / totalCount) * 100).toFixed(1) : '0.0';
    
    this.log(`📊 Updating statistics: C:${correctCount}, I:${incorrectCount}, M:${missingCount}, T:${totalCount}, A:${accuracy}%`);
    
    // Actualizar panel de telemetría
    const rpEl = document.getElementById('repaired-pixels');
    const mpEl = document.getElementById('missing-pixels');
    const apEl = document.getElementById('absent-pixels');
    
    if (rpEl) {
      rpEl.textContent = String(correctCount);
      this.log(`✅ Updated correctos: ${correctCount}`);
    }
    if (mpEl) {
      mpEl.textContent = String(incorrectCount);
      this.log(`❌ Updated incorrectos: ${incorrectCount}`);
    }
    if (apEl) {
      apEl.textContent = String(missingCount);
      this.log(`⚪ Updated faltantes: ${missingCount}`);
    }
    
    // Agregar panel de precisión si no existe
    let accuracyEl = document.getElementById('guard-accuracy');
    if (!accuracyEl) {
      const panel = document.querySelector('.grid.grid-cols-1.sm\\:grid-cols-4.gap-4');
      if (panel && panel.children.length >= 3) {
        const accuracyCard = document.createElement('div');
        accuracyCard.className = 'bg-card text-card-foreground rounded-lg border p-4';
        accuracyCard.innerHTML = `
          <div class="text-sm font-medium text-muted-foreground">🎯 Precisión</div>
          <div id="guard-accuracy" class="text-2xl font-bold">-</div>
        `;
        panel.appendChild(accuracyCard);
        accuracyEl = document.getElementById('guard-accuracy');
        this.log(`🎯 Created accuracy panel`);
      }
    }
    if (accuracyEl) {
      accuracyEl.textContent = `${accuracy}%`;
      this.log(`🎯 Updated accuracy: ${accuracy}%`);
    }
  }
}

// Exportar instancia singleton
export const guardPreviewInstance = new GuardPreview();
