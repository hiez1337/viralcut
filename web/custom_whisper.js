(() => {
  "use strict";

  const ESC_MAP = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (ch) => ESC_MAP[ch]);

  const WHISPER_ICON =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
    '<rect x="9" y="3" width="6" height="11" rx="3"/>' +
    '<path d="M5 11a7 7 0 0 0 14 0M12 18v3M8 21h8"/></svg>';

  let slotEl = null;
  let stateData = null;
  let pollTimer = null;
  let inFlight = false;
  let prevDownloading = null;
  let visBound = false;

  function fmtSizeMb(mb) {
    const val = Number(mb) || 0;
    if (val >= 1024) return `${(val / 1024).toFixed(2)} ГБ`;
    return `${Math.round(val)} МБ`;
  }

  function fmtEta(sec) {
    if (sec === null || sec === undefined) return "";
    const s = Math.max(1, Math.round(Number(sec) || 0));
    if (s < 60) return `~${s} с`;
    const m = Math.floor(s / 60);
    const r = s % 60;
    return `~${m}м ${String(r).padStart(2, "0")}с`;
  }

  function stopPoll() {
    if (pollTimer) {
      clearTimeout(pollTimer);
      pollTimer = null;
    }
  }

  function isMounted() {
    return !!(slotEl && document.contains(slotEl));
  }

  function schedulePoll(delayMs = 650) {
    stopPoll();
    if (!isMounted()) return;
    if (document.visibilityState !== "visible") return;
    if (!stateData || !stateData.downloading_model) return;
    pollTimer = setTimeout(() => fetchModels(), delayMs);
  }

  async function fetchModels() {
    if (inFlight || !isMounted()) return;
    inFlight = true;
    try {
      const resp = await fetch("/api/whisper/models", { cache: "no-store" });
      if (!resp.ok) return;
      const data = await resp.json();
      const finishedModel =
        prevDownloading && !data.downloading_model
          ? (data.models || []).find((m) => m.id === prevDownloading)
          : null;
      prevDownloading = data.downloading_model || null;
      stateData = data;
      render();
      if (finishedModel && finishedModel.status === "ready") {
        if (typeof window.toast === "function") {
          window.toast(`Модель Whisper ${finishedModel.label} скачана и готова`);
        }
        if (typeof window.vcCheckHealth === "function") window.vcCheckHealth();
      } else if (finishedModel && finishedModel.status === "error" && finishedModel.error) {
        if (typeof window.toast === "function") {
          window.toast(`Не удалось скачать ${finishedModel.label}: ${finishedModel.error}`, "err");
        }
      }
      if (data.downloading_model) schedulePoll(650);
    } catch (_) {
      if (stateData && stateData.downloading_model) schedulePoll(1500);
    } finally {
      inFlight = false;
    }
  }

  async function selectModel(modelId) {
    if (!modelId) return;
    if (stateData && stateData.active === modelId) return;
    try {
      const resp = await fetch("/api/whisper/model", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model: modelId }),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(data.detail || `HTTP ${resp.status}`);
      stateData = data;
      prevDownloading = data.downloading_model || null;
      render();
      if (typeof window.vcCheckHealth === "function") window.vcCheckHealth();
      if (data.downloading_model) schedulePoll(650);
    } catch (err) {
      if (typeof window.toast === "function") {
        window.toast(`Не удалось выбрать модель: ${err.message}`, "err");
      }
    }
  }

  async function downloadModel(modelId) {
    if (!modelId) return;
    try {
      const resp = await fetch("/api/whisper/download", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model: modelId }),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(data.detail || `HTTP ${resp.status}`);
      stateData = data;
      prevDownloading = data.downloading_model || modelId;
      render();
      if (data.downloading_model) {
        schedulePoll(350);
      } else if (typeof window.vcCheckHealth === "function") {
        window.vcCheckHealth();
      }
    } catch (err) {
      if (typeof window.toast === "function") {
        window.toast(`Ошибка загрузки модели: ${err.message}`, "err");
      }
    }
  }

  function render() {
    if (!isMounted()) return;
    if (!stateData) {
      slotEl.innerHTML = `
        <div class="vc-wm-wrap">
          <div class="vc-wm-head">
            <div class="vc-wm-head-left">
              <span class="opt-ico vc-wm-ico">${WHISPER_ICON}</span>
              <span class="opt-label">Модель речи (Whisper)</span>
            </div>
            <span class="vc-wm-dev-chip">Загрузка списка…</span>
          </div>
        </div>`;
      return;
    }

    const models = stateData.models || [];
    const rt = stateData.runtime || {};
    const anyDl = stateData.downloading_model;
    const activeObj = models.find((m) => m.id === stateData.active) || models[0];
    const dlObj = anyDl ? models.find((m) => m.id === anyDl) : null;

    const chipCls = rt.cuda_ready ? "ok" : rt.reason ? "warn" : "";
    const chipText = rt.device_label || "CPU";

    const cardsHtml = models
      .map((m) => {
        const isAct = m.id === stateData.active;
        const pct = Math.max(0, Math.min(100, Math.round(Number(m.pct) || 0)));
        let statusHtml = "";
        let actionHtml = "";

        if (m.status === "ready") {
          statusHtml = `<span class="vc-wm-status ready">✓ Готова · ${esc(fmtSizeMb(m.cache_mb || m.size_mb))}</span>`;
        } else if (m.status === "downloading") {
          statusHtml = `<span class="vc-wm-status downloading">Загрузка ${pct}%</span>`;
        } else if (m.status === "error") {
          statusHtml = `<span class="vc-wm-status error" title="${esc(m.error || "")}">Ошибка загрузки</span>`;
          actionHtml = `<button type="button" class="vc-wm-dl-btn err" data-dl="${esc(m.id)}" ${anyDl ? "disabled" : ""}>Повторить</button>`;
        } else {
          statusHtml = `<span class="vc-wm-status">↓ ~${esc(fmtSizeMb(m.size_mb))}</span>`;
          actionHtml = `<button type="button" class="vc-wm-dl-btn" data-dl="${esc(m.id)}" ${anyDl ? "disabled" : ""}>Скачать</button>`;
        }

        return `
          <div class="vc-wm-card ${isAct ? "active" : ""}" role="button" tabindex="0" aria-pressed="${isAct ? "true" : "false"}" data-model="${esc(m.id)}">
            <div class="vc-wm-card-top">
              <span class="vc-wm-title">${esc(m.label)}</span>
              <span class="vc-wm-badge">${esc(m.badge)}</span>
            </div>
            <div class="vc-wm-specs">
              <span>~${esc(fmtSizeMb(m.size_mb))}</span>
              <span>·</span>
              <span>${esc(m.speed_hint)}</span>
              <span>·</span>
              <span>${esc(m.vram_hint)}</span>
            </div>
            <div class="vc-wm-desc">${esc(m.quality_hint)}</div>
            ${m.status === "downloading" ? `<div class="vc-wm-mini-bar"><i style="width:${Math.max(2, pct)}%"></i></div>` : ""}
            <div class="vc-wm-card-foot">
              ${statusHtml}
              ${actionHtml}
            </div>
          </div>`;
      })
      .join("");

    let dlBoxHtml = "";
    if (dlObj) {
      const pct = Math.max(0, Math.min(100, Math.round(Number(dlObj.pct) || 0)));
      const speedTxt = dlObj.speed_mbps > 0 ? ` · ${dlObj.speed_mbps} МБ/с` : "";
      const etaTxt = dlObj.eta_s ? ` · ${fmtEta(dlObj.eta_s)}` : "";
      dlBoxHtml = `
        <div class="vc-wm-dl-box">
          <div class="vc-wm-dl-row">
            <strong>Скачиваю Whisper ${esc(dlObj.label)}… ${pct}%</strong>
            <span class="vc-wm-dl-meta">${esc(fmtSizeMb(dlObj.downloaded_mb))} / ${esc(fmtSizeMb(dlObj.total_mb))}${esc(speedTxt)}${esc(etaTxt)}</span>
          </div>
          <div class="vc-wm-dl-bar"><i style="width:${Math.max(2, pct)}%"></i></div>
        </div>`;
    }

    let hintHtml = "";
    if (activeObj) {
      if (activeObj.status === "ready") {
        hintHtml = `Выбрана <b>${esc(activeObj.label)}</b> — модель уже на диске, распознавание начнётся сразу.`;
      } else if (activeObj.status === "downloading") {
        hintHtml = `Выбрана <b>${esc(activeObj.label)}</b> — идёт скачивание весов; можно дождаться завершения или запустить проект.`;
      } else {
        hintHtml = `Выбрана <b>${esc(activeObj.label)}</b> (~${esc(fmtSizeMb(activeObj.size_mb))}) — ещё не скачана. Нажмите «Скачать» заранее или она загрузится автоматически при первом видео.`;
      }
    }

    const runtimeNoteHtml = rt.reason
      ? `<div class="vc-wm-runtime-note">${esc(rt.reason)}</div>`
      : "";

    slotEl.innerHTML = `
      <div class="vc-wm-wrap">
        <div class="vc-wm-head">
          <div class="vc-wm-head-left">
            <span class="opt-ico vc-wm-ico">${WHISPER_ICON}</span>
            <div>
              <span class="opt-label">Модель речи (Whisper)</span>
              <span class="vc-wm-sub">точность и размер заранее</span>
            </div>
          </div>
          <span class="vc-wm-dev-chip ${chipCls}" title="${esc(rt.reason || chipText)}">${esc(chipText)}</span>
        </div>
        <div class="vc-wm-grid">${cardsHtml}</div>
        ${dlBoxHtml}
        ${hintHtml ? `<div class="vc-wm-hint">${hintHtml}</div>` : ""}
        ${runtimeNoteHtml}
      </div>`;
  }

  function bindSlotEvents(el) {
    if (el.dataset.vcWhisperBound) return;
    el.dataset.vcWhisperBound = "1";

    el.addEventListener("click", (ev) => {
      const dlBtn = ev.target.closest("[data-dl]");
      if (dlBtn) {
        ev.stopPropagation();
        if (!dlBtn.disabled) downloadModel(dlBtn.dataset.dl);
        return;
      }
      const card = ev.target.closest(".vc-wm-card[data-model]");
      if (card) selectModel(card.dataset.model);
    });

    el.addEventListener("keydown", (ev) => {
      if (ev.key !== "Enter" && ev.key !== " ") return;
      if (ev.target.closest("[data-dl]")) return;
      const card = ev.target.closest(".vc-wm-card[data-model]");
      if (!card) return;
      ev.preventDefault();
      selectModel(card.dataset.model);
    });
  }

  function mount(selector = "#vcWhisperSlot") {
    const el = typeof selector === "string" ? document.querySelector(selector) : selector;
    if (!el) return;
    slotEl = el;
    bindSlotEvents(el);
    render();
    fetchModels();

    if (!visBound) {
      visBound = true;
      document.addEventListener("visibilitychange", () => {
        if (document.visibilityState === "visible" && isMounted()) {
          fetchModels();
        } else {
          stopPoll();
        }
      });
    }
  }

  function getSelected() {
    return stateData && stateData.active ? stateData.active : undefined;
  }

  window.vcWhisper = {
    mount,
    refresh: fetchModels,
    getSelected,
  };
})();
