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
