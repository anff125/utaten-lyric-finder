import os
import re
import threading
import webbrowser
from typing import Any, Dict, List

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

from config import (
    FUZZY_LOOKAHEAD_LINES,
    FUZZY_NEXT_LINE_THRESHOLD,
    PLAYWRIGHT_NAV_TIMEOUT_MS,
    RECOVERY_LOOKAHEAD_LINES,  # 新增
    RECOVERY_THRESHOLD,  # 新增
)
from matching import calculate_similarity, normalize_match_text, normalize_to_hiragana
from matching import calculate_ratio_similarity
from web_scraper import is_utaten_lyric_detail_url, normalize_utaten_url

_playwright = None
_browser_context = None
_browser_page = None
_latest_lyrics: List[Dict[str, Any]] = []
_current_lyric_index = -1
_pending_scroll_targets: List[int] = []
_state_lock = threading.Lock()

LYRIC_ROOT_SELECTOR = ".lyricBody .hiragana, .lyricBody"


def _reset_playwright_state():
    global _playwright, _browser_context, _browser_page
    _browser_page = None
    _browser_context = None
    _playwright = None


def _dispose_browser_handles(stop_playwright=False):
    global _playwright, _browser_context, _browser_page

    try:
        if _browser_context is not None:
            _browser_context.close()
    except Exception:
        pass

    _browser_page = None
    _browser_context = None

    if stop_playwright:
        try:
            if _playwright is not None:
                _playwright.stop()
        except Exception:
            pass
        _playwright = None


def _get_or_create_playwright_page():
    global _playwright, _browser_context, _browser_page

    profile_path = os.path.abspath("./lyrics_browser_profile")
    os.makedirs(profile_path, exist_ok=True)

    if _browser_page:
        try:
            # 單靠 is_closed() 不夠，context 已關閉時仍可能回傳 False。
            _ = _browser_page.context.pages
            if not _browser_page.is_closed():
                print(f"🔄 重用已開啟的頁面 (is_closed={_browser_page.is_closed()})")
                return _browser_page
        except Exception:
            _browser_page = None
            _browser_context = None

    if _browser_context is not None:
        try:
            _ = _browser_context.pages
        except Exception:
            _reset_playwright_state()

    if _playwright is None:
        _playwright = sync_playwright().start()
        print("🎬 已啟動 Playwright")

    if _browser_context is None:
        try:
            _browser_context = _playwright.chromium.launch_persistent_context(
                user_data_dir=profile_path,
                channel="msedge",
                headless=False,
                no_viewport=True,  # 解決放大視窗留白的問題
            )
            print("🌐 已啟動 Edge (persistent context)")
        except Exception as e:
            print(f"⚠️  Edge 啟動失敗，改用 Chromium: {e}")
            _browser_context = _playwright.chromium.launch_persistent_context(
                user_data_dir=profile_path,
                headless=False,
                no_viewport=True,  # 解決放大視窗留白的問題
            )
            print("🌐 已啟動 Chromium (persistent context)")

    pages = _browser_context.pages
    _browser_page = pages[0] if pages else _browser_context.new_page()
    print(
        f"✅ 頁面已準備 (total_pages={len(_browser_context.pages)}, closed={_browser_page.is_closed()})"
    )
    return _browser_page


def extract_lyrics_from_html(html_text):
    soup = BeautifulSoup(html_text, "html.parser")
    lyrics_root = soup.select_one(".lyricBody .hiragana") or soup.select_one(
        ".lyricBody"
    )

    if lyrics_root is None:
        return []

    # 移除所有的 rt (注音/羅馬音)
    for rt in lyrics_root.select("span.rt"):
        rt.decompose()

    for br in lyrics_root.find_all("br"):
        br.replace_with("\n")

    text = lyrics_root.get_text(separator="", strip=False)
    lines = []
    for raw_line in text.splitlines():
        line = re.sub(r"\s+", "", raw_line)
        if line:
            lines.append(line)

    return lines


