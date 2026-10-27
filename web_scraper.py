import re
import sys
import urllib.parse
from urllib.parse import urlparse

import jaconv
import requests
from bs4 import BeautifulSoup
from ddgs import DDGS
from thefuzz import fuzz

try:
    from pykakasi import kakasi

    _kakasi = kakasi()
except Exception:
    _kakasi = None

_HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}


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


def extract_base_song_title(song):
    """
    移除常見的演唱會、版本、重製或出處後綴，例如：
    - 'Kaiju no Hanauta - DOME TOUR 2026 ”SILENCE”' -> 'Kaiju no Hanauta'
    - '丸ノ内サディスティック - Just can\'t help it.より' -> '丸ノ内サディスティック'
    - '裸の勇者(TVアニメ「王様ランキング」第2クールオープニングテーマ)' -> '裸の勇者'
    """
    s = (song or "").strip()
    if not s:
        return ""

    # 移除 ' - ...' 形式的後綴（注意保留前後空白條件，避免切斷字詞內的連字號）
    s = re.split(r"\s+[-–—―]\s+", s, maxsplit=1)[0].strip()

    # 移除尾端括號附加資訊（若移除後主標題仍非空）
    s_no_bracket = re.sub(
        r"\s*[\(（\[［【〜～][^\)）\]］】〜～]*[\)）\]］】〜～]\s*$", "", s
    ).strip()
    return s_no_bracket or s


def _normalize_compare_text(text):
    lowered = (text or "").strip().lower()
    return "".join(ch for ch in lowered if ch.isalnum())


def _to_hiragana(text):
    s = (text or "").strip()
    if not s:
        return ""
    if _kakasi is not None:
        try:
            converted = _kakasi.convert(s)
            s = "".join(part.get("hira", "") for part in converted)
        except Exception:
            pass
    s = jaconv.kata2hira(s)
    return s


def _romaji_to_hiragana(text):
    s = (text or "").strip().lower()
    if not s:
        return ""
    # 先將常見英語地名/長音拼法轉為標準羅馬字以便 jaconv 轉換平假名
    s = re.sub(r"\btokyo\b", "toukyou", s)
    s = re.sub(r"\bosaka\b", "oosaka", s)
    s = re.sub(r"\s+", "", s)
    return jaconv.kata2hira(jaconv.alphabet2kana(s))


def _to_phonetic_romaji(text):
    """
    將日文（漢字/平假名/片假名）或羅馬字統一轉換為簡化音韻羅馬字：
    - 統一訓令式與平文式拼法 (si->shi, ti->chi, tu->tsu, syu->shu, etc.)
    - 統一 l -> r
    - 折疊平文式常省略的長音 (ou/oo -> o, uu -> u 等) 與促音重複子音
    """
    s = _to_hiragana(text).lower().strip()
    if not s:
        return ""
    s = jaconv.kana2alphabet(s)
    s = re.sub(r"[^a-z0-9]", "", s)
    for k, v in (
        ("si", "shi"),
        ("ti", "chi"),
        ("tu", "tsu"),
        ("zi", "ji"),
        ("sya", "sha"),
        ("syu", "shu"),
        ("syo", "sho"),
        ("tya", "cha"),
        ("tyu", "chu"),
        ("tyo", "cho"),
        ("zya", "ja"),
        ("zyu", "ju"),
        ("zyo", "jo"),
    ):
        s = s.replace(k, v)
    s = re.sub(r"(?<![sc])hu", "fu", s)
    s = s.replace("l", "r")
    s = re.sub(r"o[uo]+", "o", s)
    s = re.sub(r"u+", "u", s)
    s = re.sub(r"a+", "a", s)
    s = re.sub(r"i+", "i", s)
    s = re.sub(r"e+", "e", s)
    s = re.sub(r"([bcdfghjkmnpqrstvwxyz])\1+", r"\1", s)
    return s


