"""PyWebView single-window desktop controller and JsApi bridge."""

import json
import queue
import sys
import threading
import time
from typing import Any
from urllib.parse import urlparse
import webbrowser

import webview

from utaten_finder.core import config, events
from utaten_finder.core.orchestrator import (
    load_lyrics_into_engine,
    main as run_orchestrator,
)
from utaten_finder.lyrics.matching import LyricSyncEngine
from utaten_finder.lyrics.song_cache import set_cached_data
from utaten_finder.lyrics.web_scraper import normalize_utaten_url
from utaten_finder.media.spotify_api import (
    exchange_callback_url_for_token,
    get_spotify_authorize_url,
    has_valid_cached_token,
    load_last_callback_url,
    save_last_callback_url,
    wait_for_oauth_callback,
)

_BRIDGED_EVENTS = (
    "startup_progress",
    "worker_state",
    "asr_status",
    "track_updated",
    "lyrics_loaded",
    "lyric_highlighted",
    "asr_preview",
    "audio_level",
    "auth_status",
    "log_message",
)

_AUDIO_LEVEL_MIN_INTERVAL = 0.065


class WebViewBridge:
    """Thread-safe EventBus -> PyWebView evaluate_js dispatcher."""

    def __init__(self, window: Any) -> None:
        self._window = window
        self._queue: queue.Queue[tuple[str, Any] | None] = queue.Queue()
        self._loaded = threading.Event()
        self._closed = threading.Event()
        self._throttle_lock = threading.Lock()
        self._last_audio_emit_at = 0.0
        self._listeners: dict[str, Any] = {}
        self._dispatcher: threading.Thread | None = None

        for event_name in _BRIDGED_EVENTS:
            callback = self._make_handler(event_name)
            self._listeners[event_name] = callback
            events.bus.subscribe(event_name, callback)

    def _make_handler(self, event_name: str):
        def _handler(payload: Any) -> None:
            if self._closed.is_set():
                return
            if event_name == "audio_level":
                now = time.monotonic()
                with self._throttle_lock:
                    if now - self._last_audio_emit_at < _AUDIO_LEVEL_MIN_INTERVAL:
                        return
                    self._last_audio_emit_at = now
            self._queue.put((event_name, payload))

        return _handler

    def mark_loaded(self, *_args: Any) -> None:
        """Start draining queued events once the WebView DOM is ready."""
        if self._closed.is_set():
            return
        self._loaded.set()
        if self._dispatcher is None:
            self._dispatcher = threading.Thread(
                target=self._dispatch_loop,
                name="webview-event-bridge",
                daemon=True,
            )
            self._dispatcher.start()

    def _drain_coalesced_batch(
        self, first_item: tuple[str, Any] | None
    ) -> list[tuple[str, Any]]:
        if first_item is None:
            return []

        batch: list[tuple[str, Any]] = [first_item]
        while True:
            try:
                nxt = self._queue.get_nowait()
            except queue.Empty:
                break
            if nxt is None:
                self._closed.set()
                break
            event_name, payload = nxt
            if (
                event_name == "startup_progress"
                and batch
                and batch[-1][0] == "startup_progress"
                and float((batch[-1][1] or {}).get("fraction", 0.0)) < 1.0
            ):
                batch[-1] = (event_name, payload)
            elif event_name == "audio_level" and batch and batch[-1][0] == "audio_level":
                batch[-1] = (event_name, payload)
            else:
                batch.append((event_name, payload))
        return batch

    def _dispatch_loop(self) -> None:
        while not self._closed.is_set():
            if not self._loaded.wait(timeout=0.2):
                continue

            try:
                item = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue

            if item is None or self._closed.is_set():
                break

            batch = self._drain_coalesced_batch(item)
            for event_name, payload in batch:
                if self._closed.is_set() or self._window is None:
                    return
                try:
                    script = (
                        "window.__onBackendEvent && "
                        f"window.__onBackendEvent({json.dumps(event_name)}, {json.dumps(payload)})"
                    )
                    self._window.evaluate_js(script)
                except Exception:
                    # Window may have started closing while dispatching.
                    pass

    def close(self) -> None:
        """Unsubscribe from EventBus and terminate the JS dispatcher thread."""
        if self._closed.is_set():
            return
        self._closed.set()
        self._loaded.set()

        for event_name, callback in self._listeners.items():
            events.bus.unsubscribe(event_name, callback)
        self._listeners.clear()

        self._queue.put(None)
        if self._dispatcher is not None and self._dispatcher.is_alive():
            self._dispatcher.join(timeout=0.5)
        self._window = None


