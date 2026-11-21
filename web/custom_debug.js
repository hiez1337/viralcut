(() => {
  "use strict";

  const STORAGE_KEY = "vc_debug_open";
  const ESC_MAP = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (ch) => ESC_MAP[ch]);

  let isOpen = localStorage.getItem(STORAGE_KEY) === "1";
  let isPaused = false;
  let timerId = null;
  let inFlight = false;
  let lastData = null;

  let toggleBtn = null;
  let fabBtn = null;
  let panel = null;
  let headChips = null;
  let bodyEl = null;
  let footEl = null;
  let pauseBtn = null;

  function pctClass(pct) {
    if (pct >= 95) return "err";
    if (pct >= 85) return "warn";
    return "";
  }

  function fmtNum(val, suffix = "") {
    return val === null || val === undefined ? "—" : `${val}${suffix}`;
  }

  function fmtDurSec(s) {
    const sec = Math.max(0, Math.round(Number(s) || 0));
    const m = Math.floor(sec / 60);
    const r = sec % 60;
    return m > 0 ? `${m}м ${String(r).padStart(2, "0")}с` : `${r}с`;
  }

  function buildUI() {
    const health = document.getElementById("health");
    if (health && health.parentElement) {
      const row = document.createElement("div");
      row.className = "vc-dbg-row";
      health.parentElement.insertBefore(row, health);
      row.appendChild(health);

      toggleBtn = document.createElement("button");
      toggleBtn.type = "button";
      toggleBtn.className = "vc-dbg-toggle";
      toggleBtn.title = "Live-инспектор (Ctrl+Shift+D)";
      toggleBtn.innerHTML = `<span class="vc-dbg-dot"></span><span>Инспектор</span>`;
      row.appendChild(toggleBtn);

      health.title = "Открыть или скрыть live-инспектор (Ctrl+Shift+D)";
      health.addEventListener("click", () => setOpen(!isOpen));
      toggleBtn.addEventListener("click", () => setOpen(!isOpen));
    }

    fabBtn = document.createElement("button");
    fabBtn.type = "button";
    fabBtn.className = "vc-dbg-fab";
    fabBtn.innerHTML = `<span class="vc-dbg-dot"></span><span>Инспектор</span>`;
    fabBtn.addEventListener("click", () => setOpen(!isOpen));
    document.body.appendChild(fabBtn);

    panel = document.createElement("aside");
    panel.id = "vcDebugPanel";
    panel.className = "vc-dbg-panel";
    panel.setAttribute("aria-label", "Live-инспектор системы");
    panel.innerHTML = `
      <div class="vc-dbg-head">
        <div class="vc-dbg-head-left">
          <span class="vc-dbg-title">Инспектор</span>
          <span id="vcDbgHeadChips" style="display:flex;gap:6px;flex-wrap:wrap"></span>
        </div>
        <div class="vc-dbg-actions">
          <button type="button" class="vc-dbg-btn" id="vcDbgPause">Пауза</button>
          <button type="button" class="vc-dbg-btn" id="vcDbgClose" title="Закрыть">✕</button>
        </div>
      </div>
      <div class="vc-dbg-body" id="vcDbgBody">
        <div class="vc-dbg-note">Загрузка телеметрии…</div>
      </div>
    `;
    document.body.appendChild(panel);

    headChips = document.getElementById("vcDbgHeadChips");
    bodyEl = document.getElementById("vcDbgBody");
    pauseBtn = document.getElementById("vcDbgPause");
    const closeBtn = document.getElementById("vcDbgClose");

    pauseBtn.addEventListener("click", () => {
      isPaused = !isPaused;
      pauseBtn.textContent = isPaused ? "Возобновить" : "Пауза";
      if (!isPaused && isOpen) scheduleNext(40);
    });
    closeBtn.addEventListener("click", () => setOpen(false));

    document.addEventListener("keydown", (e) => {
      if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.code === "KeyD") {
        e.preventDefault();
        setOpen(!isOpen);
      }
    });

    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible" && isOpen && !isPaused) {
        scheduleNext(80);
      } else {
        clearTimeout(timerId);
      }
    });

    applyOpenState();
  }

  function applyOpenState() {
    if (panel) panel.classList.toggle("open", isOpen);
    if (toggleBtn) toggleBtn.classList.toggle("open", isOpen);
    document.body.classList.toggle("vc-dbg-open", isOpen);
    if (isOpen && !isPaused) {
      scheduleNext(30);
    } else {
      clearTimeout(timerId);
    }
  }

  function setOpen(next) {
    isOpen = !!next;
    localStorage.setItem(STORAGE_KEY, isOpen ? "1" : "0");
    applyOpenState();
  }

  function updateToggleBadges(data) {
    const busy =
      (data.active_projects && data.active_projects.length > 0) ||
      (data.children && data.children.length > 0) ||
      (data.download && data.download.active) ||
      (data.whisper && data.whisper.downloading);
    const warn =
      (data.download && (data.download.upscale_warning || data.download.fallback)) ||
      (data.whisper && data.whisper.fallback_reason);

    for (const btn of [toggleBtn, fabBtn]) {
      if (!btn) continue;
      btn.classList.toggle("busy", !!busy && !warn);
      btn.classList.toggle("warn", !!warn);
    }
  }

  function renderData(d) {
    lastData = d;
    updateToggleBadges(d);

    const srv = d.server || {};
    const sys = d.system || {};
    const gpu = d.gpu || {};
    const w = d.whisper || {};
    const dl = d.download || {};
    const projs = d.active_projects || [];
    const kids = d.children || [];

    const encLabel = srv.hw_encoder ? srv.hw_encoder.replace("h264_", "").toUpperCase() : "LIBX264";
    headChips.innerHTML = `
      <span class="vc-dbg-chip mono">PID ${esc(srv.pid || "—")}</span>
      <span class="vc-dbg-chip ${srv.hw_encoder ? "ok" : ""}">${esc(encLabel)}</span>
      <span class="vc-dbg-chip">${esc(srv.render_workers || 1)}× воркер</span>
    `;

    // 1. Hardware card
    const vramUsed = gpu.vram_used_mb;
    const vramTotal = gpu.vram_total_mb;
    const vramPct = vramUsed != null && vramTotal ? Math.min(100, Math.round((vramUsed / vramTotal) * 100)) : 0;
    const gpuPct = gpu.util_pct != null ? Math.min(100, Math.round(gpu.util_pct)) : 0;
    const sysCpu = sys.cpu_pct != null ? Math.min(100, Math.round(sys.cpu_pct)) : 0;

    const hwHtml = `
      <div class="vc-dbg-card">
        <div class="vc-dbg-card-head">
          <span>Ресурсы</span>
          ${gpu.available ? `<span class="vc-dbg-chip ${gpu.process_on_gpu ? "ok" : ""}">${gpu.process_on_gpu ? "Python на GPU" : "GPU готов"}</span>` : `<span class="vc-dbg-chip">CPU</span>`}
        </div>
        ${gpu.available ? `
          <div class="vc-dbg-kv"><span class="vc-dbg-k">${esc(gpu.name || "GPU")}</span><span class="vc-dbg-v">${fmtNum(gpu.util_pct, "%")}${gpu.temp_c != null ? ` · ${gpu.temp_c}°C` : ""}</span></div>
          <div class="vc-dbg-bar"><i class="${pctClass(gpuPct)}" style="width:${gpuPct}%"></i></div>
          <div class="vc-dbg-kv"><span class="vc-dbg-k">VRAM</span><span class="vc-dbg-v">${fmtNum(vramUsed)} / ${fmtNum(vramTotal, " MiB")}</span></div>
          <div class="vc-dbg-bar"><i class="${pctClass(vramPct)}" style="width:${vramPct}%"></i></div>
        ` : `<div class="vc-dbg-note">NVIDIA GPU не обнаружена</div>`}
        <div class="vc-dbg-kv"><span class="vc-dbg-k">CPU (процесс / ПК)</span><span class="vc-dbg-v">${fmtNum(srv.cpu_pct, "%")} / ${fmtNum(sys.cpu_pct, "%")}</span></div>
        <div class="vc-dbg-bar"><i class="${pctClass(sysCpu)}" style="width:${sysCpu}%"></i></div>
        <div class="vc-dbg-kv"><span class="vc-dbg-k">RAM (процесс / ПК)</span><span class="vc-dbg-v">${fmtNum(srv.ram_mb, " МБ")} / ${sys.ram_used_gb != null ? `${sys.ram_used_gb}/${sys.ram_total_gb} ГБ` : "—"}</span></div>
      </div>
    `;

    // 2. Whisper card
    const isCuda = String(w.device || "").startsWith("cuda");
    const devClass = isCuda ? "ok" : w.device === "cpu" ? "warn" : "";
    const devLabel = `${w.device || "auto"} · ${w.compute_type || "—"}`;
    const expMb = w.expected_mb;
    const dlModelPct = w.downloading && expMb ? Math.min(99, Math.round((w.cache_mb / expMb) * 100)) : 0;

    const whisperHtml = `
      <div class="vc-dbg-card">
        <div class="vc-dbg-card-head">
          <span>Whisper / Модель</span>
          <span class="vc-dbg-chip ${devClass}">${esc(devLabel)}</span>
        </div>
        <div class="vc-dbg-kv"><span class="vc-dbg-k">Модель</span><span class="vc-dbg-v">${esc(w.model || "—")}</span></div>
        <div class="vc-dbg-kv"><span class="vc-dbg-k">Состояние</span><span class="vc-dbg-v">${esc(w.status || "idle")}</span></div>
        ${w.downloading ? `
          <div class="vc-dbg-kv"><span class="vc-dbg-k">Загрузка весов</span><span class="vc-dbg-v">${fmtNum(w.cache_mb, " МБ")}${expMb ? ` / ~${expMb}` : ""} · ${fmtNum(w.download_speed_mbps, " МБ/с")}</span></div>
          <div class="vc-dbg-bar"><i style="width:${dlModelPct}%"></i></div>
        ` : `
          <div class="vc-dbg-kv"><span class="vc-dbg-k">Кэш на диске</span><span class="vc-dbg-v">${w.cached ? `${w.cache_mb} МБ (готов)` : "не скачана"}</span></div>
        `}
        ${w.fallback_reason ? `<div class="vc-dbg-note warn">Откат: ${esc(w.fallback_reason)}</div>` : ""}
      </div>
    `;

    // 3. Source & Download card
    const resPill = dl.source_height && dl.target_height
      ? (dl.upscale_warning
          ? `<span class="vc-dbg-chip warn">${dl.source_height}p → ${dl.target_height}p upscale</span>`
          : `<span class="vc-dbg-chip ok">${dl.source_height}p → ${dl.target_height}p</span>`)
      : `<span class="vc-dbg-chip">${dl.active ? "Загрузка" : "Ожидание"}</span>`;

    const dlHtml = `
      <div class="vc-dbg-card">
        <div class="vc-dbg-card-head">
          <span>Источник и загрузка</span>
          ${resPill}
        </div>
        <div class="vc-dbg-kv"><span class="vc-dbg-k">Клиент</span><span class="vc-dbg-v">${esc(dl.client || "—")}</span></div>
        <div class="vc-dbg-kv"><span class="vc-dbg-k">Формат</span><span class="vc-dbg-v mono">${esc(dl.format_id || "—")}</span></div>
        <div class="vc-dbg-kv"><span class="vc-dbg-k">Скорость / Прогресс</span><span class="vc-dbg-v">${dl.speed_mbps != null ? `${dl.speed_mbps} МБ/с` : "—"}${dl.pct != null ? ` · ${dl.pct}%` : ""}${dl.eta_s != null && dl.active ? ` · ~${fmtDurSec(dl.eta_s)}` : ""}</span></div>
        ${dl.active && dl.pct != null ? `<div class="vc-dbg-bar"><i style="width:${Math.min(100, Math.round(dl.pct))}%"></i></div>` : ""}
        ${dl.fallback_reason ? `<div class="vc-dbg-note warn">${esc(dl.fallback_reason)}</div>` : ""}
      </div>
    `;

    // 4. Active stage & child processes card
    const projRows = projs.map((p) => `
      <div class="vc-dbg-proc">
        <div class="vc-dbg-proc-top">
          <strong style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(p.title)}</strong>
          <span class="vc-dbg-chip">${esc(p.progress)}% · ${fmtDurSec(p.elapsed_s)}</span>
        </div>
        <div class="vc-dbg-note">${esc(p.stage || p.status)}${p.meta && p.meta.segments_mode ? " · эконом" : ""}</div>
      </div>
    `).join("");

    const childRows = kids.map((c) => {
      const pct = Math.round((c.progress || 0) * 100);
      return `
        <div class="vc-dbg-proc">
          <div class="vc-dbg-proc-top">
            <span class="mono">${esc(c.role)} #${esc(c.pid)} · ${esc(c.label)}</span>
            <span class="vc-dbg-chip ${c.encoder && c.encoder !== "libx264" ? "ok" : ""}">${esc(c.encoder || "—")} · ${pct}%</span>
          </div>
          <div class="vc-dbg-note">${esc(c.source_res || "?")} → ${esc(c.target_res || "?")} · ${fmtDurSec(c.elapsed_s)}</div>
          <div class="vc-dbg-bar"><i style="width:${pct}%"></i></div>
        </div>
      `;
    }).join("");

    const stageHtml = `
      <div class="vc-dbg-card">
        <div class="vc-dbg-card-head">
          <span>Этап и процессы</span>
          <span class="vc-dbg-chip">${projs.length} задач · ${kids.length} ffmpeg</span>
        </div>
        ${projRows || childRows ? `${projRows}${childRows}` : `<div class="vc-dbg-note">Активных задач и процессов ffmpeg сейчас нет.</div>`}
      </div>
    `;

    bodyEl.innerHTML = `
      <div class="vc-dbg-grid">
        ${hwHtml}
        ${whisperHtml}
        ${dlHtml}
        ${stageHtml}
      </div>
      <div class="vc-dbg-foot">
        <span>Аптайм: ${fmtDurSec(srv.uptime_s)} · Потоки CPU: ${esc(sys.cpu_count || "—")}</span>
        <span>Ctrl+Shift+D · автообновление ${isPaused ? "на паузе" : "live"}</span>
      </div>
    `;
  }

  function scheduleNext(delayMs) {
    clearTimeout(timerId);
    if (!isOpen || isPaused || document.visibilityState !== "visible") return;
    timerId = setTimeout(pollOnce, delayMs);
  }

  async function pollOnce() {
    if (!isOpen || isPaused || document.visibilityState !== "visible") return;
    if (inFlight) {
      scheduleNext(500);
      return;
    }
    inFlight = true;
    try {
      const resp = await fetch("/api/debug", { cache: "no-store" });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();
      renderData(data);
      const busy =
        (data.active_projects && data.active_projects.length > 0) ||
        (data.children && data.children.length > 0) ||
        (data.download && data.download.active) ||
        (data.whisper && data.whisper.downloading);
      scheduleNext(busy ? 1000 : 2000);
    } catch (err) {
      if (bodyEl && !lastData) {
        bodyEl.innerHTML = `<div class="vc-dbg-note warn">Не удалось прочитать /api/debug: ${esc(err.message)}</div>`;
      }
      scheduleNext(2500);
    } finally {
      inFlight = false;
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", buildUI);
  } else {
    buildUI();
  }
})();