def _extract_clean_page_title(title_elem):
    if title_elem is None:
        return ""
    # 複製節點內容或排除 UtaTen 附加的「歌詞」與副標 span
    clone = BeautifulSoup(str(title_elem), "html.parser")
    for extra in clone.select(
        ".newLyricTitle_afterTxt, .newLyricTitle__subTitle, .lyricTitle_afterTxt, .lyricTitle__subTitle"
    ):
        extra.decompose()
    cleaned = clone.get_text(" ", strip=True)
    cleaned = re.sub(r"\s*歌詞\s*$", "", cleaned).strip()
    return cleaned or title_elem.get_text(" ", strip=True)


def _extract_page_kana(soup):
    kana_elem = soup.select_one(".newLyricTitle__kana, .lyricTitle__kana")
    if not kana_elem:
        return ""
    raw = kana_elem.get_text(strip=True)
    return re.sub(r"^(よみ|読み)\s*[：:]\s*", "", raw).strip()


def _is_text_or_phonetic_match(target_text, page_texts, allow_fuzzy_phonetic=False):
    target_variants = []
    for candidate in (target_text, extract_base_song_title(target_text)):
        c = (candidate or "").strip()
        if c and c not in target_variants:
            target_variants.append(c)

    valid_page_texts = []
    for pt in page_texts:
        for candidate in (pt, extract_base_song_title(pt)):
            c = (candidate or "").strip()
            if c and c not in valid_page_texts:
                valid_page_texts.append(c)

    if not target_variants or not valid_page_texts:
        return False

    for t_str in target_variants:
        t_norm = _normalize_compare_text(t_str)
        t_kata = _normalize_compare_text(jaconv.alphabet2kata(t_str.lower()))
        t_hira = _normalize_compare_text(_romaji_to_hiragana(t_str))
        t_rom = _to_phonetic_romaji(t_str)

        for p_str in valid_page_texts:
            p_norm = _normalize_compare_text(p_str)
            if t_norm and p_norm and (t_norm in p_norm or p_norm in t_norm):
                return True

            if t_kata and p_norm and (t_kata in p_norm or p_norm in t_kata):
                return True

            p_hira = _normalize_compare_text(jaconv.kata2hira(p_str))
            if t_hira and p_hira and (t_hira in p_hira or p_hira in t_hira):
                return True

            p_rom = _to_phonetic_romaji(p_str)
            if t_rom and p_rom:
                if t_rom == p_rom and len(t_rom) >= 2:
                    return True
                if min(len(t_rom), len(p_rom)) >= 4 and (
                    t_rom in p_rom or p_rom in t_rom
                ):
                    return True
                if (
                    allow_fuzzy_phonetic
                    and min(len(t_rom), len(p_rom)) >= 6
                    and fuzz.ratio(t_rom, p_rom) >= 88
                ):
                    return True

    return False


def verify_utaten_page(url, target_artist, target_song, artist_verified=False):
    try:
        url = normalize_utaten_url(url)
        if not is_utaten_lyric_detail_url(url):
            return False, "", ""

        response = requests.get(url, headers=_HTTP_HEADERS, timeout=5)

        if response.status_code == 200:
            soup = BeautifulSoup(response.text, "html.parser")

            title_elem = soup.select_one(
                "h2.newLyricTitle__main, h2.lyricTitle__main, .newLyricTitle__main"
            )
            artist_elem = soup.select_one(
                "dt.newLyricWork__name, .newLyricWork__name, .lyricArtist__name"
            )
            lyric_root = soup.select_one(".lyricBody")

            page_title = _extract_clean_page_title(title_elem)
            page_kana = _extract_page_kana(soup)
            page_artist = artist_elem.text.strip() if artist_elem else ""

            # 缺少核心資訊時直接視為驗證失敗，避免空字串造成誤判。
            if not page_title or not page_artist or lyric_root is None:
                return False, page_artist, page_title

            is_song_match = _is_text_or_phonetic_match(
                target_song,
                [page_title, page_kana],
                allow_fuzzy_phonetic=True,
            )

            is_artist_match = artist_verified or _is_text_or_phonetic_match(
                target_artist,
                [page_artist],
                allow_fuzzy_phonetic=False,
            )

            if is_song_match and is_artist_match:
                return True, page_artist, page_title
            return False, page_artist, page_title

    except Exception as e:
        print(f"  ⚠️ 驗證網頁發生錯誤: {e}")

    return False, "未知歌手", "未知歌曲"


