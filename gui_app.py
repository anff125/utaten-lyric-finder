import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import cast
from urllib.parse import urlparse

import customtkinter as ctk

import config
from auto_lyrics import main as run_auto_lyrics
from song_cache import set_cached_data
from spotify_api import (
    exchange_callback_url_for_token,
    get_spotify_authorize_url,
    has_valid_cached_token,
    load_last_callback_url,
    save_last_callback_url,
)


class _CallbackHTTPServer(HTTPServer):
    callback_path: str
    callback_host: str
    callback_url: str | None


class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        server = cast(_CallbackHTTPServer, self.server)
        if self.path.startswith(server.callback_path):
            full_url = f"http://{server.callback_host}:{server.server_port}{self.path}"
            server.callback_url = full_url
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                "<html><body><h3>Authentication received. You can close this tab.</h3></body></html>".encode(
                    "utf-8"
                )
            )
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        return


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Utaten with Spotify Tool")
        self.geometry("720x680")

        ctk.set_appearance_mode("System")
        ctk.set_default_color_theme("green")

        self.worker_thread = None
        self.stop_event = threading.Event()
        self.current_song_id = None
        self.current_song_name = ""
        self.current_artist_name = ""
        self.current_cache_status = ""

        self._build_ui()
        self._load_defaults()

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)

        container = ctk.CTkFrame(self)
        container.grid(row=0, column=0, padx=16, pady=16, sticky="nsew")
        container.grid_columnconfigure(1, weight=1)

        row = 0
        ctk.CTkLabel(container, text="Spotify Client ID").grid(
            row=row, column=0, padx=8, pady=6, sticky="w"
        )
        self.client_id_entry = ctk.CTkEntry(container)
        self.client_id_entry.grid(row=row, column=1, padx=8, pady=6, sticky="ew")

        row += 1
        ctk.CTkLabel(container, text="Spotify Client Secret").grid(
            row=row, column=0, padx=8, pady=6, sticky="w"
        )
        self.client_secret_entry = ctk.CTkEntry(container, show="*")
        self.client_secret_entry.grid(row=row, column=1, padx=8, pady=6, sticky="ew")

        row += 1
        ctk.CTkLabel(container, text="Redirect URI").grid(
            row=row, column=0, padx=8, pady=6, sticky="w"
        )
        self.redirect_uri_entry = ctk.CTkEntry(container)
        self.redirect_uri_entry.grid(row=row, column=1, padx=8, pady=6, sticky="ew")

        row += 1
        ctk.CTkLabel(container, text="Callback URL (auto)").grid(
            row=row, column=0, padx=8, pady=6, sticky="w"
        )
        self.callback_url_entry = ctk.CTkEntry(container)
        self.callback_url_entry.grid(row=row, column=1, padx=8, pady=6, sticky="ew")

        row += 1
        auth_btn = ctk.CTkButton(
            container,
            text="Start Spotify Auth",
            command=self.start_spotify_auth,
        )
        auth_btn.grid(row=row, column=0, padx=8, pady=10, sticky="w")

        self.exchange_btn = ctk.CTkButton(
            container,
            text="Exchange Callback Token",
            command=self.exchange_callback_token,
        )
        self.exchange_btn.grid(row=row, column=1, padx=8, pady=10, sticky="w")

        row += 1
        ctk.CTkLabel(container, text="Audio Source").grid(
            row=row, column=0, padx=8, pady=6, sticky="w"
        )
        self.source_var = ctk.StringVar(value="spotify")
        source_frame = ctk.CTkFrame(container)
        source_frame.grid(row=row, column=1, padx=8, pady=6, sticky="w")
        ctk.CTkRadioButton(
            source_frame,
            text="Spotify",
            variable=self.source_var,
            value="spotify",
            command=self.on_source_changed,
        ).pack(side="left", padx=6)
        ctk.CTkRadioButton(
            source_frame,
            text="System Media",
            variable=self.source_var,
            value="system_media",
            command=self.on_source_changed,
        ).pack(side="left", padx=6)

        row += 1
        self.debug_var = ctk.BooleanVar(value=False)
        debug_checkbox = ctk.CTkCheckBox(
            container,
            text="Enable Debug Logs",
            variable=self.debug_var,
            command=self.on_debug_changed,
        )
        debug_checkbox.grid(row=row, column=0, padx=8, pady=6, sticky="w")

        self.asr_var = ctk.BooleanVar(value=True)
        asr_checkbox = ctk.CTkCheckBox(
            container,
            text="Enable Auto-scroll Lyrics (ASR)",
            variable=self.asr_var,
            command=self.on_asr_changed,
        )
        asr_checkbox.grid(row=row, column=1, padx=8, pady=6, sticky="w")

        row += 1
        ctk.CTkLabel(container, text="ASR Model").grid(
            row=row, column=0, padx=8, pady=6, sticky="w"
        )
        self.asr_model_var = ctk.StringVar(value="large-v3")
        self.asr_model_combo = ctk.CTkComboBox(
            container,
            variable=self.asr_model_var,
            values=["tiny", "base", "small", "medium", "large-v2", "large-v3"],
            command=self.on_asr_config_changed,
        )
        self.asr_model_combo.grid(row=row, column=1, padx=8, pady=6, sticky="ew")

        row += 1
        ctk.CTkLabel(container, text="ASR Device").grid(
            row=row, column=0, padx=8, pady=6, sticky="w"
        )
        self.asr_device_var = ctk.StringVar(value="auto")
        self.asr_device_combo = ctk.CTkComboBox(
            container,
            variable=self.asr_device_var,
            values=["auto", "cuda", "cpu"],
            command=self.on_asr_config_changed,
        )
        self.asr_device_combo.grid(row=row, column=1, padx=8, pady=6, sticky="ew")

        row += 1
        run_frame = ctk.CTkFrame(container)
        run_frame.grid(row=row, column=0, columnspan=2, padx=8, pady=12, sticky="ew")

        self.start_btn = ctk.CTkButton(
            run_frame, text="Start Tool", command=self.start_tool
        )
        self.start_btn.pack(side="left", padx=6, pady=8)
        self.stop_btn = ctk.CTkButton(
            run_frame, text="Stop Tool", command=self.stop_tool, state="disabled"
        )
        self.stop_btn.pack(side="left", padx=6, pady=8)

        row += 1
        track_frame = ctk.CTkFrame(container)
        track_frame.grid(row=row, column=0, columnspan=2, padx=8, pady=8, sticky="ew")
        track_frame.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(track_frame, text="Now Playing").grid(
            row=0, column=0, padx=8, pady=(8, 4), sticky="w"
        )
        self.current_song_label = ctk.CTkLabel(
            track_frame,
            text="(No track detected)",
            anchor="w",
            justify="left",
        )
        self.current_song_label.grid(row=0, column=1, padx=8, pady=(8, 4), sticky="ew")

        ctk.CTkLabel(track_frame, text="Cache Status").grid(
            row=1, column=0, padx=8, pady=4, sticky="w"
        )
        self.cache_status_label = ctk.CTkLabel(
            track_frame,
            text="-",
            anchor="w",
        )
        self.cache_status_label.grid(row=1, column=1, padx=8, pady=4, sticky="ew")

        ctk.CTkLabel(track_frame, text="Manual Lyric URL").grid(
            row=2, column=0, padx=8, pady=4, sticky="w"
        )
        self.manual_url_entry = ctk.CTkEntry(
            track_frame,
            placeholder_text="status 是 partial/not_found 時可在此貼上正確 Utaten 歌詞網址",
        )
        self.manual_url_entry.grid(row=2, column=1, padx=8, pady=4, sticky="ew")
        self.manual_url_entry.configure(state="disabled")

        self.update_cache_btn = ctk.CTkButton(
            track_frame,
            text="Update Cache URL",
            command=self.update_cache_url,
            state="disabled",
        )
        self.update_cache_btn.grid(row=3, column=1, padx=8, pady=(4, 8), sticky="w")

        row += 1
        self.status_label = ctk.CTkLabel(container, text="Ready")
        self.status_label.grid(
            row=row, column=0, columnspan=2, padx=8, pady=6, sticky="w"
        )

        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def _load_defaults(self):
        self.client_id_entry.insert(0, config.CLIENT_ID)
        self.client_secret_entry.insert(0, config.CLIENT_SECRET)
        self.redirect_uri_entry.insert(0, config.REDIRECT_URI)
        cached_callback_url = load_last_callback_url()
        if cached_callback_url:
            self.callback_url_entry.insert(0, cached_callback_url)
        self.source_var.set(config.AUDIO_SOURCE_MODE)
        self.debug_var.set(config.DEBUG)
        self.asr_var.set(config.ASR_ENABLED)
        self.asr_model_var.set(config.ASR_MODEL_SIZE)
        self.asr_device_var.set(config.ASR_DEVICE)

    def _sync_config_from_ui(self):
        config.set_spotify_credentials(
            self.client_id_entry.get(),
            self.client_secret_entry.get(),
            self.redirect_uri_entry.get(),
        )
        config.set_audio_source_mode(self.source_var.get())
        config.set_debug(self.debug_var.get())
        config.set_asr_enabled(self.asr_var.get())
        config.set_asr_model_size(self.asr_model_var.get())
        config.set_asr_device(self.asr_device_var.get())

    def set_status(self, text):
        self.status_label.configure(text=text)

    def _on_track_update_from_worker(self, payload):
        self.after(0, lambda p=payload: self._render_track_update(p))

    def _render_track_update(self, payload):
        if not isinstance(payload, dict):
            return

        self.current_song_id = payload.get("song_id")
        self.current_song_name = payload.get("song_name", "")
        self.current_artist_name = payload.get("artist_name", "")
        self.current_cache_status = payload.get("cache_status", "")
        source = payload.get("source", "Unknown")
        cache_url = payload.get("cache_url", "")

        song_text = f"[{source}] {self.current_artist_name} - {self.current_song_name}"
        self.current_song_label.configure(text=song_text)

        status_map = {
            "perfect": "✅ perfect",
            "partial": "⚠️ partial",
            "not_found": "❌ not_found",
            "checking": "⏳ checking",
        }
        self.cache_status_label.configure(
            text=status_map.get(
                self.current_cache_status, self.current_cache_status or "-"
            )
        )

        can_manual_fix = self.current_cache_status in {"partial", "not_found"}
        if can_manual_fix:
            self.update_cache_btn.configure(state="normal")
            self.manual_url_entry.configure(state="normal")
            if cache_url:
                self.manual_url_entry.delete(0, "end")
                self.manual_url_entry.insert(0, cache_url)
        else:
            self.update_cache_btn.configure(state="disabled")
            self.manual_url_entry.configure(state="disabled")
            self.manual_url_entry.delete(0, "end")

    def update_cache_url(self):
        if self.current_cache_status not in {"partial", "not_found"}:
            self.set_status("Cache 狀態不是 partial/not_found，無需手動修正")
            return

        if not self.current_song_id:
            self.set_status("目前沒有可修正的歌曲")
            return

        url = self.manual_url_entry.get().strip()
        parsed = urlparse(url)
        if not parsed.scheme.startswith("http") or not parsed.netloc:
            self.set_status("請輸入有效網址 (http/https)")
            return

        try:
            set_cached_data(
                self.current_song_id,
                url,
                "perfect",
                self.current_artist_name,
                self.current_song_name,
            )
            self.current_cache_status = "perfect"
            self.cache_status_label.configure(text="✅ perfect (manual)")
            self.update_cache_btn.configure(state="disabled")
            self.manual_url_entry.configure(state="disabled")
            self.set_status("已手動更新 cache，下一次播放這首歌會使用你提供的網址")
        except Exception as e:
            self.set_status(f"更新 cache 失敗: {e}")

    def on_source_changed(self):
        try:
            config.set_audio_source_mode(self.source_var.get())
            self.set_status(
                f"Source changed: {config.AUDIO_SOURCE_MODE} (applies next poll)"
            )
        except Exception as e:
            self.set_status(f"Source change failed: {e}")

    def on_debug_changed(self):
        config.set_debug(self.debug_var.get())
        self.set_status(f"Debug: {'ON' if config.DEBUG else 'OFF'}")

    def on_asr_changed(self):
        config.set_asr_enabled(self.asr_var.get())
        if config.ASR_ENABLED:
            self.set_status("Auto-scroll ASR: ON")
        else:
            self.set_status("Auto-scroll ASR: OFF (GPU load reduced)")

    def on_asr_config_changed(self, _=None):
        config.set_asr_model_size(self.asr_model_var.get())
        config.set_asr_device(self.asr_device_var.get())
        self.set_status(
            f"ASR Config updated: Model={config.ASR_MODEL_SIZE}, Device={config.ASR_DEVICE} (Applies on next Start)"
        )

    def start_spotify_auth(self):
        try:
            self._sync_config_from_ui()
            if has_valid_cached_token():
                self.set_status("Spotify token cache still valid, auth skipped")
                return

            parsed = urlparse(config.REDIRECT_URI)
            if (
                not parsed.scheme.startswith("http")
                or not parsed.hostname
                or not parsed.port
            ):
                raise ValueError("Redirect URI 必須是 http(s)://host:port/path")

            callback_host = parsed.hostname
            callback_port = parsed.port
            callback_path = parsed.path or "/"

            authorize_url = get_spotify_authorize_url()
            self.set_status("Auth URL generated, opening browser...")
            webbrowser.open(authorize_url)

            def listen_callback():
                try:
                    server = _CallbackHTTPServer(
                        (callback_host, callback_port), _CallbackHandler
                    )
                    server.callback_path = callback_path
                    server.callback_host = callback_host
                    server.callback_url = None
                    server.timeout = 180
                    server.handle_request()
                    callback_url = server.callback_url
                    server.server_close()
                    if callback_url:
                        self.after(0, lambda: self._on_callback_received(callback_url))
                    else:
                        self.after(
                            0, lambda: self.set_status("No callback URL received")
                        )
                except Exception as e:
                    self.after(
                        0,
                        lambda err=e: self.set_status(
                            f"Callback listener failed: {err}"
                        ),
                    )

            threading.Thread(target=listen_callback, daemon=True).start()
        except Exception as e:
            self.set_status(f"Auth start failed: {e}")

    def _on_callback_received(self, callback_url):
        self.callback_url_entry.delete(0, "end")
        self.callback_url_entry.insert(0, callback_url)
        save_last_callback_url(callback_url)

        try:
            self._sync_config_from_ui()
            token_info = exchange_callback_url_for_token(callback_url)
            if not token_info:
                raise ValueError("Token exchange returned empty result")
            expires_in = token_info.get("expires_in", "?")
            self.set_status(
                f"Callback received and token saved. expires_in={expires_in}s"
            )
        except Exception as e:
            self.set_status(f"Callback received, but token exchange failed: {e}")

    def exchange_callback_token(self):
        callback_url = self.callback_url_entry.get().strip()
        if not callback_url:
            self.set_status("Callback URL is empty")
            return

        try:
            self._sync_config_from_ui()
            save_last_callback_url(callback_url)
            token_info = exchange_callback_url_for_token(callback_url)
            if not token_info:
                raise ValueError("Token exchange returned empty result")
            expires_in = token_info.get("expires_in", "?")
            self.set_status(f"Spotify token saved. expires_in={expires_in}s")
        except Exception as e:
            self.set_status(f"Token exchange failed: {e}")

    def start_tool(self):
        if self.worker_thread and self.worker_thread.is_alive():
            self.set_status("Tool already running")
            return

        try:
            self._sync_config_from_ui()
            self.stop_event.clear()

            def _run():
                run_auto_lyrics(
                    stop_event=self.stop_event,
                    on_track_update=self._on_track_update_from_worker,
                )

            self.worker_thread = threading.Thread(target=_run, daemon=True)
            self.worker_thread.start()
            self.start_btn.configure(state="disabled")
            self.stop_btn.configure(state="normal")
            self.set_status("Tool running")
            self.after(300, self._poll_worker_status)
        except Exception as e:
            self.set_status(f"Start failed: {e}")

    def stop_tool(self):
        if not (self.worker_thread and self.worker_thread.is_alive()):
            self._on_worker_stopped()
            return

        self.stop_event.set()
        self.stop_btn.configure(state="disabled")
        self.set_status("Stopping tool...")
        self.after(300, self._poll_worker_status)

    def _poll_worker_status(self):
        if self.worker_thread and self.worker_thread.is_alive():
            self.after(300, self._poll_worker_status)
            return

        self._on_worker_stopped()

    def _on_worker_stopped(self):
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.set_status("Tool stopped")

    def on_close(self):
        self.stop_event.set()
        self.destroy()


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
