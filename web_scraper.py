import sys
import urllib.parse
from urllib.parse import urlparse

import requests
import jaconv
from bs4 import BeautifulSoup
from ddgs import DDGS


def normalize_utaten_url(url):
    if not url:
        return url
    return url.replace("https://www.utaten.com/", "https://utaten.com/")


def is_utaten_lyric_detail_url(url):
    if not url:
        return False

    normalized = normalize_utaten_url(url)
    parsed = urlparse(normalized)
    host = parsed.netloc.lower()
    path = parsed.path or ""

    if host not in {"utaten.com", "www.utaten.com"}:
        return False

    if not path.startswith("/lyric/"):
        return False

    # 排除列表頁、彙整頁等非單曲歌詞頁
    suffix = path[len("/lyric/") :].strip("/").lower()
    blocked_prefixes = {"newest", "ranking", "index", "tag", "special"}
    if not suffix or any(suffix.startswith(prefix) for prefix in blocked_prefixes):
        return False

    return True


def _normalize_compare_text(text):
    lowered = (text or "").strip().lower()
    return "".join(ch for ch in lowered if ch.isalnum())


def verify_utaten_page(url, target_artist, target_song):
    try:
        url = normalize_utaten_url(url)
        if not is_utaten_lyric_detail_url(url):
            return False, "", ""

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        response = requests.get(url, headers=headers, timeout=5)

        if response.status_code == 200:
            soup = BeautifulSoup(response.text, "html.parser")

            title_elem = soup.select_one(
                "h2.newLyricTitle__main, h2.lyricTitle__main, .newLyricTitle__main"
            )
            artist_elem = soup.select_one(
                "dt.newLyricWork__name, .newLyricWork__name, .lyricArtist__name"
            )
            lyric_root = soup.select_one(".lyricBody")

            page_title = title_elem.text.strip() if title_elem else ""
            page_artist = artist_elem.text.strip() if artist_elem else ""

            # 缺少核心資訊時直接視為驗證失敗，避免空字串造成誤判。
            if not page_title or not page_artist or lyric_root is None:
                return False, page_artist, page_title

            target_song_lower = _normalize_compare_text(target_song)
            target_artist_lower = _normalize_compare_text(target_artist)
            page_title_lower = _normalize_compare_text(page_title)
            page_artist_lower = _normalize_compare_text(page_artist)

            is_song_match = (target_song_lower in page_title_lower) or (
                page_title_lower in target_song_lower
            )

            # 若英/羅馬字拼音歌曲未完全吻合，嘗試轉換為片假名再度比對
            if not is_song_match:
                target_song_kana_lower = _normalize_compare_text(
                    jaconv.alphabet2kata(target_song.lower())
                )
                if target_song_kana_lower:
                    is_song_match = (target_song_kana_lower in page_title_lower) or (
                        page_title_lower in target_song_kana_lower
                    )

            is_artist_match = (target_artist_lower in page_artist_lower) or (
                page_artist_lower in target_artist_lower
            )

            # 若英/羅馬字拼音藝人未完全吻合，嘗試轉換為片假名再度比對 (如 YORUSHIKA -> ヨルシカ)
            if not is_artist_match:
                target_artist_kana_lower = _normalize_compare_text(
                    jaconv.alphabet2kata(target_artist.lower())
                )
                if target_artist_kana_lower:
                    is_artist_match = (
                        target_artist_kana_lower in page_artist_lower
                    ) or (page_artist_lower in target_artist_kana_lower)

            if is_song_match and is_artist_match:
                return True, page_artist, page_title
            return False, page_artist, page_title

    except Exception as e:
        print(f"  ⚠️ 驗證網頁發生錯誤: {e}")

    return False, "未知歌手", "未知歌曲"


def search_utaten(artist, song, open_url_callback):
    print(f"🎵 偵測到新歌！交給 DuckDuckGo 搜尋: {artist} - {song}")

    query = f"{artist} {song} utaten 歌詞"
    is_testing = not getattr(sys, "frozen", False)

    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, region="jp-jp", max_results=20))

            if is_testing:
                print("🔍 程式背後抓到的網址:")

            full_lyric_url = None
            first_utaten_link = None

            for i, r in enumerate(results):
                link = r.get("href", "")

                if is_testing:
                    print(f"  {i+1}: {link}")

                if "utaten.com/lyric/" in link:
                    link = normalize_utaten_url(link)
                    if not is_utaten_lyric_detail_url(link):
                        if is_testing:
                            print("  ⏭️ 跳過非單曲歌詞頁網址")
                        continue

                    if first_utaten_link is None:
                        first_utaten_link = link

                    if is_testing:
                        print("  ▶️ 正在驗證此網址的內容...")

                    is_match, page_artist, page_title = verify_utaten_page(
                        link, artist, song
                    )

                    if is_match:
                        full_lyric_url = link
                        if is_testing:
                            print(
                                f"  ✅ 驗證成功: 確實是 {page_artist} 的 {page_title}"
                            )
                        break
                    else:
                        if is_testing:
                            print(
                                f"  ⏭️ 驗證失敗: 網頁內容為 {page_artist} - {page_title}，跳過。"
                            )

            if full_lyric_url:
                print(f"✅ 找到完全吻合的 Utaten 網頁了！正在打開 {full_lyric_url}")
                open_url_callback(full_lyric_url, "perfect")  # 👈 新增狀態 "perfect"
            elif first_utaten_link:
                print("⚠️ 找不到名稱完全吻合的結果 (可能是羅馬音/日文差異)。")
                print("👉 信任搜尋引擎，為你開啟第一個 Utaten 結果當作備案！")
                open_url_callback(first_utaten_link, "partial")  # 👈 新增狀態 "partial"
            else:
                print("❌ 前 20 筆結果都沒有包含 Utaten 的網址。")
                safe_query = urllib.parse.quote(query)
                fallback_url = f"https://www.google.com/search?q={safe_query}"
                open_url_callback(fallback_url, "not_found")  # 👈 新增狀態 "not_found"

    except Exception as e:
        print(f"⚠️ 搜尋發生錯誤: {e}")
