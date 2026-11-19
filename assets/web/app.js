(() => {
  const PREFS_KEY = "utaten_ui_prefs_v1";
  const DEFAULT_PREFS = {
    fontSize: 28,
    rubySize: 52,
    showRuby: true,
    autoScroll: true,
  };
  const MAX_LOG_ITEMS = 500;

  const state = {
    prefs: loadPrefs(),
    workerRunning: false,
    currentSongId: null,
    currentCacheStatus: "",
    currentUrl: "",
    externalUrl: "",
    activeIndex: -1,
    linesCount: 0,
    audioSourceMode: "spotify",
    asrModels: {},
    asrReady: false,
    asrDeviceMode: "",
    asrModel: "",
    asrFallback: false,
    vuDecayTimer: null,
    logs: [],
    logFilter: "all",
    activeDrawer: null,
    isBootstrapped: false,
  };

  const els = {
    trackTitle: document.getElementById("track-title"),
    trackArtist: document.getElementById("track-artist"),
    sourceBadge: document.getElementById("source-badge"),
    statusBadge: document.getElementById("status-badge"),
    btnResearch: document.getElementById("btn-research"),
    btnUrlFix: document.getElementById("btn-url-fix"),
    btnTypography: document.getElementById("btn-typography"),
    typographyPopover: document.getElementById("typography-popover"),
    btnResetTypography: document.getElementById("btn-reset-typography"),
    prefFontSize: document.getElementById("pref-font-size"),
    valFontSize: document.getElementById("val-font-size"),
    prefRubySize: document.getElementById("pref-ruby-size"),
    valRubySize: document.getElementById("val-ruby-size"),
    prefShowRuby: document.getElementById("pref-show-ruby"),
    prefAutoScroll: document.getElementById("pref-auto-scroll"),
    btnSettings: document.getElementById("btn-settings"),
    btnLogs: document.getElementById("btn-logs"),
    logUnreadDot: document.getElementById("log-unread-dot"),

    urlFixBar: document.getElementById("url-fix-bar"),
    manualUrlInput: document.getElementById("manual-url-input"),
    btnApplyUrl: document.getElementById("btn-apply-url"),
    btnOpenExternal: document.getElementById("btn-open-external"),
    btnCloseUrlBar: document.getElementById("btn-close-url-bar"),

    lyricsStage: document.getElementById("lyrics-stage"),
    stateEmpty: document.getElementById("state-empty"),
    stateLoading: document.getElementById("state-loading"),
    loadingTitle: document.getElementById("loading-title"),
    loadingSubtitle: document.getElementById("loading-subtitle"),
    stateNotFound: document.getElementById("state-not-found"),
    notFoundTitle: document.getElementById("not-found-title"),
    notFoundDesc: document.getElementById("not-found-desc"),
    btnFallbackSearch: document.getElementById("btn-fallback-search"),
    btnStageResearch: document.getElementById("btn-stage-research"),
    lyricsScrollContainer: document.getElementById("lyrics-scroll-container"),
    lyricsList: document.getElementById("lyrics-list"),
    btnRecenter: document.getElementById("btn-recenter"),

    startupProgressFill: document.getElementById("startup-progress-fill"),
    startupStatusText: document.getElementById("startup-status-text"),
    btnToggleListen: document.getElementById("btn-toggle-listen"),
    listenIcon: document.getElementById("listen-icon"),
    listenLabel: document.getElementById("listen-label"),
    vuSourceLabel: document.getElementById("vu-source-label"),
    vuBarFill: document.getElementById("vu-bar-fill"),
    asrDeviceBadge: document.getElementById("asr-device-badge"),
    asrPreviewText: document.getElementById("asr-preview-text"),
    asrMatchBadge: document.getElementById("asr-match-badge"),

    drawerBackdrop: document.getElementById("drawer-backdrop"),
    settingsDrawer: document.getElementById("settings-drawer"),
    btnCloseSettings: document.getElementById("btn-close-settings"),
    tokenStatusBadge: document.getElementById("token-status-badge"),
    settingClientId: document.getElementById("setting-client-id"),
    settingClientSecret: document.getElementById("setting-client-secret"),
    btnToggleSecret: document.getElementById("btn-toggle-secret"),
    settingRedirectUri: document.getElementById("setting-redirect-uri"),
    btnSpotifyAuth: document.getElementById("btn-spotify-auth"),
    inputCallbackUrl: document.getElementById("input-callback-url"),
    btnExchangeToken: document.getElementById("btn-exchange-token"),
    authFeedback: document.getElementById("auth-feedback"),
    sourceOptSpotify: document.getElementById("source-opt-spotify"),
    sourceOptSystem: document.getElementById("source-opt-system"),
    settingProcessOnly: document.getElementById("setting-process-only"),
    settingAsrEnabled: document.getElementById("setting-asr-enabled"),
    settingAsrModel: document.getElementById("setting-asr-model"),
    settingAsrDevice: document.getElementById("setting-asr-device"),
    settingDebugPolling: document.getElementById("setting-debug-polling"),
    settingDebugRecognition: document.getElementById("setting-debug-recognition"),
    settingMatchTrace: document.getElementById("setting-match-trace"),
    btnSaveSettings: document.getElementById("btn-save-settings"),

    logDrawer: document.getElementById("log-drawer"),
    btnCloseLogs: document.getElementById("btn-close-logs"),
    logFilterBtns: Array.from(document.querySelectorAll(".filter-pill")),
    logAutoscroll: document.getElementById("log-autoscroll"),
    btnCopyLogs: document.getElementById("btn-copy-logs"),
    btnClearLogs: document.getElementById("btn-clear-logs"),
    logList: document.getElementById("log-list"),

    toastContainer: document.getElementById("toast-container"),
  };

  function loadPrefs() {
    try {
      const raw = localStorage.getItem(PREFS_KEY);
      if (!raw) {
        return { ...DEFAULT_PREFS };
      }
      const parsed = JSON.parse(raw);
      return {
        fontSize: Number(parsed.fontSize) || DEFAULT_PREFS.fontSize,
        rubySize: Number(parsed.rubySize) || DEFAULT_PREFS.rubySize,
        showRuby: parsed.showRuby !== undefined ? Boolean(parsed.showRuby) : DEFAULT_PREFS.showRuby,
        autoScroll: parsed.autoScroll !== undefined ? Boolean(parsed.autoScroll) : DEFAULT_PREFS.autoScroll,
      };
    } catch {
      return { ...DEFAULT_PREFS };
    }
  }

  function savePrefs() {
    try {
      localStorage.setItem(PREFS_KEY, JSON.stringify(state.prefs));
    } catch {
      // Ignore storage quota errors in restricted webviews.
    }
  }

  function applyTypographyPrefs() {
    document.documentElement.style.setProperty("--lyric-font-size", `${state.prefs.fontSize}px`);
    document.documentElement.style.setProperty("--ruby-font-size", `${(state.prefs.rubySize / 100).toFixed(2)}em`);
    els.lyricsStage.classList.toggle("hide-ruby", !state.prefs.showRuby);

    els.prefFontSize.value = String(state.prefs.fontSize);
    els.valFontSize.textContent = `${state.prefs.fontSize}px`;
    els.prefRubySize.value = String(state.prefs.rubySize);
    els.valRubySize.textContent = `${state.prefs.rubySize}%`;
    els.prefShowRuby.checked = state.prefs.showRuby;
    els.prefAutoScroll.checked = state.prefs.autoScroll;
  }

  function showToast(message, tone = "info") {
    if (!message) {
      return;
    }
    const toast = document.createElement("div");
    toast.className = `toast toast-${tone}`;
    toast.textContent = message;
    els.toastContainer.appendChild(toast);
    window.setTimeout(() => {
      toast.remove();
    }, 3400);
  }

  function getApi() {
    return window.pywebview && window.pywebview.api ? window.pywebview.api : null;
  }

  async function callApi(method, ...args) {
    const api = getApi();
    if (!api || typeof api[method] !== "function") {
      showToast("後端橋接尚未就緒，請稍候再試", "warn");
      return null;
    }
    try {
      return await api[method](...args);
    } catch (err) {
      showToast(`操作失敗: ${err && err.message ? err.message : err}`, "error");
      return null;
    }
  }

  function setStageView(mode) {
    els.stateEmpty.classList.toggle("hidden", mode !== "empty");
    els.stateLoading.classList.toggle("hidden", mode !== "loading");
    els.stateNotFound.classList.toggle("hidden", mode !== "not_found");
    els.lyricsScrollContainer.classList.toggle("hidden", mode !== "lyrics");
    if (mode !== "lyrics") {
      els.btnRecenter.classList.add("hidden");
    }
  }

  function updateSourceBadge(sourceText) {
    const text = sourceText || "待機中";
    els.sourceBadge.textContent = text;
    els.sourceBadge.className = "badge";
    const lower = text.toLowerCase();
    if (lower.includes("spotify")) {
      els.sourceBadge.classList.add("badge-spotify");
    } else if (lower.includes("system") || lower.includes("系統") || lower.includes("youtube")) {
      els.sourceBadge.classList.add("badge-system");
    } else {
      els.sourceBadge.classList.add("badge-neutral");
    }
  }

  function updateStatusBadge(status) {
    const map = {
      perfect: { text: "✅ Perfect", cls: "badge-perfect" },
      partial: { text: "⚠️ Partial", cls: "badge-partial" },
      not_found: { text: "❌ Not Found", cls: "badge-error" },
      checking: { text: "⏳ Checking", cls: "badge-checking" },
    };
    const meta = map[status] || { text: status || "-", cls: "badge-muted" };
    els.statusBadge.textContent = meta.text;
    els.statusBadge.className = `badge ${meta.cls}`;
  }

  function updateResearchButtons() {
    const canResearch =
      state.workerRunning &&
      Boolean(state.currentSongId) &&
      state.currentCacheStatus !== "checking";
    els.btnResearch.disabled = !canResearch;
    els.btnStageResearch.disabled = !canResearch;
  }

  function openUrlFixBar(prefillUrl = "") {
    els.urlFixBar.classList.remove("hidden");
    els.btnUrlFix.classList.add("active");
    if (prefillUrl) {
      els.manualUrlInput.value = prefillUrl;
    }
  }

  function closeUrlFixBar() {
    els.urlFixBar.classList.add("hidden");
    els.btnUrlFix.classList.remove("active");
  }

  function renderTrackUpdate(payload) {
    if (!payload || typeof payload !== "object") {
      return;
    }
    state.currentSongId = payload.song_id || null;
    state.currentCacheStatus = payload.cache_status || "";
    const songName = payload.song_name || "未知歌曲";
    const artistName = payload.artist_name || "未知歌手";
    const cacheUrl = payload.cache_url || "";

    els.trackTitle.textContent = songName;
    els.trackArtist.textContent = artistName;
    updateSourceBadge(payload.source || "待機中");
    updateStatusBadge(state.currentCacheStatus);
    updateResearchButtons();

    if (cacheUrl) {
      state.externalUrl = cacheUrl;
      if (cacheUrl.includes("utaten.com/lyric/")) {
        state.currentUrl = cacheUrl;
      }
    }

    if (state.currentCacheStatus === "checking") {
      els.loadingTitle.textContent = `正在搜尋「${songName}」...`;
      els.loadingSubtitle.textContent = `${artistName} · 正在比對並解析 UtaTen 純淨振假名歌詞`;
      setStageView("loading");
      return;
    }

    if (state.currentCacheStatus === "partial" || state.currentCacheStatus === "not_found") {
      const prefill = cacheUrl.includes("utaten.com/lyric/") ? cacheUrl : "";
      openUrlFixBar(prefill);
    } else if (state.currentCacheStatus === "perfect") {
      if (cacheUrl) {
        els.manualUrlInput.value = cacheUrl;
      }
    }
  }

  function renderLyricsLoaded(payload) {
    if (!payload || typeof payload !== "object") {
      return;
    }

    const lines = Array.isArray(payload.lines) ? payload.lines : [];
    const url = payload.url || "";
    if (url) {
      state.externalUrl = url;
      if (url.includes("utaten.com/lyric/")) {
        state.currentUrl = url;
        els.manualUrlInput.value = url;
      }
    }

    if (payload.title) {
      els.trackTitle.textContent = payload.title;
    }
    if (payload.artist) {
      els.trackArtist.textContent = payload.artist;
    }
    if (payload.cache_status) {
      state.currentCacheStatus = payload.cache_status;
      updateStatusBadge(payload.cache_status);
      updateResearchButtons();
    }

    els.lyricsList.innerHTML = "";
    state.linesCount = lines.length;
    state.activeIndex = -1;

    if (lines.length === 0) {
      const errorMsg = payload.error || "";
      els.notFoundTitle.textContent = errorMsg ? "歌詞載入失敗" : "找不到對應的 UtaTen 歌詞頁";
      els.notFoundDesc.textContent =
        errorMsg ||
        "請在上方「UtaTen 歌詞網址」貼上正確頁面連結，或點擊下方按鈕在外部瀏覽器搜尋。";
      els.btnFallbackSearch.classList.toggle("hidden", !state.externalUrl);
      setStageView("not_found");
      openUrlFixBar(url.includes("utaten.com/lyric/") ? url : "");
      return;
    }

    const frag = document.createDocumentFragment();
    for (const line of lines) {
      const idx = Number(line.index);
      const li = document.createElement("li");
      li.className = "lyric-line is-upcoming";
      if (line.stanza_break) {
        li.classList.add("stanza-start");
      }
      li.dataset.index = String(idx);
      li.title = `點擊定位到第 ${idx + 1} 行`;

      const dot = document.createElement("span");
      dot.className = "lyric-jump-dot";
      dot.textContent = String(idx + 1);

      const textSpan = document.createElement("span");
      textSpan.className = "lyric-text";
      // ruby_html is strictly sanitized with a <ruby>/<rt> whitelist on the Python backend.
      textSpan.innerHTML = line.ruby_html || "";

      li.appendChild(dot);
      li.appendChild(textSpan);
      frag.appendChild(li);
    }

    els.lyricsList.appendChild(frag);
    setStageView("lyrics");
    els.lyricsScrollContainer.scrollTop = 0;
    setActiveLine(0, { scroll: false });
  }

  function setActiveLine(index, options = {}) {
    const targetIdx = Number(index);
    if (Number.isNaN(targetIdx) || targetIdx < 0 || state.linesCount <= 0) {
      return;
    }

    const items = els.lyricsList.children;
    if (!items || items.length === 0) {
      return;
    }

    state.activeIndex = Math.min(targetIdx, items.length - 1);
    for (let i = 0; i < items.length; i += 1) {
      const el = items[i];
      el.classList.remove("is-active", "is-past", "is-upcoming");
      if (i < state.activeIndex) {
        el.classList.add("is-past");
      } else if (i === state.activeIndex) {
        el.classList.add("is-active");
      } else {
        el.classList.add("is-upcoming");
      }
    }

    const activeEl = items[state.activeIndex];
    if (activeEl && options.scroll) {
      activeEl.scrollIntoView({ behavior: "smooth", block: "center" });
    }
    updateRecenterVisibility();
  }

  function updateRecenterVisibility() {
    if (els.lyricsScrollContainer.classList.contains("hidden") || state.activeIndex < 0) {
      els.btnRecenter.classList.add("hidden");
      return;
    }
    const activeEl = els.lyricsList.children[state.activeIndex];
    if (!activeEl) {
      els.btnRecenter.classList.add("hidden");
      return;
    }
    const containerRect = els.lyricsScrollContainer.getBoundingClientRect();
    const itemRect = activeEl.getBoundingClientRect();
    const isVisible =
      itemRect.bottom >= containerRect.top + 24 &&
      itemRect.top <= containerRect.bottom - 24;
    els.btnRecenter.classList.toggle("hidden", isVisible);
  }

  function renderHighlightEvent(payload) {
    if (!payload || typeof payload !== "object") {
      return;
    }
    const idx = Number(payload.index);
    setActiveLine(idx, { scroll: state.prefs.autoScroll });

    if (payload.manual) {
      els.asrMatchBadge.textContent = `手動定位 第 ${idx + 1} 行`;
      els.asrMatchBadge.classList.remove("hidden");
    } else if (typeof payload.score === "number") {
      const tag = payload.is_recovery ? "恢復" : "比對";
      els.asrMatchBadge.textContent = `${tag} 第 ${idx + 1} 行 (${payload.score.toFixed(2)})`;
      els.asrMatchBadge.classList.remove("hidden");
    }
  }

  function renderWorkerState(payload) {
    if (!payload || typeof payload !== "object") {
      return;
    }
    state.workerRunning = Boolean(payload.running);
    els.btnToggleListen.classList.toggle("is-running", state.workerRunning);
    els.listenIcon.textContent = state.workerRunning ? "⏹" : "▶";
    els.listenLabel.textContent = state.workerRunning ? "停止監聽" : "開始監聽";
    if (payload.audio_source_label) {
      els.vuSourceLabel.textContent = payload.audio_source_label;
      els.vuSourceLabel.title = payload.audio_source_label;
    } else if (!state.workerRunning) {
      if (state.vuDecayTimer) {
        window.clearTimeout(state.vuDecayTimer);
        state.vuDecayTimer = null;
      }
      els.vuSourceLabel.textContent = "待命";
      els.vuSourceLabel.title = "";
      els.vuBarFill.style.width = "0%";
      els.vuBarFill.classList.add("is-silent");
    }
    updateResearchButtons();
  }

  function renderStartupProgress(payload) {
    if (!payload || typeof payload !== "object") {
      return;
    }
    const fraction = Math.max(0, Math.min(1, Number(payload.fraction) || 0));
    const pct = Math.round(fraction * 100);
    els.startupProgressFill.style.width = `${pct}%`;
    if (payload.message) {
      els.startupStatusText.textContent = pct >= 100 ? "就緒" : `${pct}% · ${payload.message}`;
      els.startupStatusText.title = payload.message;
    }
  }

  function renderAsrStatus(payload) {
    if (!payload || typeof payload !== "object") {
      return;
    }
    if (typeof payload.ready === "boolean") {
      state.asrReady = payload.ready;
    }
    if (payload.device_mode !== undefined) {
      state.asrDeviceMode = payload.device_mode || "";
    }
    if (payload.model) {
      state.asrModel = payload.model;
    }
    if (typeof payload.fallback === "boolean") {
      state.asrFallback = payload.fallback;
    }

    const device = (payload.device_mode ?? state.asrDeviceMode ?? "").toLowerCase();
    const modelName = payload.model || state.asrModel || "";
    const modelSuffix = modelName ? ` (${modelName})` : "";
    const isFallback =
      typeof payload.fallback === "boolean" ? payload.fallback : state.asrFallback;

    els.asrDeviceBadge.className = "badge";
    if (payload.error) {
      els.asrDeviceBadge.textContent = "ASR 異常";
      els.asrDeviceBadge.classList.add("badge-error");
      els.asrDeviceBadge.title = payload.error;
    } else if (!payload.ready) {
      els.asrDeviceBadge.textContent = els.settingAsrEnabled.checked ? "ASR 待命" : "ASR 關閉";
      els.asrDeviceBadge.classList.add("badge-muted");
      els.asrDeviceBadge.title = "";
    } else if (device === "cuda") {
      els.asrDeviceBadge.textContent = "CUDA GPU";
      els.asrDeviceBadge.classList.add("badge-spotify");
      els.asrDeviceBadge.title = `CUDA GPU${modelSuffix}`;
    } else if (isFallback) {
      els.asrDeviceBadge.textContent = "CPU 降級";
      els.asrDeviceBadge.classList.add("badge-partial");
      els.asrDeviceBadge.title = `GPU 異常已自動降級至 CPU${modelSuffix}`;
    } else {
      els.asrDeviceBadge.textContent = "CPU 模式";
      els.asrDeviceBadge.classList.add("badge-partial");
      els.asrDeviceBadge.title = `CPU 模式${modelSuffix}`;
    }
  }

  function renderAsrPreview(payload) {
    if (!payload || typeof payload !== "object") {
      return;
    }
    if (payload.text) {
      const bufText = payload.buffer_len ? ` · 緩衝 ${payload.buffer_len}` : "";
      els.asrPreviewText.textContent = `🎤 「${payload.text}」${bufText}`;
      els.asrPreviewText.title = payload.text;
    }
    if (payload.device_mode) {
      renderAsrStatus({ ready: true, device_mode: payload.device_mode });
    }
  }

  function renderAudioLevel(payload) {
    if (!payload || typeof payload !== "object") {
      return;
    }
    if (state.vuDecayTimer) {
      window.clearTimeout(state.vuDecayTimer);
      state.vuDecayTimer = null;
    }
    const rms = Number(payload.rms) || 0;
    const peak = Number(payload.peak) || 0;
    const silent = Boolean(payload.silent) || (rms <= 0 && peak <= 0);
    const rawLevel = silent
      ? 0
      : Math.max(0, Math.min(1, Math.max(rms * 1.4, peak * 0.85)));
    const pct = silent ? 0 : Math.round(Math.pow(rawLevel, 0.55) * 100);
    els.vuBarFill.style.width = `${pct}%`;
    els.vuBarFill.classList.toggle("is-silent", silent);
    if (payload.source_name) {
      els.vuSourceLabel.textContent = payload.source_name;
      els.vuSourceLabel.title = payload.source_name;
    }
    if (!silent) {
      state.vuDecayTimer = window.setTimeout(() => {
        els.vuBarFill.style.width = "0%";
        els.vuBarFill.classList.add("is-silent");
        state.vuDecayTimer = null;
      }, 700);
    }
  }

  function updateTokenBadge(valid) {
    els.tokenStatusBadge.className = "badge";
    if (valid) {
      els.tokenStatusBadge.textContent = "Token 有效";
      els.tokenStatusBadge.classList.add("badge-perfect");
    } else {
      els.tokenStatusBadge.textContent = "尚未授權";
      els.tokenStatusBadge.classList.add("badge-muted");
    }
  }

  function renderAuthStatus(payload) {
    if (!payload || typeof payload !== "object") {
      return;
    }
    if (typeof payload.token_valid === "boolean") {
      updateTokenBadge(payload.token_valid);
    }
    if (payload.callback_url) {
      els.inputCallbackUrl.value = payload.callback_url;
    }
    if (payload.message) {
      els.authFeedback.textContent = payload.message;
    }
    if (payload.toast) {
      showToast(payload.message, payload.tone || "info");
    }
  }

  function appendLogEntry(entry, renderImmediate = true) {
    if (!entry || typeof entry !== "object") {
      return;
    }
    const normalized = {
      timestamp: entry.timestamp || "",
      level: entry.level || "info",
      message: entry.message || "",
    };
    state.logs.push(normalized);
    if (state.logs.length > MAX_LOG_ITEMS) {
      state.logs.shift();
      if (renderImmediate) {
        renderAllLogs();
        return;
      }
    }

    if (normalized.level === "error" && state.activeDrawer !== "logs") {
      els.logUnreadDot.classList.remove("hidden");
    }

    if (renderImmediate && (state.logFilter === "all" || state.logFilter === normalized.level)) {
      els.logList.appendChild(createLogRow(normalized));
      if (els.logAutoscroll.checked) {
        els.logList.scrollTop = els.logList.scrollHeight;
      }
    }
  }

  function createLogRow(entry) {
    const row = document.createElement("div");
    row.className = `log-entry log-level-${entry.level}`;

    const timeSpan = document.createElement("span");
    timeSpan.className = "log-time";
    timeSpan.textContent = `[${entry.timestamp}]`;

    const msgSpan = document.createElement("span");
    msgSpan.className = "log-msg";
    msgSpan.textContent = entry.message;

    row.appendChild(timeSpan);
    row.appendChild(msgSpan);
    return row;
  }

  function renderAllLogs() {
    els.logList.innerHTML = "";
    const frag = document.createDocumentFragment();
    for (const entry of state.logs) {
      if (state.logFilter === "all" || state.logFilter === entry.level) {
        frag.appendChild(createLogRow(entry));
      }
    }
    els.logList.appendChild(frag);
    if (els.logAutoscroll.checked) {
      els.logList.scrollTop = els.logList.scrollHeight;
    }
  }

  function setAudioSourceSelection(mode) {
    state.audioSourceMode = mode === "system_media" ? "system_media" : "spotify";
    els.sourceOptSpotify.classList.toggle("active", state.audioSourceMode === "spotify");
    els.sourceOptSystem.classList.toggle("active", state.audioSourceMode === "system_media");
  }

  function populateModelOptions(modelsMap, currentModelId) {
    els.settingAsrModel.innerHTML = "";
    const map = modelsMap || {};
    let matched = false;
    for (const [display, modelId] of Object.entries(map)) {
      const opt = document.createElement("option");
      opt.value = modelId;
      opt.textContent = display;
      if (modelId === currentModelId) {
        opt.selected = true;
        matched = true;
      }
      els.settingAsrModel.appendChild(opt);
    }
    if (currentModelId && !matched) {
      const customOpt = document.createElement("option");
      customOpt.value = currentModelId;
      customOpt.textContent = currentModelId;
      customOpt.selected = true;
      els.settingAsrModel.appendChild(customOpt);
    }
  }

  function collectSettingsPayload() {
    return {
      client_id: els.settingClientId.value,
      client_secret: els.settingClientSecret.value,
      redirect_uri: els.settingRedirectUri.value,
      audio_source_mode: state.audioSourceMode,
      capture_spotify_process_only: els.settingProcessOnly.checked,
      asr_enabled: els.settingAsrEnabled.checked,
      asr_model_size: els.settingAsrModel.value,
      asr_device: els.settingAsrDevice.value,
      debug_polling: els.settingDebugPolling.checked,
      debug_recognition: els.settingDebugRecognition.checked,
      match_trace_enabled: els.settingMatchTrace.checked,
    };
  }

  async function persistSettings(quiet = true) {
    const res = await callApi("save_settings", collectSettingsPayload());
    if (!quiet && res && res.ok) {
      showToast("設定已儲存", "success");
    }
    return res;
  }

  function openDrawer(drawerName) {
    closeTypographyPopover();
    state.activeDrawer = drawerName;
    els.drawerBackdrop.classList.remove("hidden");
    els.settingsDrawer.classList.toggle("hidden", drawerName !== "settings");
    els.logDrawer.classList.toggle("hidden", drawerName !== "logs");
    if (drawerName === "logs") {
      els.logUnreadDot.classList.add("hidden");
      if (els.logAutoscroll.checked) {
        els.logList.scrollTop = els.logList.scrollHeight;
      }
    }
  }

  function closeDrawers() {
    state.activeDrawer = null;
    els.drawerBackdrop.classList.add("hidden");
    els.settingsDrawer.classList.add("hidden");
    els.logDrawer.classList.add("hidden");
  }

  function closeTypographyPopover() {
    els.typographyPopover.classList.add("hidden");
    els.btnTypography.setAttribute("aria-expanded", "false");
  }

  function toggleTypographyPopover() {
    const isHidden = els.typographyPopover.classList.contains("hidden");
    els.typographyPopover.classList.toggle("hidden", !isHidden);
    els.btnTypography.setAttribute("aria-expanded", String(isHidden));
  }

  window.__onBackendEvent = function onBackendEvent(eventName, payload) {
    switch (eventName) {
      case "startup_progress":
        renderStartupProgress(payload);
        break;
      case "worker_state":
        renderWorkerState(payload);
        break;
      case "asr_status":
        renderAsrStatus(payload);
        break;
      case "track_updated":
        renderTrackUpdate(payload);
        break;
      case "lyrics_loaded":
        renderLyricsLoaded(payload);
        break;
      case "lyric_highlighted":
        renderHighlightEvent(payload);
        break;
      case "asr_preview":
        renderAsrPreview(payload);
        break;
      case "audio_level":
        renderAudioLevel(payload);
        break;
      case "auth_status":
        renderAuthStatus(payload);
        break;
      case "log_message":
        appendLogEntry(payload, true);
        break;
      default:
        break;
    }
  };

  async function bootstrap() {
    if (state.isBootstrapped) {
      return;
    }
    const api = getApi();
    if (!api) {
      return;
    }
    state.isBootstrapped = true;

    const initial = await callApi("get_initial_state");
    if (!initial || typeof initial !== "object") {
      return;
    }

    const settings = initial.settings || {};
    state.asrModels = initial.asr_models || {};

    els.settingClientId.value = settings.client_id || "";
    els.settingClientSecret.value = settings.client_secret || "";
    els.settingRedirectUri.value = settings.redirect_uri || "http://127.0.0.1:8888/callback";
    els.inputCallbackUrl.value = initial.last_callback_url || "";
    updateTokenBadge(Boolean(initial.token_valid));

    setAudioSourceSelection(settings.audio_source_mode || "spotify");
    els.settingProcessOnly.checked = settings.capture_spotify_process_only !== false;
    els.settingAsrEnabled.checked = settings.asr_enabled !== false;
    populateModelOptions(state.asrModels, settings.asr_model_size || "turbo");
    els.settingAsrDevice.value = settings.asr_device || "auto";
    els.settingDebugPolling.checked = Boolean(settings.debug_polling);
    els.settingDebugRecognition.checked = Boolean(settings.debug_recognition);
    els.settingMatchTrace.checked = Boolean(settings.match_trace_enabled);

    renderWorkerState({
      running: Boolean(initial.worker_running),
      audio_source_label: initial.audio_source_label || "",
    });
    renderAsrStatus({
      ready: Boolean(initial.asr_ready),
      device_mode: initial.asr_device_mode || "",
      model: initial.asr_model || settings.asr_model_size || "",
      fallback: Boolean(initial.asr_fallback),
    });

    if (Array.isArray(initial.logs)) {
      state.logs = [];
      for (const entry of initial.logs) {
        appendLogEntry(entry, false);
      }
      renderAllLogs();
    }

    if (initial.current_track) {
      renderTrackUpdate(initial.current_track);
    }
    if (initial.current_lyrics && Array.isArray(initial.current_lyrics.lines) && initial.current_lyrics.lines.length > 0) {
      renderLyricsLoaded(initial.current_lyrics);
      if (typeof initial.current_highlight_index === "number" && initial.current_highlight_index >= 0) {
        setActiveLine(initial.current_highlight_index, { scroll: false });
      }
    }
  }

  function bindEvents() {
    applyTypographyPrefs();

    els.btnTypography.addEventListener("click", (event) => {
      event.stopPropagation();
      toggleTypographyPopover();
    });

    els.typographyPopover.addEventListener("click", (event) => {
      event.stopPropagation();
    });

    document.addEventListener("click", () => {
      closeTypographyPopover();
    });

    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        closeTypographyPopover();
        closeDrawers();
      }
    });

    els.prefFontSize.addEventListener("input", () => {
      state.prefs.fontSize = Number(els.prefFontSize.value);
      applyTypographyPrefs();
      savePrefs();
    });

    els.prefRubySize.addEventListener("input", () => {
      state.prefs.rubySize = Number(els.prefRubySize.value);
      applyTypographyPrefs();
      savePrefs();
    });

    els.prefShowRuby.addEventListener("change", () => {
      state.prefs.showRuby = els.prefShowRuby.checked;
      applyTypographyPrefs();
      savePrefs();
    });

    els.prefAutoScroll.addEventListener("change", () => {
      state.prefs.autoScroll = els.prefAutoScroll.checked;
      applyTypographyPrefs();
      savePrefs();
    });

    els.btnResetTypography.addEventListener("click", () => {
      state.prefs = { ...DEFAULT_PREFS };
      applyTypographyPrefs();
      savePrefs();
    });

    els.btnUrlFix.addEventListener("click", () => {
      if (els.urlFixBar.classList.contains("hidden")) {
        openUrlFixBar(state.currentUrl || "");
        els.manualUrlInput.focus();
      } else {
        closeUrlFixBar();
      }
    });

    els.btnCloseUrlBar.addEventListener("click", closeUrlFixBar);

    els.btnApplyUrl.addEventListener("click", async () => {
      const url = els.manualUrlInput.value.trim();
      if (!url) {
        showToast("請先貼上 UtaTen 歌詞網址", "warn");
        return;
      }
      const res = await callApi("update_manual_url", url);
      if (res && res.ok) {
        showToast(res.message || "已更新並載入歌詞", "success");
      } else if (res && res.error) {
        showToast(res.error, "error");
      }
    });

    els.manualUrlInput.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        els.btnApplyUrl.click();
      }
    });

    els.btnOpenExternal.addEventListener("click", async () => {
      const url = els.manualUrlInput.value.trim() || state.externalUrl || "https://utaten.com/";
      const res = await callApi("open_external_url", url);
      if (res && !res.ok && res.error) {
        showToast(res.error, "error");
      }
    });

    els.btnFallbackSearch.addEventListener("click", async () => {
      const url = state.externalUrl || "https://utaten.com/";
      const res = await callApi("open_external_url", url);
      if (res && !res.ok && res.error) {
        showToast(res.error, "error");
      }
    });

    const triggerResearch = async () => {
      const res = await callApi("research_lyrics");
      if (res && !res.ok && res.error) {
        showToast(res.error, "warn");
      }
    };
    els.btnResearch.addEventListener("click", triggerResearch);
    els.btnStageResearch.addEventListener("click", triggerResearch);

    els.lyricsList.addEventListener("click", async (event) => {
      const selection = window.getSelection ? window.getSelection().toString() : "";
      if (selection) {
        return;
      }
      const lineEl = event.target.closest(".lyric-line");
      if (!lineEl) {
        return;
      }
      const index = Number(lineEl.dataset.index);
      if (Number.isNaN(index)) {
        return;
      }
      setActiveLine(index, { scroll: true });
      const res = await callApi("jump_to_line", index);
      if (res && res.ok) {
        els.asrMatchBadge.textContent = `手動定位 第 ${index + 1} 行`;
        els.asrMatchBadge.classList.remove("hidden");
      }
    });

    els.lyricsScrollContainer.addEventListener("scroll", updateRecenterVisibility);
    els.btnRecenter.addEventListener("click", () => {
      if (state.activeIndex >= 0) {
        setActiveLine(state.activeIndex, { scroll: true });
      }
    });

    els.btnToggleListen.addEventListener("click", async () => {
      if (state.workerRunning) {
        await callApi("stop_listening");
      } else {
        await persistSettings(true);
        await callApi("start_listening");
      }
    });

    els.btnSettings.addEventListener("click", () => openDrawer("settings"));
    els.btnLogs.addEventListener("click", () => openDrawer("logs"));
    els.btnCloseSettings.addEventListener("click", closeDrawers);
    els.btnCloseLogs.addEventListener("click", closeDrawers);
    els.drawerBackdrop.addEventListener("click", closeDrawers);

    els.btnToggleSecret.addEventListener("click", () => {
      const isMasked = els.settingClientSecret.type === "password";
      els.settingClientSecret.type = isMasked ? "text" : "password";
      els.btnToggleSecret.textContent = isMasked ? "隱藏" : "顯示";
    });

    els.sourceOptSpotify.addEventListener("click", () => {
      setAudioSourceSelection("spotify");
      persistSettings(true);
    });
    els.sourceOptSystem.addEventListener("click", () => {
      setAudioSourceSelection("system_media");
      persistSettings(true);
    });

    for (const control of [
      els.settingProcessOnly,
      els.settingAsrEnabled,
      els.settingAsrModel,
      els.settingAsrDevice,
      els.settingDebugPolling,
      els.settingDebugRecognition,
      els.settingMatchTrace,
    ]) {
      control.addEventListener("change", () => {
        persistSettings(true);
        if (control === els.settingAsrEnabled) {
          if (els.settingAsrEnabled.checked && state.asrReady) {
            renderAsrStatus({
              ready: true,
              device_mode: state.asrDeviceMode,
              model: state.asrModel,
              fallback: state.asrFallback,
            });
          } else {
            const prevReady = state.asrReady;
            const prevDevice = state.asrDeviceMode;
            renderAsrStatus({ ready: false, device_mode: "" });
            if (!els.settingAsrEnabled.checked) {
              state.asrReady = prevReady;
              state.asrDeviceMode = prevDevice;
            }
          }
        }
      });
    }

    els.btnSaveSettings.addEventListener("click", async () => {
      await persistSettings(false);
    });

    els.btnSpotifyAuth.addEventListener("click", async () => {
      await persistSettings(true);
      const res = await callApi("start_spotify_auth");
      if (res && res.message) {
        els.authFeedback.textContent = res.message;
        showToast(res.message, res.ok ? "info" : "error");
      }
    });

    els.btnExchangeToken.addEventListener("click", async () => {
      const url = els.inputCallbackUrl.value.trim();
      if (!url) {
        showToast("請先輸入 Callback URL", "warn");
        return;
      }
      await persistSettings(true);
      const res = await callApi("exchange_callback_token", url);
      if (res && res.message) {
        els.authFeedback.textContent = res.message;
        showToast(res.message, res.ok ? "success" : "error");
      }
      if (res && typeof res.token_valid === "boolean") {
        updateTokenBadge(res.token_valid);
      }
    });

    for (const pill of els.logFilterBtns) {
      pill.addEventListener("click", () => {
        state.logFilter = pill.dataset.level || "all";
        for (const btn of els.logFilterBtns) {
          btn.classList.toggle("active", btn === pill);
        }
        renderAllLogs();
      });
    }

    els.btnCopyLogs.addEventListener("click", async () => {
      const text = state.logs
        .filter((entry) => state.logFilter === "all" || state.logFilter === entry.level)
        .map((entry) => `[${entry.timestamp}] [${entry.level.toUpperCase()}] ${entry.message}`)
        .join("\n");
      if (!text) {
        showToast("目前沒有可複製的日誌", "warn");
        return;
      }
      try {
        await navigator.clipboard.writeText(text);
        showToast("已複製日誌到剪貼簿", "success");
      } catch {
        showToast("無法存取剪貼簿", "error");
      }
    });

    els.btnClearLogs.addEventListener("click", async () => {
      state.logs = [];
      els.logList.innerHTML = "";
      await callApi("clear_logs");
    });
  }

  bindEvents();
  window.addEventListener("pywebviewready", bootstrap);
  if (getApi()) {
    bootstrap();
  }
})();
