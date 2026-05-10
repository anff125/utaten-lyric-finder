import re
from thefuzz import fuzz

try:
    from pykakasi import kakasi

    _kakasi = kakasi()
    KAKASI_AVAILABLE = True
except Exception:
    _kakasi = None
    KAKASI_AVAILABLE = False


def normalize_match_text(text):
    text = text or ""
    text = text.lower()

    # 修正 1: 改為 \s，這樣才能正確清除所有空白與全形空白
    text = re.sub(r"[\s\u3000]", "", text)

    # 修正 2: 將減號 '-' 移到最後面，避免它被誤認為是字元範圍 (Range)
    # 同時把多餘的雙斜線修掉
    text = re.sub(r"[、。,.!！?？『』「」\\ー〜~…-]", "", text)

    return text


def normalize_to_hiragana(text):
    text = (text or "").strip()
    if not text:
        return ""
    if KAKASI_AVAILABLE and _kakasi is not None:
        try:
            converted = _kakasi.convert(text)
            return "".join(part.get("hira", "") for part in converted)
        except Exception:
            return text
    return text


def calculate_similarity(a, b):
    na = normalize_match_text(a)
    nb = normalize_match_text(b)
    if not na or not nb:
        return 0.0

    # 1. partial_ratio: 解決 Whisper 只聽到一半的問題 (例如只聽到「きみが」，歌詞是「きみがすきだよ」)
    partial_score = fuzz.partial_ratio(na, nb) / 100.0

    # 2. ratio: 解決 Whisper 聽錯少數假名的問題 (基於 Levenshtein 編輯距離)
    ratio_score = fuzz.ratio(na, nb) / 100.0

    # 回傳兩者中較高的分數
    return max(partial_score, ratio_score)


def calculate_ratio_similarity(a, b):
    na = normalize_match_text(a)
    nb = normalize_match_text(b)
    if not na or not nb:
        return 0.0

    # 迷失恢復模式僅使用 ratio，避免 partial_ratio 對片段誤命中過於寬鬆
    ratio_score = fuzz.ratio(na, nb) / 100.0
    return ratio_score


def find_best_match_line(transcribed_text, lyrics, last_line_index=-1):
    """
    Find the best matching line in the lyrics for the transcribed text.

    Args:
        transcribed_text (str): The text from speech-to-text.
        lyrics (list): A list of lyric lines.
        last_line_index (int): The index of the last matched line, to start searching from.

    Returns:
        tuple: (best_match_index, best_score) or (None, 0) if no good match is found.
    """
    best_score = 0
    best_match_index = -1

    # Start searching from the line after the last matched one
    start_index = last_line_index + 1

    # Use a higher threshold for better precision
    match_threshold = 85

    # Limit search to a window of lines to avoid jumping too far ahead
    search_window = min(len(lyrics), start_index + 10)

    for i in range(start_index, search_window):
        line = lyrics[i]
        # Use partial_ratio for better matching of substrings
        score = fuzz.partial_ratio(transcribed_text, line)

        if score > best_score:
            best_score = score
            best_match_index = i

    if best_score >= match_threshold:
        return best_match_index, best_score
    else:
        return None, 0


def find_best_match_line_with_lookahead(transcribed_text, lyrics, last_line_index=-1):
    """
    Find the best matching line in the lyrics for the transcribed text,
    with a lookahead to consider the next line.
    """
    # First, find the best single-line match
    best_match_index, best_score = find_best_match_line(
        transcribed_text, lyrics, last_line_index
    )

    if best_match_index is None:
        return None, 0

    # Now, check if combining with the next line gives a better score
    if best_match_index + 1 < len(lyrics):
        combined_line = lyrics[best_match_index] + lyrics[best_match_index + 1]
        combined_score = fuzz.partial_ratio(transcribed_text, combined_line)

        # If the combined score is significantly better, it might be a better match
        # This helps with short transcribed phrases that span two lines.
        if combined_score > best_score + 10:  # e.g., 10 points higher
            # We still return the original line index to start from there
            return best_match_index, combined_score

    return best_match_index, best_score