def _fetch_utaten_search_links(artist_name="", title="", sort=""):
    params = {
        "artist_name": artist_name or "",
        "title": title or "",
    }
    if sort:
        params["sort"] = sort

    response = requests.get(
        "https://utaten.com/search",
        params=params,
        headers=_HTTP_HEADERS,
        timeout=5,
    )
    if response.status_code != 200:
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    results = []
    seen = set()
    for a in soup.select("p.searchResult__title a"):
        href = a.get("href", "").strip()
        if not href:
            continue
        full_url = urllib.parse.urljoin("https://utaten.com", href)
        full_url = normalize_utaten_url(full_url)
        if not is_utaten_lyric_detail_url(full_url) or full_url in seen:
            continue
        seen.add(full_url)
        row_title = a.get_text(strip=True)
        results.append((full_url, row_title))
    return results


def _search_utaten_native(artist, song, is_testing=False):
    """
    使用 UtaTen 官方站內搜尋 (https://utaten.com/search) 尋找歌詞頁：
    1. 支援去除演唱會/版本後綴後的曲名查詢
    2. 支援羅馬字曲名轉平假名（及前綴容錯，處理平文式省略長音如 Fukakoryoku -> ふかこ）
    3. 支援 UtaTen 官方藝人關聯（如搜尋 東京事変 自動涵蓋 椎名林檎 的 丸ノ内サディスティック）
    4. 若為英/羅馬字混合曲名（如 Tokyo Flash -> 東京フラッシュ），從該歌手熱門曲目清單以音韻相似度篩選候選
    回傳: (perfect_url, first_candidate_url)
    """
    base_song = extract_base_song_title(song)
    title_queries = []
    if base_song:
        title_queries.append(base_song)
    elif (song or "").strip():
        title_queries.append(song.strip())

    # 若曲名主要為英數/羅馬字，加入平假名轉換與前綴查詢
    if base_song and re.search(r"[a-zA-Z]", base_song):
        hira_full = _romaji_to_hiragana(base_song)
        if hira_full and hira_full not in title_queries:
            title_queries.append(hira_full)
        if len(hira_full) >= 3 and hira_full[:3] not in title_queries:
            title_queries.append(hira_full[:3])

    first_candidate_url = None
    checked_urls = set()

    for t_query in title_queries:
        if is_testing:
            print(f"🔎 UtaTen 站內搜尋: artist='{artist}', title='{t_query}'")
        rows = _fetch_utaten_search_links(artist_name=artist, title=t_query)
        # 只有當查詢詞是完整曲名或完整平假名（非 3 字截斷前綴）時，才將第一筆結果作為 partial 備案
        is_full_query = t_query in {base_song, (song or "").strip()}
        for link, row_title in rows[:5]:
            if is_full_query and first_candidate_url is None:
                first_candidate_url = link
            if link in checked_urls:
                continue
            checked_urls.add(link)

            if is_testing:
                print(f"  ▶️ 驗證站內結果: {row_title} ({link})")
            is_match, page_artist, page_title = verify_utaten_page(
                link, artist, song, artist_verified=bool(artist)
            )
            if is_match:
                if is_testing:
                    print(f"  ✅ 站內驗證成功: {page_artist} - {page_title}")
                return link, first_candidate_url

    # 針對羅馬字與外來語混合曲名（例如 Tokyo Flash -> 東京フラッシュ），查詢歌手熱門曲目並以音韻分數篩選
    if artist and base_song and re.search(r"[a-zA-Z]", base_song):
        target_rom = _to_phonetic_romaji(base_song)
        if len(target_rom) >= 4:
            artist_rows = _fetch_utaten_search_links(
                artist_name=artist, title="", sort="popular_sort_asc"
            )
            scored_candidates = []
            for link, row_title in artist_rows:
                if link in checked_urls:
                    continue
                row_base = extract_base_song_title(row_title)
                row_rom = _to_phonetic_romaji(row_base)
                if not row_rom:
                    continue
                score = fuzz.ratio(target_rom, row_rom)
                if (
                    score >= 75
                    or target_rom in row_rom
                    or row_rom in target_rom
                    or (len(target_rom) >= 5 and row_rom[:4] == target_rom[:4])
                ):
                    scored_candidates.append((score, link, row_title))

            scored_candidates.sort(key=lambda x: x[0], reverse=True)
            for _score, link, row_title in scored_candidates[:3]:
                checked_urls.add(link)
                if is_testing:
                    print(f"  ▶️ 驗證歌手熱門曲目候選: {row_title} ({link})")
                is_match, page_artist, page_title = verify_utaten_page(
                    link, artist, song, artist_verified=True
                )
                if is_match:
                    if is_testing:
                        print(f"  ✅ 站內驗證成功: {page_artist} - {page_title}")
                    return link, first_candidate_url

    return None, first_candidate_url