def extract_lyrics_from_current_page(page):
    lines: List[Dict[str, Any]] = []

    paragraph_locator = page.locator(".lyricBody .hiragana > p, .lyricBody > p")
    paragraph_count = paragraph_locator.count()

    # 如果找不到 <p> 標籤，代表這是只有 <br> 換行的版型
    if paragraph_count == 0:
        # 動態注入 JS：將被 <br> 切開的文字片段包裝進 <span class="lyric-line"> 中
        page.evaluate(
            """() => {
            const root = document.querySelector('.lyricBody .hiragana') || document.querySelector('.lyricBody');
            if (root) {
                const lines = root.innerHTML.split(/<br\\s*\\/?>/gi);
                root.innerHTML = lines.map(line => `<span class="lyric-line">${line}</span>`).join('<br/>');
            }
        }"""
        )
        # 將定位器改為我們自己生成的 span
        paragraph_locator = page.locator(
            ".lyricBody .hiragana span.lyric-line, .lyricBody span.lyric-line"
        )
        paragraph_count = paragraph_locator.count()

    if paragraph_count > 0:
        for idx in range(paragraph_count):
            p = paragraph_locator.nth(idx)

            # 使用 JS 複製節點，拔除所有 rt (注音/羅馬音) 後取得純文字
            # 這樣可以確保不漏抓任何沒被 ruby 包住的字，同時也不會混入拼音
            line_text = p.evaluate(
                """(el) => {
                let clone = el.cloneNode(true);
                let rts = clone.querySelectorAll('.rt');
                rts.forEach(rt => rt.remove());
                return clone.textContent || "";
            }"""
            )

            line_text = re.sub(r"\s+", "", line_text)
            if not line_text:
                continue

            normalized = normalize_match_text(normalize_to_hiragana(line_text))
            if normalized:
                lines.append(
                    {
                        "original": line_text,
                        "normalized": normalized,
                        "locator_index": idx,
                    }
                )
        return lines

    # 最壞情況的 Fallback
    html_text = page.content()
    fallback_lines = extract_lyrics_from_html(html_text)
    for idx, line_text in enumerate(fallback_lines):
        lines.append(
            {
                "original": line_text,
                "normalized": normalize_match_text(normalize_to_hiragana(line_text)),
                "locator_index": idx,
            }
        )
    return lines


def scroll_to_lyric_locator(page, locator_index):
    # 同時支援原生的 <p> 以及我們剛才動態注入的 <span class="lyric-line">
    target = page.locator(
        ".lyricBody .hiragana > p, .lyricBody > p, .lyricBody .hiragana span.lyric-line, .lyricBody span.lyric-line"
    ).nth(locator_index)
    if target.count() == 0:
        return False

    # 改用 block: 'center' 讓目標歌詞保持在畫面正中央
    target.evaluate("el => el.scrollIntoView({ behavior: 'smooth', block: 'center' })")
    return True


def open_in_dedicated_window(url):
    global _latest_lyrics, _current_lyric_index, _pending_scroll_targets

    try:
        url = normalize_utaten_url(url)
        page = _get_or_create_playwright_page()

        print(f"📄 正在導航至: {url}")
        try:
            page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=PLAYWRIGHT_NAV_TIMEOUT_MS,
            )
        except Exception as nav_error:
            # 使用者手動關閉瀏覽器後，舊 page/context 可能仍殘留在記憶體，重建一次再試。
            print(f"⚠️ 偵測到頁面失效，嘗試重建瀏覽器: {nav_error}")
            _dispose_browser_handles(stop_playwright=False)
            page = _get_or_create_playwright_page()
            page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=PLAYWRIGHT_NAV_TIMEOUT_MS,
            )
        print(f"✅ 頁面已加載，URL: {page.url}")

        if is_utaten_lyric_detail_url(url):
            try:
                page.wait_for_selector(
                    LYRIC_ROOT_SELECTOR, timeout=PLAYWRIGHT_NAV_TIMEOUT_MS
                )
            except Exception as wait_error:
                print(f"⚠️ 歌詞區塊等待逾時，略過自動捲動初始化: {wait_error}")
                return

            with _state_lock:
                _latest_lyrics = extract_lyrics_from_current_page(page)
                _current_lyric_index = -1
                _pending_scroll_targets = []
            print(f"📝 已解析歌詞行數: {len(_latest_lyrics)}")
            if _latest_lyrics:
                preview = " | ".join(line["original"] for line in _latest_lyrics[:3])
                print(f"📝 歌詞預覽(前三行): {preview}")
                first_line_scrolled = scroll_to_lyric_locator(page, 0)
                if first_line_scrolled:
                    print("🎯 已自動定位到第一句歌詞")
                else:
                    print("⚠️ 無法定位到第一句歌詞")
            print("🖼️  瀏覽器視窗應已開啟，等待聲音識別觸發自動滾動...")
    except Exception as e:
        print(f"⚠️ Playwright 開啟失敗，退回預設方式: {e}")
        webbrowser.open(url)


