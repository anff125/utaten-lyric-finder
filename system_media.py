# system_media.py
import asyncio
import re
import concurrent.futures

# 👇 除錯模式開關
DEBUG = False


def debug_log(msg):
    if DEBUG:
        print(f"[DEBUG-Media] {msg}")


try:
    from winrt.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager,
    )

    WINRT_AVAILABLE = True
except ImportError:
    WINRT_AVAILABLE = False
    print("⚠️ 找不到 winrt 套件，無法讀取系統媒體 (YouTube) 資訊。")


def clean_youtube_title(title):
    if not title:
        return ""
    cleaned = re.sub(
        r"[\(\[\【].*?(Official|Video|MV|Lyric|Audio|Music).*?[\)\]\】]",
        "",
        title,
        flags=re.IGNORECASE,
    )
    return cleaned.strip()


def clean_artist_name(artist):
    if not artist:
        return ""
    return artist.replace(" - Topic", "").strip()


def _looks_like_lyrics_page(title, artist):
    normalized_title = (title or "").strip().lower()
    normalized_artist = (artist or "").strip().lower()

    if not normalized_title:
        return False

    lyrics_markers = ("utaten", "うたてん", "歌詞", "lyrics", "lyric")
    if any(marker in normalized_title for marker in lyrics_markers):
        return True

    if normalized_artist == "unknown" and normalized_title.startswith("unknown - "):
        return True

    return False


async def _get_media_info_async():
    if not WINRT_AVAILABLE:
        return None

    sessions = await GlobalSystemMediaTransportControlsSessionManager.request_async()
    current_session = sessions.get_current_session()

    if current_session:
        # 取得播放狀態 (5 = Playing, 4 = Paused, 3 = Stopped)
        playback_info = current_session.get_playback_info()
        status_code = playback_info.playback_status

        # 只有在狀態改變或抓取時，可以印出目前的狀態碼來除錯
        # debug_log(f"目前系統媒體狀態碼: {status_code} (5=Playing, 4=Paused)")

        if status_code != 4:
            debug_log(f"系統媒體未處於播放狀態 (狀態碼: {status_code})，略過處理。")
            return None

        info = await current_session.try_get_media_properties_async()

        debug_log(f"抓取到原始資訊 -> 標題: '{info.title}', 頻道: '{info.artist}'")

        title = clean_youtube_title(info.title)
        artist = clean_artist_name(info.artist)

        debug_log(f"清理後準備搜尋 -> 標題: '{title}', 頻道: '{artist}'")

        if _looks_like_lyrics_page(title, artist):
            debug_log("偵測到歌詞頁或自動開啟的 Utaten 分頁，略過本次媒體資訊。")
            return {"ignored": True, "reason": "lyrics_page"}

        if title:
            return {"title": title, "artist": artist if artist else "Unknown"}
    else:
        debug_log("找不到任何系統媒體工作階段 (瀏覽器可能沒發出媒體訊號)。")

    return None


def _sync_runner():
    """在獨立的執行緒中建立全新的 Event Loop，避免與 Playwright 衝突"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(_get_media_info_async())
    finally:
        loop.close()


def get_system_media_info():
    """同步封裝，供主迴圈直接呼叫"""
    try:
        # 使用 ThreadPoolExecutor 將非同步任務放到獨立執行緒執行
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_sync_runner)
            return future.result()
    except Exception as e:
        debug_log(f"發生例外錯誤: {e}")
        return None