def search_utaten(artist, song, open_url_callback):
    print(f"🎵 偵測到新歌！開始搜尋歌詞: {artist} - {song}")

    base_song = extract_base_song_title(song)
    search_queries = []
    for s_variant in (base_song, (song or "").strip()):
        if not s_variant:
            continue
        q1 = f"{artist} {s_variant} utaten 歌詞"
        q2 = f"site:utaten.com {artist} {s_variant}"
        if q1 not in search_queries:
            search_queries.append(q1)
        if q2 not in search_queries:
            search_queries.append(q2)

    is_testing = not getattr(sys, "frozen", False)

    try:
        full_lyric_url = None
        first_utaten_link = None

        # 1. 優先透過 UtaTen 站內搜尋（速度快、支援讀音與官方樂團/主唱關聯）
        try:
            full_lyric_url, native_first_link = _search_utaten_native(
                artist, song, is_testing=is_testing
            )
            if native_first_link:
                first_utaten_link = native_first_link
        except Exception as e:
            if is_testing:
                print(f"⚠️ UtaTen 站內搜尋發生例外，改用搜尋引擎: {e}")

        # 2. 若站內搜尋未完全命中，使用 DuckDuckGo 搜尋
        if not full_lyric_url:
            checked_ddgs_urls = set()
            with DDGS() as ddgs:
                for q_idx, query in enumerate(search_queries):
                    if q_idx > 0:
                        print(f"🔄 嘗試下一組搜尋語法: {query}")
                    results = list(ddgs.text(query, region="jp-jp", max_results=20))

                    if is_testing:
                        print(f"🔍 程式背後抓到的網址 (查詢: {query}):")

                    for i, r in enumerate(results):
                        link = r.get("href", "")

                        if is_testing:
                            print(f"  {i + 1}: {link}")

                        if "utaten.com/lyric/" in link:
                            link = normalize_utaten_url(link)
                            if not is_utaten_lyric_detail_url(link):
                                if is_testing:
                                    print("  ⏭️ 跳過非單曲歌詞頁網址")
                                continue

                            if first_utaten_link is None:
                                first_utaten_link = link

                            if link in checked_ddgs_urls:
                                continue
                            checked_ddgs_urls.add(link)

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
                        break

        if full_lyric_url:
            print(f"✅ 找到完全吻合的 Utaten 網頁了！正在打開 {full_lyric_url}")
            open_url_callback(full_lyric_url, "perfect")  # 👈 新增狀態 "perfect"
        elif first_utaten_link:
            print("⚠️ 找不到名稱完全吻合的結果 (可能是羅馬音/日文差異)。")
            print("👉 信任搜尋引擎，為你開啟第一個 Utaten 結果當作備案！")
            open_url_callback(first_utaten_link, "partial")  # 👈 新增狀態 "partial"
        else:
            print("❌ 嘗試搜尋都沒有包含 Utaten 的網址。")
            primary_query = (
                search_queries[0] if search_queries else f"{artist} {song} utaten 歌詞"
            )
            safe_query = urllib.parse.quote(primary_query)
            fallback_url = f"https://www.google.com/search?q={safe_query}"
            open_url_callback(fallback_url, "not_found")  # 👈 新增狀態 "not_found"

    except Exception as e:
        print(f"⚠️ 搜尋發生錯誤: {e}")
