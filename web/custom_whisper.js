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
  let actionBusy = false;
  let reqSeq = 0;
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

  function patchDownloadProgress(data) {
    if (!isMounted() || !data || !data.downloading_model) return false;
    const dlBox = slotEl.querySelector("#vcWmDlBox");
    if (!dlBox) return false;
    if (
      slotEl.dataset.dlModel !== (data.downloading_model || "") ||
      slotEl.dataset.activeModel !== (data.active || "") ||
      slotEl.dataset.activeLang !== (data.active_language || "auto")
    ) {
      return false;
    }
    const dlObj = (data.models || []).find((m) => m.id === data.downloading_model);
    if (!dlObj) return false;
    const pct = Math.max(0, Math.min(100, Math.round(Number(dlObj.pct) || 0)));
    const speedTxt = dlObj.speed_mbps > 0 ? ` · ${dlObj.speed_mbps} МБ/с` : "";
    const etaTxt = dlObj.eta_s ? ` · ${fmtEta(dlObj.eta_s)}` : "";
    const titleEl = dlBox.querySelector("strong");
    const metaEl = dlBox.querySelector(".vc-wm-dl-meta");
    const barEl = dlBox.querySelector(".vc-wm-dl-bar i");
    if (titleEl) titleEl.textContent = `Скачиваю Whisper ${dlObj.label}… ${pct}%`;
    if (metaEl) metaEl.textContent = `${fmtSizeMb(dlObj.downloaded_mb)} / ${fmtSizeMb(dlObj.total_mb)}${speedTxt}${etaTxt}`;
    if (barEl) barEl.style.width = `${Math.max(2, pct)}%`;

    for (const card of slotEl.querySelectorAll(".vc-wm-card[data-model]")) {
      if (card.dataset.model !== dlObj.id) continue;
      const stEl = card.querySelector(".vc-wm-status");
      const miniEl = card.querySelector(".vc-wm-mini-bar i");
      if (!stEl || !miniEl) return false;
      stEl.textContent = `Загрузка ${pct}%`;
      miniEl.style.width = `${Math.max(2, pct)}%`;
      break;
    }
    return true;
  }

  async function fetchModels() {
    if (inFlight || !isMounted()) return;
    inFlight = true;
    const seq = ++reqSeq;
    try {
      const resp = await fetch("/api/whisper/models", { cache: "no-store" });
      if (!resp.ok || seq !== reqSeq || !isMounted()) return;
      const data = await resp.json();
      if (seq !== reqSeq || !isMounted()) return;
      const finishedModel =
        prevDownloading && !data.downloading_model
          ? (data.models || []).find((m) => m.id === prevDownloading)
          : null;
      prevDownloading = data.downloading_model || null;
      stateData = data;
      if (!patchDownloadProgress(data)) {
        render();
      }
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
    if (!modelId || actionBusy) return;
    if (stateData && stateData.active === modelId) return;
    actionBusy = true;
    const seq = ++reqSeq;
    try {
      const resp = await fetch("/api/whisper/model", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model: modelId }),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(data.detail || `HTTP ${resp.status}`);
      if (seq > reqSeq) return;
      stateData = data;
      prevDownloading = data.downloading_model || null;
      if (isMounted()) render();
      if (typeof window.vcCheckHealth === "function") window.vcCheckHealth();
      if (data.downloading_model) schedulePoll(650);
    } catch (err) {
      if (typeof window.toast === "function") {
        window.toast(`Не удалось выбрать модель: ${err.message}`, "err");
      }
    } finally {
      actionBusy = false;
    }
  }

  async function downloadModel(modelId) {
    if (!modelId || actionBusy) return;
    actionBusy = true;
    const seq = ++reqSeq;
    try {
      const resp = await fetch("/api/whisper/download", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model: modelId }),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(data.detail || `HTTP ${resp.status}`);
      if (seq > reqSeq) return;
      stateData = data;
      prevDownloading = data.downloading_model || modelId;
      if (isMounted()) render();
      if (data.downloading_model) {
        schedulePoll(350);
      } else if (typeof window.vcCheckHealth === "function") {
        window.vcCheckHealth();
      }
    } catch (err) {
      if (typeof window.toast === "function") {
        window.toast(`Ошибка загрузки модели: ${err.message}`, "err");
      }
    } finally {
      actionBusy = false;
    }
  }

  async function selectLanguage(langId) {
    if (!langId || actionBusy) return;
    if (stateData && stateData.active_language === langId) return;
    actionBusy = true;
    const seq = ++reqSeq;
    try {
      const resp = await fetch("/api/whisper/language", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ language: langId }),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(data.detail || `HTTP ${resp.status}`);
      if (seq > reqSeq) return;
      stateData = data;
      prevDownloading = data.downloading_model || null;
      if (isMounted()) render();
      if (data.downloading_model) schedulePoll(650);
    } catch (err) {
      if (typeof window.toast === "function") {
        window.toast(`Не удалось выбрать язык: ${err.message}`, "err");
      }
    } finally {
      actionBusy = false;
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
        <div class="vc-wm-dl-box" id="vcWmDlBox">
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

    const activeLang = (stateData && stateData.active_language) || "auto";
    const langs = (stateData && stateData.languages) || [
      { id: "auto", label: "Авто", desc: "Умное определение языка речи" },
      { id: "ru", label: "Русский (RU)", desc: "Русская дорожка и субтитры" },
      { id: "en", label: "English (EN)", desc: "Английская дорожка и субтитры" },
      { id: "orig", label: "Оригинал", desc: "Оригинальная дорожка без автодубляжа" },
    ];
    const langBtnsHtml = langs
      .map(
        (l) => `
        <button type="button" class="vc-wl-btn ${l.id === activeLang ? "active" : ""}" data-lang="${esc(l.id)}" title="${esc(l.desc || l.label)}">
          ${esc(l.label)}
        </button>`
      )
      .join("");

    const langSectionHtml = `
      <div class="vc-wl-wrap">
        <span class="vc-wl-label">Язык дорожки и субтитров:</span>
        <div class="vc-wl-group">${langBtnsHtml}</div>
      </div>
    `;

    slotEl.dataset.dlModel = anyDl || "";
    slotEl.dataset.activeModel = stateData.active || "";
    slotEl.dataset.activeLang = activeLang || "auto";
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
        ${langSectionHtml}
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
      const langBtn = ev.target.closest("[data-lang]");
      if (langBtn) {
        ev.stopPropagation();
        selectLanguage(langBtn.dataset.lang);
        return;
      }
      const card = ev.target.closest(".vc-wm-card[data-model]");
      if (card) selectModel(card.dataset.model);
    });

    el.addEventListener("keydown", (ev) => {
      if (ev.key !== "Enter" && ev.key !== " ") return;
      if (ev.target.closest("[data-dl]") || ev.target.closest("[data-lang]")) return;
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

  function getLanguage() {
    return (stateData && stateData.active_language) || "auto";
  }

  window.vcWhisper = {
    mount,
    refresh: fetchModels,
    getSelected,
    getLanguage,
  };
})();
