"""Main playback polling and lyrics synchronization orchestrator."""

from datetime import datetime
import sys
import time
from typing import Any
import webbrowser

from utaten_finder.core import config, events
from utaten_finder.core.config import (
    MAIN_LOOP_SLEEP_SECONDS,
    SPOTIFY_POLL_SECONDS,
)

config.report_startup_progress(0.08, "載入歌詞比對與解析模組...")
from utaten_finder.lyrics.matching import LyricSyncEngine  # noqa: E402
from utaten_finder.lyrics.parser import parse_utaten_lyrics  # noqa: E402
from utaten_finder.lyrics.song_cache import get_cached_data, set_cached_data  # noqa: E402
from utaten_finder.media.spotify_api import get_spotify_client  # noqa: E402

config.report_startup_progress(0.14, "載入系統媒體與歌詞爬蟲模組...")
from utaten_finder.lyrics.web_scraper import (  # noqa: E402
    fetch_utaten_lyrics_html,
    is_utaten_lyric_detail_url,
    normalize_utaten_url,
    search_utaten,
)
from utaten_finder.media.system_media import get_system_media_info  # noqa: E402


def debug_log(msg: str) -> None:
    if config.DEBUG_POLLING:
        now_time = datetime.now().strftime("%H:%M:%S")
        print(f"[{now_time}] [DEBUG-Main] {msg}")


def load_lyrics_into_engine(
    url: str,
    sync_engine: LyricSyncEngine,
    *,
    song_name: str = "",
    artist_name: str = "",
    cache_status: str = "",
) -> dict[str, Any]:
    """Fetch and parse a UtaTen lyric page (or clear lyrics) and publish to EventBus."""
    normalized_url = normalize_utaten_url((url or "").strip())

    if is_utaten_lyric_detail_url(normalized_url):
        try:
            html_text = fetch_utaten_lyrics_html(normalized_url)
            parsed = parse_utaten_lyrics(html_text)
            lines = parsed.get("lines", [])
            sync_engine.reset_with_lyrics(lines)

            ui_lines = [
                {
                    "index": line["index"],
                    "original": line["original"],
                    "ruby_html": line["ruby_html"],
                    "stanza_break": bool(line.get("stanza_break", False)),
                }
                for line in lines
            ]

            payload: dict[str, Any] = {
                "title": parsed.get("title") or song_name,
                "artist": parsed.get("artist") or artist_name,
                "url": normalized_url,
                "cache_status": cache_status or "perfect",
                "lines": ui_lines,
                "error": ""
                if ui_lines
                else "此 UtaTen 頁面未解析到可用歌詞行，可嘗試切換在瀏覽器開啟。",
            }
            events.bus.emit("lyrics_loaded", payload)
            if ui_lines:
                print(
                    f"🎯 已載入純淨歌詞 ({len(ui_lines)} 行): {payload['artist']} - {payload['title']}"
                )
            else:
                print(f"⚠️ UtaTen 頁面未解析到歌詞內容: {normalized_url}")
            return payload
        except Exception as exc:
            sync_engine.reset_with_lyrics([])
            error_msg = f"載入歌詞失敗: {exc}"
            print(f"⚠️ {error_msg}")
            payload = {
                "title": song_name,
                "artist": artist_name,
                "url": normalized_url,
                "cache_status": cache_status or "partial",
                "lines": [],
                "error": error_msg,
            }
            events.bus.emit("lyrics_loaded", payload)
            return payload

    sync_engine.reset_with_lyrics([])
    payload = {
        "title": song_name,
        "artist": artist_name,
        "url": normalized_url,
        "cache_status": cache_status or "not_found",
        "lines": [],
        "error": "",
    }
    events.bus.emit("lyrics_loaded", payload)
    return payload


def _open_url_if_requested(url: str, open_external_browser: bool) -> None:
    if (
        open_external_browser
        and isinstance(url, str)
        and url.startswith(("http://", "https://"))
    ):
        try:
            webbrowser.open(url)
        except Exception as exc:
            debug_log(f"⚠️ 無法開啟系統預設瀏覽器: {exc}")