def queue_scroll_when_next_line_matches(recognized_text):
    global _current_lyric_index, _pending_scroll_targets

    recognized_text = (recognized_text or "").strip()
    if not recognized_text:
        return

    normalized_recognized = normalize_match_text(normalize_to_hiragana(recognized_text))
    if not normalized_recognized:
        return

    with _state_lock:
        if not _latest_lyrics:
            return

        start_index = _current_lyric_index + 1
        if start_index >= len(_latest_lyrics):
            return

        # ==========================================
        # 階段一：常規推進 (搜尋接下來的 4 行)
        # ==========================================
        end_index_phase1 = min(
            start_index + FUZZY_LOOKAHEAD_LINES, len(_latest_lyrics) - 1
        )
        best_index = None
        best_score = 0.0

        for idx in range(start_index, end_index_phase1 + 1):
            target_normalized = _latest_lyrics[idx]["normalized"]
            score = calculate_similarity(normalized_recognized, target_normalized)

            if score > best_score:
                best_score = score
                best_index = idx

        # ==========================================
        # 階段二：迷失恢復機制 (如果階段一沒過門檻，擴大搜尋範圍)
        # ==========================================
        is_recovery = False
        if best_score < FUZZY_NEXT_LINE_THRESHOLD:
            start_index_phase2 = end_index_phase1 + 1
            end_index_phase2 = min(
                _current_lyric_index + RECOVERY_LOOKAHEAD_LINES, len(_latest_lyrics) - 1
            )

            if start_index_phase2 <= end_index_phase2:
                for idx in range(start_index_phase2, end_index_phase2 + 1):
                    target_normalized = _latest_lyrics[idx]["normalized"]
                    score = calculate_ratio_similarity(
                        normalized_recognized, target_normalized
                    )

                    # 迷失恢復需要嚴格的匹配分數 (RECOVERY_THRESHOLD)
                    if score > best_score and score >= RECOVERY_THRESHOLD:
                        best_score = score
                        best_index = idx
                        is_recovery = True

        # ==========================================
        # 最終判定與執行捲動
        # ==========================================
        threshold_to_pass = (
            RECOVERY_THRESHOLD if is_recovery else FUZZY_NEXT_LINE_THRESHOLD
        )

        if best_index is None or best_score < threshold_to_pass:
            return

        # 🎯 印出結果
        target_original = _latest_lyrics[best_index]["original"]
        target_normalized = _latest_lyrics[best_index]["normalized"]

        print(
            f"\n🎤 [語音識別] 原句: '{recognized_text}' (正規化: '{normalized_recognized}')"
        )
        print(
            f"🎯 [比對成功] 歌詞: '{target_original}' (正規化: '{target_normalized}', 分數: {best_score:.2f})"
        )

        if is_recovery:
            print(f"⚠️ [迷失恢復] 成功跨越較大段落，重新定位到第 {best_index + 1} 行")

        start_queue_index = max(0, _current_lyric_index + 1)
        for idx in range(start_queue_index, best_index + 1):
            _pending_scroll_targets.append(_latest_lyrics[idx]["locator_index"])
        _current_lyric_index = best_index


def flush_pending_scroll():
    global _pending_scroll_targets

    with _state_lock:
        target_indices = list(_pending_scroll_targets)
        _pending_scroll_targets = []

    if not target_indices:
        return

    page = _browser_page
    if page is None:
        print("⚠️ 頁面對象為 None，無法捲動")
        return

    if page.is_closed():
        print("⚠️ 頁面已關閉 (page.is_closed()=True)，無法捲動")
        return

    try:
        for locator_index in target_indices:
            scrolled = scroll_to_lyric_locator(page, locator_index)
            if not scrolled:
                print(f"⚠️ 找不到對應歌詞定位 locator_index={locator_index}")
    except Exception as e:
        print(f"⚠️ 捲動失敗: {e}")


def cleanup_playwright():
    print("\n🛑 收到停止要求，正在清理瀏覽器資源...")
    try:
        _dispose_browser_handles(stop_playwright=True)
        print("✅ 清理完成，程式安全結束。")
    except Exception as e:
        print(f"⚠️ 清理時發生錯誤 (可忽略): {e}")
