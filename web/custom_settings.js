/**
 * ViralCut — Управление оптимизацией Whisper (пресеты и расширенные настройки)
 */
(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  const ESC_MAP = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ESC_MAP[c]);

  let currentSettingsPayload = null;
  let activePresetId = "auto";
  let fetchPromise = null;

  async function fetchSettings() {
    if (fetchPromise) return fetchPromise;
    fetchPromise = (async () => {
      try {
        const res = await fetch("/api/whisper/settings", { cache: "no-store" });
        if (!res.ok) return null;
        const data = await res.json();
        currentSettingsPayload = data;
        updateSidebarBadge(data);
        return data;
      } catch (err) {
        console.warn("[vcSettings] Не удалось получить настройки:", err);
        return null;
      } finally {
        fetchPromise = null;
      }
    })();
    return fetchPromise;
  }

  function updateSidebarBadge(data) {
    const badgeEl = $("#vcSettingsBadge");
    if (!badgeEl) return;
    if (!data || !data.effective) {
      badgeEl.textContent = "Настройки";
      return;
    }
    badgeEl.textContent = data.effective.badge || "Оптимизация";
  }

  function renderHardwareCard(hw) {
    if (!hw) return "";
    const cpuName = esc(hw.cpu_name || "CPU");
    const physical = hw.cpu_physical_cores || 4;
    const logical = hw.cpu_logical_cores || 8;
    const isCuda = Boolean(hw.cuda_ready);
    const gpuName = esc(hw.gpu_name || "NVIDIA GPU");
    const totalVram = hw.vram_total_mb ? `${(hw.vram_total_mb / 1024).toFixed(1)} ГБ` : "";
    const freeVram = hw.vram_free_mb ? `${(hw.vram_free_mb / 1024).toFixed(1)} ГБ` : "";

    return `
      <div class="settings-hw-card">
        <div class="settings-hw-item">
          <div class="settings-hw-icon">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <rect x="4" y="4" width="16" height="16" rx="2"></rect>
              <rect x="9" y="9" width="6" height="6"></rect>
              <path d="M9 1v3M15 1v3M9 20v3M15 20v3M20 9h3M20 14h3M1 9h3M1 14h3"></path>
            </svg>
          </div>
          <div class="settings-hw-info">
            <span class="label">Процессор (CPU)</span>
            <span class="val">${cpuName}</span>
            <span class="sub">${physical} физ. ядер · ${logical} потоков (AVX2/FMA)</span>
          </div>
        </div>

        <div class="settings-hw-item">
          <div class="settings-hw-icon ${isCuda ? "gpu" : ""}">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M6 19v-3M10 19v-3M14 19v-3M18 19v-3M2 11h20M2 7h20M2 15h20"></path>
            </svg>
          </div>
          <div class="settings-hw-info">
            <span class="label">Видеокарта (GPU)</span>
            ${
              isCuda
                ? `<span class="val">${gpuName}</span>
                   <span class="sub">VRAM: ${freeVram} свободно из ${totalVram} · CUDA 12</span>`
                : `<span class="val">GPU недоступен / не настроен</span>
                   <span class="sub">Используется процессор (рекомендуется INT8)</span>`
            }
          </div>
        </div>
      </div>
    `;
  }

  function setSelectValue(sel, val, fallback) {
    if (!sel) return;
    const raw = val !== undefined && val !== null && val !== "" ? String(val) : String(fallback);
    if (!Array.from(sel.options).some((o) => o.value === raw)) {
      const opt = document.createElement("option");
      opt.value = raw;
      opt.textContent = raw;
      sel.appendChild(opt);
    }
    sel.value = raw;
  }

  function renderPresetsGrid(presets, currentPreset) {
    if (!presets || !presets.length) return "";
    return `
      <div class="settings-presets-grid" id="vcPresetsGrid">
        ${presets
          .map((p) => {
            const isActive = p.id === currentPreset;
            const title = p.label || p.name || p.id || "";
            const desc = p.desc || p.description || "";
            return `
            <div class="preset-card ${isActive ? "active" : ""}" role="button" tabindex="0" aria-pressed="${isActive ? "true" : "false"}" data-preset-id="${esc(p.id)}">
              <div class="preset-card-top">
                <span class="preset-name">${esc(title)}</span>
                <span class="preset-badge">${esc(p.badge)}</span>
              </div>
              <div class="preset-desc">${esc(desc)}</div>
            </div>
          `;
          })
          .join("")}
      </div>
    `;
  }

  function fillFormFromPreset(presetId, presets) {
    const p = presets.find((x) => x.id === presetId);
    if (!p) return;
    const s = p.settings || {
      device: "auto",
      compute_type: "auto",
      cpu_threads: 0,
      beam_size: 2,
      batch_size: 16,
      recheck_limit: 4,
    };
    setSelectValue($("#vcOptDevice"), s.device, "auto");
    setSelectValue($("#vcOptCompute"), s.compute_type, "auto");
    setSelectValue($("#vcOptThreads"), s.cpu_threads, 0);
    setSelectValue($("#vcOptBeam"), s.beam_size, 2);
    setSelectValue($("#vcOptBatch"), s.batch_size, 16);
    setSelectValue($("#vcOptRecheck"), s.recheck_limit, 4);
  }

  function openSettingsModal() {
    if (typeof window.vcCloseAnyModal === "function") {
      window.vcCloseAnyModal();
    }
    if (!currentSettingsPayload) {
      fetchSettings().then((data) => {
        if (data) showModal(data);
        else if (typeof window.toast === "function") {
          window.toast("Не удалось загрузить настройки Whisper", "err");
        }
      });
      return;
    }
    showModal(currentSettingsPayload);
  }

  function showModal(data) {
    if (typeof window.vcCloseAnyModal === "function") {
      window.vcCloseAnyModal();
    }
    const modal = $("#modal");
    if (!modal) return;

    const s = data.settings || {};
    activePresetId = s.preset || "auto";

    const closeSvg =
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M18 6 6 18M6 6l12 12"/></svg>';

    modal.innerHTML = `
      <div class="modal-backdrop" data-close="settings"></div>
      <div class="modal-panel workbench vc-settings-panel" role="dialog" aria-modal="true" aria-labelledby="vcSettingsTitle">
        <div class="modal-head">
          <div style="display:flex; align-items:center; gap:10px;">
            <div class="side-settings-ico" style="width:28px;height:28px;">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <circle cx="12" cy="12" r="3"></circle>
                <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"></path>
              </svg>
            </div>
            <h3 id="vcSettingsTitle" style="margin:0; font-size:16px; font-weight:600;">Оптимизация и аппаратное ускорение Whisper</h3>
          </div>
          <button class="icon-btn" data-close="settings" type="button" title="Закрыть">${closeSvg}</button>
        </div>

        <div class="vc-settings-body">
          ${renderHardwareCard(data.hardware)}

          <div>
            <div class="settings-sec-title">
              <h4>Готовые профили ускорения</h4>
              <span class="tag">Один клик для настройки</span>
            </div>
            ${renderPresetsGrid(data.presets || [], activePresetId)}
          </div>

          <div>
            <div class="settings-sec-title">
              <h4>Детальная ручная настройка параметров</h4>
              <span class="tag" id="vcModeTag">Режим: ${activePresetId === "custom" ? "Пользовательский" : "Пресет"}</span>
            </div>

            <div class="settings-params-grid">
              <div class="setting-field">
                <div class="setting-label">
                  <span>Устройство (Device)</span>
                </div>
                <div class="setting-control">
                  <select id="vcOptDevice">
                    <option value="auto">Авто (GPU если доступна, иначе CPU)</option>
                    <option value="cuda">NVIDIA GPU (CUDA)</option>
                    <option value="cpu">Процессор (CPU)</option>
                  </select>
                </div>
                <div class="setting-hint">GPU ускоряет распознавание в 12–21 раз по сравнению с CPU.</div>
              </div>

              <div class="setting-field">
                <div class="setting-label">
                  <span>Точность вычислений (Compute Type)</span>
                </div>
                <div class="setting-control">
                  <select id="vcOptCompute">
                    <option value="auto">Авто (FP16 для GPU, INT8 для CPU)</option>
                    <option value="float16">float16 (Максимальная скорость GPU)</option>
                    <option value="int8_float16">int8_float16 (Экономия 1 ГБ VRAM)</option>
                    <option value="int8">int8 (Оптимально для CPU: -37% времени)</option>
                    <option value="float32">float32 (Полная точность FP32)</option>
                  </select>
                </div>
                <div class="setting-hint">FP16 раскрывает тензорные ядра RTX; INT8 снижает расход памяти в 2.5 раза.</div>
              </div>

              <div class="setting-field">
                <div class="setting-label">
                  <span>Потоки CPU (Thread Count)</span>
                </div>
                <div class="setting-control">
                  <select id="vcOptThreads">
                    <option value="0">Авто (${data.hardware?.cpu_physical_cores || 8} физ. ядер процессора)</option>
                    <option value="4">4 потока (Эко-режим, минимум нагрева)</option>
                    <option value="8">8 потоков</option>
                    <option value="12">12 потоков</option>
                    <option value="16">16 потоков (Все логические потоки)</option>
                  </select>
                </div>
                <div class="setting-hint">Использование физических ядер без гипертрейдинга устраняет троттлинг кэша.</div>
              </div>

              <div class="setting-field">
                <div class="setting-label">
                  <span>Размер луча (Beam Size)</span>
                </div>
                <div class="setting-control">
                  <select id="vcOptBeam">
                    <option value="1">Beam 1 (Жадный поиск — 143x RTF на GPU)</option>
                    <option value="2">Beam 2 (Рекомендуется: +59% ускорение без потери качества)</option>
                    <option value="5">Beam 5 (Максимальный перебор 5 гипотез)</option>
                  </select>
                </div>
                <div class="setting-hint">Beam 2 экономит до 1.5 минут на 12-минутном видео при 100% точности.</div>
              </div>

              <div class="setting-field">
                <div class="setting-label">
                  <span>Размер пачки (Batch Size)</span>
                </div>
                <div class="setting-control">
                  <select id="vcOptBatch">
                    <option value="4">4 сегмента</option>
                    <option value="8">8 сегментов</option>
                    <option value="16">16 сегментов (Оптимально для RTX 4060)</option>
                    <option value="32">32 сегмента (Максимальная параллелизация GPU)</option>
                  </select>
                </div>
                <div class="setting-hint">Пачечное декодирование BatchedPipeline ускоряет работу в 2.4–4.8 раза.</div>
              </div>

              <div class="setting-field">
                <div class="setting-label">
                  <span>Перепроверка сомнительных слов (Recheck)</span>
                </div>
                <div class="setting-control">
                  <select id="vcOptRecheck">
                    <option value="0">0 (Отключено — чистая скорость)</option>
                    <option value="4">4 фрагмента (Оптимальный баланс)</option>
                    <option value="8">8 фрагментов (Стандартная перепроверка)</option>
                    <option value="16">16 фрагментов (Глубокий анализ спорных мест)</option>
                  </select>
                </div>
                <div class="setting-hint">Повторно прогоняет участки с низким logprob в контексте соседей.</div>
              </div>
            </div>
          </div>
        </div>

        <div class="settings-foot">
          <button class="btn-reset-settings" id="vcBtnResetSettings" type="button">
            Сбросить на Авто
          </button>
          <button class="btn-save-settings" id="vcBtnSaveSettings" type="button">
            Сохранить и применить
          </button>
        </div>
      </div>
    `;

    modal.classList.remove("hidden");

    // Инициализация значений селектов
    const devSelect = $("#vcOptDevice", modal);
    const compSelect = $("#vcOptCompute", modal);
    const threadsSelect = $("#vcOptThreads", modal);
    const beamSelect = $("#vcOptBeam", modal);
    const batchSelect = $("#vcOptBatch", modal);
    const recheckSelect = $("#vcOptRecheck", modal);

    setSelectValue(devSelect, s.device, "auto");
    setSelectValue(compSelect, s.compute_type, "auto");
    setSelectValue(threadsSelect, s.cpu_threads, 0);
    setSelectValue(beamSelect, s.beam_size, 2);
    setSelectValue(batchSelect, s.batch_size, 16);
    setSelectValue(recheckSelect, s.recheck_limit, 4);

    // Клик или клавиша по карточке пресета
    const cards = $$(".preset-card", modal);
    const selectPresetCard = (card) => {
      const pid = card.getAttribute("data-preset-id");
      activePresetId = pid;
      cards.forEach((c) => {
        const match = c === card;
        c.classList.toggle("active", match);
        c.setAttribute("aria-pressed", match ? "true" : "false");
      });
      fillFormFromPreset(pid, data.presets || []);
      const modeTag = $("#vcModeTag", modal);
      if (modeTag) modeTag.textContent = "Режим: Пресет";
    };
    cards.forEach((card) => {
      card.addEventListener("click", () => selectPresetCard(card));
      card.addEventListener("keydown", (e) => {
        if (e.key !== "Enter" && e.key !== " ") return;
        e.preventDefault();
        selectPresetCard(card);
      });
    });

    // Изменение любого селекта переключает на "Свой"
    const onParamChange = () => {
      activePresetId = "custom";
      cards.forEach((c) => {
        c.classList.remove("active");
        c.setAttribute("aria-pressed", "false");
      });
      const modeTag = $("#vcModeTag", modal);
      if (modeTag) modeTag.textContent = "Режим: Пользовательский";
    };

    [devSelect, compSelect, threadsSelect, beamSelect, batchSelect, recheckSelect].forEach((el) => {
      if (el) el.addEventListener("change", onParamChange);
    });

    // Кнопка сброса
    const resetBtn = $("#vcBtnResetSettings", modal);
    if (resetBtn) {
      resetBtn.addEventListener("click", () => {
        activePresetId = "auto";
        cards.forEach((c) => {
          const match = c.getAttribute("data-preset-id") === "auto";
          c.classList.toggle("active", match);
          c.setAttribute("aria-pressed", match ? "true" : "false");
        });
        fillFormFromPreset("auto", data.presets || []);
        const modeTag = $("#vcModeTag", modal);
        if (modeTag) modeTag.textContent = "Режим: Пресет";
      });
    }

    // Кнопка сохранения
    const saveBtn = $("#vcBtnSaveSettings", modal);
    if (saveBtn) {
      const safeInt = (sel, def) => {
        const n = sel ? parseInt(sel.value, 10) : NaN;
        return Number.isFinite(n) ? n : def;
      };
      saveBtn.addEventListener("click", async () => {
        saveBtn.disabled = true;
        saveBtn.textContent = "Применение…";
        try {
          const payload = {
            preset: activePresetId,
            device: devSelect ? devSelect.value : "auto",
            compute_type: compSelect ? compSelect.value : "auto",
            cpu_threads: safeInt(threadsSelect, 0),
            beam_size: safeInt(beamSelect, 2),
            batch_size: safeInt(batchSelect, 16),
            recheck_limit: safeInt(recheckSelect, 4),
          };
          const res = await fetch("/api/whisper/settings", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
          });
          const updated = await res.json();
          if (!res.ok) throw new Error(updated.detail || "Ошибка сохранения");
          currentSettingsPayload = updated;
          updateSidebarBadge(updated);
          closeModal();
          if (typeof window.toast === "function") {
            window.toast("Параметры оптимизации Whisper сохранены");
          }
          if (typeof window.vcCheckHealth === "function") {
            window.vcCheckHealth();
          }
        } catch (err) {
          if (typeof window.toast === "function") {
            window.toast(err.message || "Не удалось сохранить настройки", "err");
          }
          saveBtn.disabled = false;
          saveBtn.textContent = "Сохранить и применить";
        }
      });
    }
  }

  function closeModal() {
    const modal = $("#modal");
    if (!modal) return;
    modal.classList.add("hidden");
    modal.innerHTML = "";
  }

  function init() {
    // Получение настроек и установка бейджа в сайдбаре
    fetchSettings();

    // Слушатель на кнопку в сайдбаре
    const btn = $("#vcSettingsBtn");
    if (btn) {
      btn.addEventListener("click", (e) => {
        e.preventDefault();
        openSettingsModal();
      });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  window.vcSettings = {
    open: openSettingsModal,
    close: closeModal,
    refresh: fetchSettings,
  };
})();