def main(
    stop_event=None,
    on_track_update=None,
    force_research_event=None,
    *,
    open_external_browser: bool = False,
    sync_engine: LyricSyncEngine | None = None,
):
    sp = None
    last_source_mode = None
    last_played_song_id = None
    last_track_info = None
    last_spotify_poll_at = 0.0

    set_asr_listening_fn = None
    asr_listening_started = False

    engine = sync_engine if sync_engine is not None else LyricSyncEngine()
    get_device_fn = None

    def _on_asr_recognized(recognized_text: str) -> None:
        match_payload = engine.process_recognized_text(recognized_text)
        device_mode = get_device_fn() if get_device_fn is not None else ""
        events.bus.emit(
            "asr_preview",
            {
                "text": recognized_text,
                "buffer_len": match_payload["buffer_length"]
                if match_payload
                else engine.get_buffer_length(),
                "matched": bool(match_payload),
                "score": match_payload["score"] if match_payload else None,
                "line_index": match_payload["display_index"]
                if match_payload
                else None,
                "device_mode": device_mode,
            },
        )
        if match_payload:
            print(
                f"\n🎤 [語音識別] 最新片段: '{match_payload['recognized_text']}' "
                f"(累積長度: {match_payload['buffer_length']})"
            )
            print(
                f"🎯 [比對成功] 歌詞: '{match_payload['target_original']}' "
                f"(正規化: '{match_payload['target_normalized']}', 分數: {match_payload['score']:.2f})"
            )
            if match_payload["is_recovery"]:
                print(
                    f"⚠️ [迷失恢復] 成功跨越較大段落，重新定位到第 {match_payload['matched_index'] + 1} 行"
                )

    asr_callback = _on_asr_recognized

    def notify_track_update(payload):
        events.bus.emit("track_updated", payload)
        if not on_track_update:
            return
        try:
            on_track_update(payload)
        except Exception as e:
            debug_log(f"track update callback 發生錯誤: {e}")

    try:
        config.STARTUP_PROGRESS_ACTIVE = sys.__stdout__ is not None
        try:
            if config.ASR_ENABLED:
                config.report_startup_progress(0.18, "準備啟動語音辨識引擎 (ASR)...")
                from utaten_finder.audio.asr_worker import (
                    ensure_asr_worker_started,
                    get_asr_device_mode,
                    set_asr_listening,
                )

                set_asr_listening_fn = set_asr_listening
                get_device_fn = get_asr_device_mode
                set_asr_listening(True)
                asr_listening_started = True
                ensure_asr_worker_started(asr_callback)
            else:
                events.bus.emit(
                    "asr_status",
                    {
                        "ready": False,
                        "device_mode": "",
                        "model": config.ASR_MODEL_SIZE,
                        "error": "",
                        "fallback": False,
                    },
                )
                config.report_startup_progress(
                    1.0, "系統初始化完成 (ASR 未啟用)！", newline=True
                )
                debug_log("ASR 自動捲動功能未啟用，跳過 ASR worker 啟動。")
        finally:
            config.STARTUP_PROGRESS_ACTIVE = False

        print("🚀 開始監聽播放狀態...")
        if open_external_browser:
            print(
                "✨ 【系統已全部就緒】請開始播放日語歌曲，將自動為您載入歌詞並開啟預設瀏覽器！"
            )
        else:
            print(
                "✨ 【系統已全部就緒】請開始播放日語歌曲，將自動為您載入純淨歌詞！"
            )
        if config.DEBUG_POLLING:
            print("🛠️  Debug 模式已開啟，將顯示詳細的輪詢日誌。")

        while True:
            if stop_event and stop_event.is_set():
                debug_log("收到停止訊號，主迴圈即將結束。")
                break

            now = time.time()
            force_research = bool(
                force_research_event and force_research_event.is_set()
            )
            if force_research and force_research_event is not None:
                force_research_event.clear()

            if force_research or (now - last_spotify_poll_at >= SPOTIFY_POLL_SECONDS):
                debug_log("=== 開始新的狀態輪詢 ===")
                current_track_info = None
                system_media_ignored = False
                source_mode = config.AUDIO_SOURCE_MODE

                if source_mode != last_source_mode:
                    debug_log(f"偵測到來源切換: {last_source_mode} -> {source_mode}")
                    last_source_mode = source_mode
                    last_played_song_id = None
                    last_track_info = None
                    if source_mode != "spotify":
                        sp = None

                if source_mode == "spotify":
                    debug_log("來源模式: spotify")
                    if sp is None:
                        try:
                            sp = get_spotify_client()
                            debug_log("Spotify client 初始化成功。")
                        except Exception as e:
                            debug_log(f"⚠️ Spotify client 初始化失敗: {e}")
                            sp = None

                    if sp is not None:
                        try:
                            sp_track = sp.current_user_playing_track()
                            if sp_track is not None and sp_track.get("is_playing"):
                                track = sp_track["item"]
                                current_track_info = {
                                    "id": track["id"],
                                    "song_name": track["name"],
                                    "artist_name": track["artists"][0]["name"],
                                    "source": "Spotify",
                                }
                                debug_log(
                                    f"-> 命中 Spotify: {current_track_info['song_name']}"
                                )
                            else:
                                debug_log("-> Spotify 沒有在播放音樂。")
                        except Exception as e:
                            debug_log(f"⚠️ Spotify API 發生錯誤: {e}")

                elif source_mode == "system_media":
                    debug_log("來源模式: system_media")
                    sys_media = get_system_media_info()
                    if sys_media:
                        if sys_media.get("ignored"):
                            system_media_ignored = True
                            debug_log(
                                f"-> 系統媒體資訊被忽略 (reason={sys_media.get('reason', 'unknown')})"
                            )
                        else:
                            virtual_id = (
                                f"sys_{sys_media['artist']}_{sys_media['title']}"
                            )
                            current_track_info = {
                                "id": virtual_id,
                                "song_name": sys_media["title"],
                                "artist_name": sys_media["artist"],
                                "source": "系統媒體 (YouTube)",
                            }
                            debug_log(
                                f"-> 命中系統媒體: {current_track_info['song_name']}"
                            )
                    else:
                        debug_log("-> 系統媒體沒有在播放音樂。")
                else:
                    debug_log(f"⚠️ 未知來源模式: {source_mode}")

                if current_track_info:
                    last_track_info = current_track_info
                elif force_research and last_track_info is not None:
                    current_track_info = last_track_info
                    debug_log(
                        f"目前無即時播放曲目，使用上一筆曲目進行重新搜尋: {current_track_info['song_name']}"
                    )

                if current_track_info:
                    song_id = current_track_info["id"]

                    if force_research or song_id != last_played_song_id:
                        song_name = current_track_info["song_name"]
                        artist_name = current_track_info["artist_name"]
                        track_source = current_track_info.get("source", "")

                        notify_track_update(
                            {
                                "song_id": song_id,
                                "song_name": song_name,
                                "artist_name": artist_name,
                                "source": track_source,
                                "cache_status": "checking",
                                "cache_url": "",
                            }
                        )

                        if force_research:
                            print(
                                f"\n🔄 手動觸發重新搜尋 [{track_source}]: {artist_name} - {song_name}"
                            )
                            cached_data = None
                        else:
                            print(
                                f"\n🎧 偵測到新歌 [{track_source}]: {artist_name} - {song_name}"
                            )
                            debug_log(f"準備進入快取/搜尋流程，Song ID: {song_id}")
                            cached_data = get_cached_data(song_id)

                        if cached_data and cached_data.get("status") in {
                            "perfect",
                            "partial",
                        }:
                            status_icon = {
                                "perfect": "✅",
                                "partial": "⚠️",
                            }.get(cached_data["status"], "❓")
                            print(
                                f"📦 從暫存庫找到歌曲 [{status_icon} {cached_data['status']}]"
                            )
                            debug_log(f"開啟快取網址: {cached_data['url']}")
                            notify_track_update(
                                {
                                    "song_id": song_id,
                                    "song_name": song_name,
                                    "artist_name": artist_name,
                                    "source": track_source,
                                    "cache_status": cached_data.get(
                                        "status", "unknown"
                                    ),
                                    "cache_url": cached_data.get("url", ""),
                                }
                            )
                            load_lyrics_into_engine(
                                cached_data["url"],
                                engine,
                                song_name=song_name,
                                artist_name=artist_name,
                                cache_status=cached_data.get("status", "perfect"),
                            )
                            _open_url_if_requested(
                                cached_data["url"], open_external_browser
                            )
                        else:
                            debug_log("快取未命中，開始啟動爬蟲搜尋 Utaten...")

                            def cache_and_open_url(
                                url,
                                status="unknown",
                                _song_id=song_id,
                                _song_name=song_name,
                                _artist_name=artist_name,
                                _source=track_source,
                            ):
                                debug_log(
                                    f"爬蟲回傳結果，寫入快取: status={status}, url={url}"
                                )
                                set_cached_data(
                                    _song_id, url, status, _artist_name, _song_name
                                )
                                notify_track_update(
                                    {
                                        "song_id": _song_id,
                                        "song_name": _song_name,
                                        "artist_name": _artist_name,
                                        "source": _source,
                                        "cache_status": status,
                                        "cache_url": url,
                                    }
                                )
                                load_lyrics_into_engine(
                                    url,
                                    engine,
                                    song_name=_song_name,
                                    artist_name=_artist_name,
                                    cache_status=status,
                                )
                                _open_url_if_requested(url, open_external_browser)

                            search_utaten(
                                artist=artist_name,
                                song=song_name,
                                open_url_callback=cache_and_open_url,
                            )
                        last_played_song_id = song_id
                    else:
                        debug_log("歌曲未切換，維持目前狀態。")
                elif system_media_ignored:
                    debug_log(
                        "目前系統媒體顯示的是我們自己開啟的歌詞頁，已略過本輪搜尋。"
                    )
                else:
                    debug_log("目前沒有任何來源正在播放音樂。")

                last_spotify_poll_at = now
                debug_log("=== 輪詢結束 ===\n")

            if stop_event:
                if stop_event.wait(MAIN_LOOP_SLEEP_SECONDS):
                    debug_log("停止事件於等待期間觸發，主迴圈結束。")
                    break
            else:
                time.sleep(MAIN_LOOP_SLEEP_SECONDS)
    finally:
        if asr_listening_started and set_asr_listening_fn is not None:
            set_asr_listening_fn(False)


if __name__ == "__main__":
    try:
        main(open_external_browser=True)
    except KeyboardInterrupt:
        print("\n🛑 收到終止訊號，正在關閉程式...")
        sys.exit(0)
