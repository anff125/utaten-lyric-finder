# auto_lyrics.py
import sys
import time
from datetime import datetime

import config
from asr import ensure_asr_worker_started
from config import (
    MAIN_LOOP_SLEEP_SECONDS,
    SPOTIFY_POLL_SECONDS,
)
from lyrics_browser import (
    cleanup_playwright,
    flush_pending_scroll,
    open_in_dedicated_window,
    queue_scroll_when_next_line_matches,
)
from spotify_api import get_spotify_client
from web_scraper import search_utaten
from song_cache import get_cached_data, set_cached_data
from system_media import get_system_media_info


def debug_log(msg):
    if config.DEBUG:
        now_time = datetime.now().strftime("%H:%M:%S")
        print(f"[{now_time}] [DEBUG-Main] {msg}")


def main(stop_event=None, on_track_update=None, on_asr_status=None):
    sp = None
    last_source_mode = None
    last_played_song_id = None
    last_spotify_poll_at = 0.0

    def notify_track_update(payload):
        if not on_track_update:
            return
        try:
            on_track_update(payload)
        except Exception as e:
            debug_log(f"track update callback 發生錯誤: {e}")

    try:
        ensure_asr_worker_started(
            queue_scroll_when_next_line_matches,
            on_asr_status=on_asr_status,
        )

        print("🚀 開始監聽播放狀態... (按 Ctrl+C 停止)")
        if config.DEBUG:
            print("🛠️  Debug 模式已開啟，將顯示詳細的輪詢日誌。")

        while True:
            if stop_event and stop_event.is_set():
                debug_log("收到停止訊號，主迴圈即將結束。")
                break

            now = time.time()

            if now - last_spotify_poll_at >= SPOTIFY_POLL_SECONDS:
                debug_log("=== 開始新的狀態輪詢 ===")
                current_track_info = None
                system_media_ignored = False
                source_mode = config.AUDIO_SOURCE_MODE

                if source_mode != last_source_mode:
                    debug_log(f"偵測到來源切換: {last_source_mode} -> {source_mode}")
                    last_source_mode = source_mode
                    last_played_song_id = None
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
                                "source": "系統媒體",
                            }
                            debug_log(
                                f"-> 命中系統媒體: {current_track_info['song_name']}"
                            )
                    else:
                        debug_log("-> 系統媒體沒有在播放音樂。")
                else:
                    debug_log(f"⚠️ 未知來源模式: {source_mode}")

                # 3. 處理偵測到的歌曲
                if current_track_info:
                    song_id = current_track_info["id"]

                    if song_id != last_played_song_id:
                        song_name = current_track_info["song_name"]
                        artist_name = current_track_info["artist_name"]

                        notify_track_update(
                            {
                                "song_id": song_id,
                                "song_name": song_name,
                                "artist_name": artist_name,
                                "source": current_track_info["source"],
                                "cache_status": "checking",
                                "cache_url": "",
                            }
                        )

                        print(
                            f"\n🎧 偵測到新歌 [{current_track_info['source']}]: {artist_name} - {song_name}"
                        )
                        debug_log(f"準備進入快取/搜尋流程，Song ID: {song_id}")

                        cached_data = get_cached_data(song_id)

                        if cached_data:
                            status_icon = {
                                "perfect": "✅",
                                "partial": "⚠️",
                                "not_found": "❌",
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
                                    "source": current_track_info["source"],
                                    "cache_status": cached_data.get(
                                        "status", "unknown"
                                    ),
                                    "cache_url": cached_data.get("url", ""),
                                }
                            )
                            open_in_dedicated_window(cached_data["url"])
                        else:
                            debug_log("快取未命中，開始啟動爬蟲搜尋 Utaten...")

                            def cache_and_open_url(url, status="unknown"):
                                debug_log(
                                    f"爬蟲回傳結果，寫入快取: status={status}, url={url}"
                                )
                                set_cached_data(
                                    song_id, url, status, artist_name, song_name
                                )
                                notify_track_update(
                                    {
                                        "song_id": song_id,
                                        "song_name": song_name,
                                        "artist_name": artist_name,
                                        "source": current_track_info["source"],
                                        "cache_status": status,
                                        "cache_url": url,
                                    }
                                )
                                open_in_dedicated_window(url)

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

            try:
                flush_pending_scroll()
            except Exception as e:
                debug_log(f"⚠️ 自動捲動發生錯誤: {e}")

            if stop_event:
                if stop_event.wait(MAIN_LOOP_SLEEP_SECONDS):
                    debug_log("停止事件於等待期間觸發，主迴圈結束。")
                    break
            else:
                time.sleep(MAIN_LOOP_SLEEP_SECONDS)
    finally:
        cleanup_playwright()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n🛑 收到終止訊號，正在關閉程式...")
        sys.exit(0)