class AppController:
    """Backend state and action coordinator for the PyWebView window."""

    def __init__(self) -> None:
        self._sync_engine = LyricSyncEngine()
        self._state_lock = threading.Lock()
        self._worker_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._force_research_event = threading.Event()
        self._oauth_lock = threading.Lock()
        self._oauth_running = False

        self._current_track: dict[str, Any] | None = None
        self._current_lyrics: dict[str, Any] | None = None
        self._current_highlight_index = -1
        self._audio_source_label = ""
        self._asr_ready = False
        self._asr_device_mode = ""

        self._bus_handlers = {
            "track_updated": self._on_track_updated,
            "lyrics_loaded": self._on_lyrics_loaded,
            "lyric_highlighted": self._on_lyric_highlighted,
            "asr_status": self._on_asr_status,
            "audio_level": self._on_audio_level,
        }
        for name, fn in self._bus_handlers.items():
            events.bus.subscribe(name, fn)

    def _on_track_updated(self, payload: Any) -> None:
        if isinstance(payload, dict):
            with self._state_lock:
                self._current_track = dict(payload)

    def _on_lyrics_loaded(self, payload: Any) -> None:
        if isinstance(payload, dict):
            with self._state_lock:
                self._current_lyrics = dict(payload)
                lines = payload.get("lines") or []
                self._current_highlight_index = 0 if lines else -1

    def _on_lyric_highlighted(self, payload: Any) -> None:
        if isinstance(payload, dict) and "index" in payload:
            try:
                idx = int(payload["index"])
            except (TypeError, ValueError):
                return
            with self._state_lock:
                self._current_highlight_index = idx

    def _on_asr_status(self, payload: Any) -> None:
        if isinstance(payload, dict):
            with self._state_lock:
                self._asr_ready = bool(payload.get("ready"))
                self._asr_device_mode = str(payload.get("device_mode") or "")

    def _on_audio_level(self, payload: Any) -> None:
        if isinstance(payload, dict):
            source_name = payload.get("source_name")
            if isinstance(source_name, str) and source_name:
                with self._state_lock:
                    self._audio_source_label = source_name

    def _is_worker_running(self) -> bool:
        return bool(self._worker_thread is not None and self._worker_thread.is_alive())

    def get_initial_state(self) -> dict[str, Any]:
        with self._state_lock:
            current_track = dict(self._current_track) if self._current_track else None
            current_lyrics = dict(self._current_lyrics) if self._current_lyrics else None
            highlight_index = self._current_highlight_index
            audio_source_label = self._audio_source_label
            asr_ready = self._asr_ready
            asr_device_mode = self._asr_device_mode

        try:
            token_valid = bool(
                config.CLIENT_ID
                and config.CLIENT_SECRET
                and has_valid_cached_token()
            )
        except Exception:
            token_valid = False

        return {
            "settings": {
                "client_id": config.CLIENT_ID,
                "client_secret": config.CLIENT_SECRET,
                "redirect_uri": config.REDIRECT_URI,
                "audio_source_mode": config.AUDIO_SOURCE_MODE,
                "capture_spotify_process_only": config.CAPTURE_SPOTIFY_PROCESS_ONLY,
                "asr_enabled": config.ASR_ENABLED,
                "asr_model_size": config.ASR_MODEL_SIZE,
                "asr_device": config.ASR_DEVICE,
                "debug_polling": config.DEBUG_POLLING,
                "debug_recognition": config.DEBUG_RECOGNITION,
            },
            "asr_models": dict(config.ASR_MODEL_MAP),
            "token_valid": token_valid,
            "last_callback_url": load_last_callback_url(),
            "worker_running": self._is_worker_running(),
            "audio_source_label": audio_source_label,
            "asr_ready": asr_ready,
            "asr_device_mode": asr_device_mode,
            "current_track": current_track,
            "current_lyrics": current_lyrics,
            "current_highlight_index": highlight_index,
            "logs": events.get_recent_logs(),
        }

    def save_settings(self, settings_dict: dict[str, Any] | None = None) -> dict[str, Any]:
        data = settings_dict if isinstance(settings_dict, dict) else {}
        try:
            if (
                "client_id" in data
                or "client_secret" in data
                or "redirect_uri" in data
            ):
                config.set_spotify_credentials(
                    str(data.get("client_id", config.CLIENT_ID)),
                    str(data.get("client_secret", config.CLIENT_SECRET)),
                    str(data.get("redirect_uri", config.REDIRECT_URI)),
                )

            if "audio_source_mode" in data:
                config.set_audio_source_mode(str(data["audio_source_mode"]))

            if "capture_spotify_process_only" in data:
                config.set_capture_spotify_process_only(
                    bool(data["capture_spotify_process_only"])
                )

            if "asr_enabled" in data:
                config.set_asr_enabled(bool(data["asr_enabled"]))

            if "asr_model_size" in data and str(data["asr_model_size"]).strip():
                config.set_asr_model_size(str(data["asr_model_size"]))

            if "asr_device" in data and str(data["asr_device"]).strip():
                config.set_asr_device(str(data["asr_device"]))

            if "debug_polling" in data:
                config.set_polling_debug(bool(data["debug_polling"]))

            if "debug_recognition" in data:
                config.set_recognition_debug(bool(data["debug_recognition"]))

            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def start_listening(self) -> dict[str, Any]:
        if self._is_worker_running():
            return {"ok": True, "running": True, "message": "監聽服務已在運行中"}

        self._stop_event.clear()
        self._force_research_event.clear()

        events.bus.emit(
            "worker_state",
            {"running": True, "audio_source_label": self._audio_source_label},
        )
        events.bus.emit(
            "startup_progress",
            {"fraction": 0.04, "message": "正在啟動背景監聽與語音辨識服務..."},
        )

        def _run_worker() -> None:
            try:
                run_orchestrator(
                    stop_event=self._stop_event,
                    force_research_event=self._force_research_event,
                    use_browser=False,
                    sync_engine=self._sync_engine,
                )
            except Exception as exc:
                print(f"❌ 背景監聽服務發生例外: {exc}")
            finally:
                with self._state_lock:
                    self._audio_source_label = ""
                events.bus.emit(
                    "worker_state",
                    {"running": False, "audio_source_label": ""},
                )
                events.bus.emit(
                    "startup_progress",
                    {"fraction": 0.0, "message": "已停止監聽"},
                )

        self._worker_thread = threading.Thread(
            target=_run_worker,
            name="orchestrator-worker",
            daemon=True,
        )
        self._worker_thread.start()
        return {"ok": True, "running": True}

    def stop_listening(self) -> dict[str, Any]:
        self._stop_event.set()
        asr_mod = sys.modules.get("utaten_finder.audio.asr_worker")
        if asr_mod is not None and hasattr(asr_mod, "set_asr_listening"):
            asr_mod.set_asr_listening(False)
        return {"ok": True, "running": False}

    def jump_to_line(self, lyric_index: int) -> dict[str, Any]:
        try:
            idx = int(lyric_index)
        except (TypeError, ValueError):
            return {"ok": False, "error": "無效的歌詞行索引"}

        jump_info = self._sync_engine.apply_manual_jump(idx)
        if not jump_info:
            return {"ok": False, "error": "目前沒有可定位的歌詞行"}

        print(
            f"🖱️ [手動定位] 目前定位到第 {jump_info['current_line_no']} 行: "
            f"'{jump_info['current_line_text']}'"
        )
        return {"ok": True, "jump_info": jump_info}

    def research_lyrics(self) -> dict[str, Any]:
        if not self._is_worker_running():
            return {"ok": False, "error": "請先啟動監聽後再重新搜尋"}
        with self._state_lock:
            has_track = bool(self._current_track and self._current_track.get("song_id"))
        if not has_track:
            return {"ok": False, "error": "目前尚未偵測到播放曲目"}

        self._force_research_event.set()
        return {"ok": True}

    def update_manual_url(self, url: str) -> dict[str, Any]:
        raw_url = (url or "").strip()
        parsed = urlparse(raw_url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            return {"ok": False, "error": "請輸入有效的 http:// 或 https:// 網址"}

        normalized_url = normalize_utaten_url(raw_url)

        with self._state_lock:
            track = dict(self._current_track) if self._current_track else {}

        song_id = track.get("song_id")
        song_name = track.get("song_name", "")
        artist_name = track.get("artist_name", "")

        if song_id:
            set_cached_data(song_id, normalized_url, "perfect", artist_name, song_name)
            updated_track = {
                **track,
                "cache_status": "perfect",
                "cache_url": normalized_url,
            }
            events.bus.emit("track_updated", updated_track)

        def _load_worker() -> None:
            load_lyrics_into_engine(
                normalized_url,
                self._sync_engine,
                song_name=song_name,
                artist_name=artist_name,
                cache_status="perfect",
            )

        threading.Thread(
            target=_load_worker,
            name="manual-url-loader",
            daemon=True,
        ).start()

        return {
            "ok": True,
            "url": normalized_url,
            "message": "已套用網址並開始載入純淨歌詞",
        }

    def open_external_url(self, url: str) -> dict[str, Any]:
        clean_url = (url or "").strip()
        parsed = urlparse(clean_url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            return {"ok": False, "error": "僅支援開啟 http 或 https 外部網址"}

        try:
            webbrowser.open(clean_url)
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": f"無法開啟外部瀏覽器: {exc}"}

    def start_spotify_auth(self) -> dict[str, Any]:
        if not config.CLIENT_ID or not config.CLIENT_SECRET:
            return {
                "ok": False,
                "message": "請先填寫 Spotify Client ID 與 Client Secret",
            }

        try:
            if has_valid_cached_token():
                events.bus.emit(
                    "auth_status",
                    {
                        "token_valid": True,
                        "message": "Spotify Token 快取仍然有效，無需重新授權",
                        "toast": True,
                        "tone": "success",
                    },
                )
                return {"ok": True, "message": "Spotify Token 仍然有效"}
        except Exception:
            pass

        parsed = urlparse(config.REDIRECT_URI)
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.hostname
            or not parsed.port
        ):
            return {
                "ok": False,
                "message": "Redirect URI 格式須為 http(s)://host:port/path",
            }

        with self._oauth_lock:
            if self._oauth_running:
                return {
                    "ok": False,
                    "message": "正在等待 Spotify 瀏覽器授權回呼，請完成瀏覽器登入",
                }
            self._oauth_running = True

        try:
            authorize_url = get_spotify_authorize_url()
            webbrowser.open(authorize_url)
        except Exception as exc:
            with self._oauth_lock:
                self._oauth_running = False
            return {"ok": False, "message": f"無法啟動 Spotify 授權: {exc}"}

        callback_host = parsed.hostname
        callback_port = parsed.port
        callback_path = parsed.path or "/"

        def _listen_oauth() -> None:
            try:
                callback_url = wait_for_oauth_callback(
                    callback_host,
                    callback_port,
                    callback_path,
                    timeout=180,
                )
                if not callback_url:
                    events.bus.emit(
                        "auth_status",
                        {
                            "token_valid": False,
                            "message": "等待 Spotify 授權回呼逾時，可手動貼上 Callback URL 交換 Token",
                            "toast": True,
                            "tone": "warn",
                        },
                    )
                    return

                save_last_callback_url(callback_url)
                token_info = exchange_callback_url_for_token(callback_url)
                if not token_info:
                    raise ValueError("Token 交換回傳空結果")
                expires_in = token_info.get("expires_in", "?")
                events.bus.emit(
                    "auth_status",
                    {
                        "token_valid": True,
                        "callback_url": callback_url,
                        "message": f"Spotify 授權成功！Token 有效時間 {expires_in} 秒",
                        "toast": True,
                        "tone": "success",
                    },
                )
            except Exception as exc:
                events.bus.emit(
                    "auth_status",
                    {
                        "token_valid": False,
                        "message": f"Spotify 授權處理失敗: {exc}",
                        "toast": True,
                        "tone": "error",
                    },
                )
            finally:
                with self._oauth_lock:
                    self._oauth_running = False

        threading.Thread(
            target=_listen_oauth,
            name="spotify-oauth-callback",
            daemon=True,
        ).start()

        return {
            "ok": True,
            "message": "已開啟瀏覽器進行 Spotify 授權，完成後將自動儲存 Token...",
        }

    def exchange_callback_token(self, callback_url: str) -> dict[str, Any]:
        clean_url = (callback_url or "").strip()
        if not clean_url:
            return {"ok": False, "token_valid": False, "message": "請先輸入 Callback URL"}

        try:
            save_last_callback_url(clean_url)
            token_info = exchange_callback_url_for_token(clean_url)
            if not token_info:
                raise ValueError("Token 交換回傳空結果")
            expires_in = token_info.get("expires_in", "?")
            msg = f"Spotify Token 交換成功 (expires_in={expires_in}s)"
            events.bus.emit(
                "auth_status",
                {
                    "token_valid": True,
                    "callback_url": clean_url,
                    "message": msg,
                },
            )
            return {"ok": True, "token_valid": True, "message": msg}
        except Exception as exc:
            msg = f"Token 交換失敗: {exc}"
            return {"ok": False, "token_valid": False, "message": msg}

    def clear_logs(self) -> dict[str, Any]:
        events.clear_recent_logs()
        return {"ok": True}

    def shutdown(self) -> None:
        self._stop_event.set()
        asr_mod = sys.modules.get("utaten_finder.audio.asr_worker")
        if asr_mod is not None and hasattr(asr_mod, "request_asr_stop"):
            asr_mod.request_asr_stop()

        if self._worker_thread is not None and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=2.0)

        for name, fn in self._bus_handlers.items():
            events.bus.unsubscribe(name, fn)
        self._bus_handlers.clear()


