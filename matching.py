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


def _length_coverage_ratio(source_text, target_text):
    if not target_text:
        return 0.0
    return min(len(source_text) / len(target_text), 1.0)


def calculate_similarity_normalized(na, nb, min_coverage_ratio=0.4):
    if not na or not nb:
        return 0.0

    coverage = _length_coverage_ratio(na, nb)

    # 1. partial_ratio: 解決 Whisper 只聽到一半的問題 (例如只聽到「きみが」，歌詞是「きみがすきだよ」)
    partial_score = fuzz.partial_ratio(na, nb) / 100.0
    if coverage < min_coverage_ratio:
        partial_score *= coverage / min_coverage_ratio

    # 2. ratio: 解決 Whisper 聽錯少數假名的問題 (基於 Levenshtein 編輯距離)
    ratio_score = fuzz.ratio(na, nb) / 100.0

    # 回傳兩者中較高的分數
    return max(partial_score, ratio_score)


def calculate_similarity(a, b, min_coverage_ratio=0.4):
    na = normalize_match_text(a)
    nb = normalize_match_text(b)
    return calculate_similarity_normalized(na, nb, min_coverage_ratio)


def calculate_ratio_similarity_normalized(na, nb):
    if not na or not nb:
        return 0.0

    # 迷失恢復模式僅使用 ratio，避免 partial_ratio 對片段誤命中過於寬鬆
    ratio_score = fuzz.ratio(na, nb) / 100.0
    return ratio_score


def calculate_ratio_similarity(a, b):
    na = normalize_match_text(a)
    nb = normalize_match_text(b)
    return calculate_ratio_similarity_normalized(na, nb)


class LyricsAgent:
    def __init__(self, min_coverage_ratio=0.4, max_buffer_ratio=1.8):
        self.min_coverage_ratio = min_coverage_ratio
        self.max_buffer_ratio = max_buffer_ratio
        self.asr_buffer = ""

    def reset(self):
        self.asr_buffer = ""

    def append(self, normalized_text):
        if normalized_text:
            self.asr_buffer += normalized_text

    def trim_if_needed(self, target_length):
        if target_length <= 0:
            return False

        max_length = int(target_length * self.max_buffer_ratio)
        if max_length <= 0:
            return False

        if len(self.asr_buffer) > max_length:
            drop = len(self.asr_buffer) // 2
            self.asr_buffer = self.asr_buffer[drop:]
            return True

        return False

    def score(self, target_normalized):
        return calculate_similarity_normalized(
            self.asr_buffer,
            target_normalized,
            self.min_coverage_ratio,
        )

    def ratio_score(self, target_normalized):
        return calculate_ratio_similarity_normalized(self.asr_buffer, target_normalized)
