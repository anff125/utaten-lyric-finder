# system_media.py
import asyncio
import concurrent.futures
import platform
import re
import subprocess

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
    if platform.system() == "Windows":
        print("⚠️ 找不到 winrt 套件，無法讀取系統媒體 (SMTC) 資訊。")


LINUX_PLAYERCTL_WARNING_SHOWN = False
LINUX_PLAYERCTL_ACCESS_DENIED_WARNING_SHOWN = False


def _is_access_denied_error(message):
    text = (message or "").lower()
    return (
        "accessdenied" in text
        or "access denied" in text
        or "apparmor" in text
        or "org.freedesktop.dbus.error.accessdenied" in text
    )


def _warn_linux_playerctl_access_denied_once():
    global LINUX_PLAYERCTL_ACCESS_DENIED_WARNING_SHOWN
    if LINUX_PLAYERCTL_ACCESS_DENIED_WARNING_SHOWN:
        return

    print("⚠️ 偵測到 Linux 媒體權限被拒絕 (AppArmor/snap)。")
    print("   目前無法讀取 Firefox/Spotify 的 MPRIS 資訊。")
    print("   建議改用非 snap 版本的播放器，或改用 Spotify API 模式。")
    LINUX_PLAYERCTL_ACCESS_DENIED_WARNING_SHOWN = True


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


def _get_linux_media_info():
    """使用 playerctl 讀取 Linux (MPRIS) 正在播放的媒體資訊。"""
    global LINUX_PLAYERCTL_WARNING_SHOWN

    try:
        players_result = subprocess.run(
            ["playerctl", "--list-all"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        if not LINUX_PLAYERCTL_WARNING_SHOWN:
            print(
                "⚠️ 找不到 playerctl，Linux 系統媒體模式需要先安裝: sudo apt install playerctl"
            )
            LINUX_PLAYERCTL_WARNING_SHOWN = True
        return None
    except Exception as e:
        debug_log(f"Linux playerctl 執行失敗: {e}")
        return None

    if players_result.returncode != 0:
        stderr_msg = (players_result.stderr or "").strip()
        if stderr_msg:
            debug_log(f"playerctl --list-all 回傳非 0: {stderr_msg}")
        return None

    players = []
    seen = set()
    for line in (players_result.stdout or "").splitlines():
        player = (line or "").strip()
        if not player or player in seen:
            continue
        seen.add(player)
        players.append(player)

    if not players:
        return None

    # 優先挑選 Playing，找不到就回退第一筆可用資料。
    chosen = None
    fallback = None
    access_denied_detected = False

    for player_name in players:
        status_result = subprocess.run(
            ["playerctl", "-p", player_name, "status"],
            capture_output=True,
            text=True,
            check=False,
        )
        if status_result.returncode != 0:
            status_err = (status_result.stderr or "").strip()
            if status_err:
                debug_log(f"player '{player_name}' status 失敗: {status_err}")
            if _is_access_denied_error(status_err):
                access_denied_detected = True
            continue

        metadata_result = subprocess.run(
            [
                "playerctl",
                "-p",
                player_name,
                "metadata",
                "--format",
                "{{artist}}|||{{title}}",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if metadata_result.returncode != 0:
            metadata_err = (metadata_result.stderr or "").strip()
            if metadata_err:
                debug_log(f"player '{player_name}' metadata 失敗: {metadata_err}")
            if _is_access_denied_error(metadata_err):
                access_denied_detected = True
            continue

        metadata_text = (metadata_result.stdout or "").strip()
        parts = metadata_text.split("|||", 1)
        if len(parts) < 2:
            continue

        artist, title = parts
        title = clean_youtube_title(title)
        artist = clean_artist_name(artist)
        status = (status_result.stdout or "").strip()
        status_norm = (status or "").strip().lower()

        if not title:
            continue

        info = {
            "player": (player_name or "").strip(),
            "status": status_norm,
            "artist": artist if artist else "Unknown",
            "title": title,
        }

        if _looks_like_lyrics_page(info["title"], info["artist"]):
            return {"ignored": True, "reason": "lyrics_page"}

        if fallback is None:
            fallback = info
        if status_norm == "playing":
            chosen = info
            break

    selected = chosen or fallback
    if not selected:
        if access_denied_detected:
            _warn_linux_playerctl_access_denied_once()
        return None

    if chosen is None and selected.get("status") != "playing":
        debug_log(
            f"Linux 未找到 Playing 狀態，回退使用 {selected.get('player', 'unknown')} 的最後一筆媒體資訊。"
        )

    return {"title": selected["title"], "artist": selected["artist"]}


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
    current_os = platform.system()

    if current_os == "Linux":
        return _get_linux_media_info()

    if current_os != "Windows":
        return None

    try:
        # 使用 ThreadPoolExecutor 將非同步任務放到獨立執行緒執行
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_sync_runner)
            return future.result()
    except Exception as e:
        debug_log(f"發生例外錯誤: {e}")
        return None