class JsApi:
    """Thin facade exposed to WebView JavaScript via window.pywebview.api."""

    def __init__(self, controller: AppController) -> None:
        self._controller = controller

    def get_initial_state(self) -> dict[str, Any]:
        return self._controller.get_initial_state()

    def save_settings(self, settings_dict: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._controller.save_settings(settings_dict)

    def start_listening(self) -> dict[str, Any]:
        return self._controller.start_listening()

    def stop_listening(self) -> dict[str, Any]:
        return self._controller.stop_listening()

    def jump_to_line(self, lyric_index: int) -> dict[str, Any]:
        return self._controller.jump_to_line(lyric_index)

    def research_lyrics(self) -> dict[str, Any]:
        return self._controller.research_lyrics()

    def update_manual_url(self, url: str) -> dict[str, Any]:
        return self._controller.update_manual_url(url)

    def open_external_url(self, url: str) -> dict[str, Any]:
        return self._controller.open_external_url(url)

    def start_spotify_auth(self) -> dict[str, Any]:
        return self._controller.start_spotify_auth()

    def exchange_callback_token(self, callback_url: str) -> dict[str, Any]:
        return self._controller.exchange_callback_token(callback_url)

    def clear_logs(self) -> dict[str, Any]:
        return self._controller.clear_logs()


def main(debug: bool = False) -> None:
    """Launch the PyWebView single-window desktop application."""
    events.install_console_log_capture()
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    webview_profile_dir = config.DATA_DIR / "webview_profile"
    webview_profile_dir.mkdir(parents=True, exist_ok=True)

    index_path = (config.ASSETS_DIR / "web" / "index.html").resolve()
    if not index_path.is_file():
        raise FileNotFoundError(f"找不到前端頁面檔案: {index_path}")

    controller = AppController()
    api = JsApi(controller)

    config.report_startup_progress(0.90, "正在建立 PyWebView 單一視窗介面...")
    window = webview.create_window(
        "UtaTen Lyric Finder",
        url=str(index_path),
        js_api=api,
        width=1120,
        height=780,
        min_size=(820, 560),
        background_color="#0b0f17",
        text_select=True,
    )
    if window is None:
        raise RuntimeError("建立 PyWebView 視窗失敗")

    bridge = WebViewBridge(window)
    window.events.loaded += bridge.mark_loaded

    def _on_closing() -> None:
        bridge.close()
        controller.shutdown()

    window.events.closing += _on_closing

    config.report_startup_progress(
        1.0,
        "單一歌詞視窗已啟動！請點擊「▶ 開始監聽」。",
        newline=True,
    )
    config.STARTUP_PROGRESS_ACTIVE = False

    start_kwargs: dict[str, Any] = {
        "gui": "edgechromium",
        "debug": bool(debug),
        "private_mode": False,
        "storage_path": str(webview_profile_dir),
    }
    icon_path = config.ASSETS_DIR / "app.ico"
    if icon_path.is_file():
        start_kwargs["icon"] = str(icon_path)

    try:
        webview.start(**start_kwargs)
    finally:
        bridge.close()
        controller.shutdown()
