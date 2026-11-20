"""Japanese singing phonological normalization, weighted edit distance, and continuous timeline alignment."""

from dataclasses import dataclass
import re
from typing import Any

from rapidfuzz import fuzz

_EXTRA_PUNCT_RE = re.compile(r"[・･（）()\[\]【】\"'“”‘’♪♫☆★]")
_SMALL_VOWEL_MAP = str.maketrans("ぁぃぅぇぉ", "あいうえお")
_HOMOPHONE_MAP = str.maketrans("をぢづゐゑ", "おじずいえ")
_PARTICLE_MAP = str.maketrans("はへ", "わえ")

DAKUTEN_BASE_MAP: dict[str, str] = dict(
    zip(
        "がぎぐげござじずぜぞだぢづでどばびぶべぼぱぴぷぺぽゔ",
        "かきくけこさしすせそたちつてとはひふへほはひふへほう",
    )
)
_DAKUTEN_TRANSLATION = str.maketrans(DAKUTEN_BASE_MAP)

_A_COL = frozenset("あかさたなはまやらわがざだばぱゃ")
_I_COL = frozenset("いきしちにひみりぎじぢびぴ")
_U_COL = frozenset("うくすつぬふむゆるぐずづぶぷゅゔ")
_E_COL = frozenset("えけせてねへめれげぜでべぺ")
_O_COL = frozenset("おこそとのほもよろをごぞどぼぽょ")


def collapse_japanese_long_vowels(text: str, *, include_ei: bool = False) -> str:
    """Collapse consecutive same-row vowel elongations (e.g. おう/おお -> お)."""
    if not text:
        return ""
    out = [text[0]]
    for ch in text[1:]:
        prev = out[-1]
        if ch == "あ" and prev in _A_COL:
            continue
        if ch == "い" and (prev in _I_COL or (include_ei and prev in _E_COL)):
            continue
        if ch == "う" and (prev in _U_COL or prev in _O_COL):
            continue
        if ch == "え" and prev in _E_COL:
            continue
        if ch in ("お", "を") and prev in _O_COL:
            continue
        out.append(ch)
    return "".join(out)


def strip_dakuten(text: str) -> str:
    """Map voiced and semi-voiced Japanese kana to their unvoiced base characters."""
    if not text:
        return ""
    return text.translate(_DAKUTEN_TRANSLATION)


def normalize_singing_phonology(
    text: str,
    *,
    normalize_particles: bool = True,
    collapse_long_vowels: bool = True,
    include_ei: bool = False,
    drop_sokuon: bool = True,
    strip_dakuten_marks: bool = False,
) -> str:
    """Normalize Japanese singing text into phonologically equivalent kana."""
    if not text:
        return ""
    s = _EXTRA_PUNCT_RE.sub("", text)
    s = s.translate(_SMALL_VOWEL_MAP).translate(_HOMOPHONE_MAP)
    if normalize_particles:
        s = s.translate(_PARTICLE_MAP)
    if collapse_long_vowels:
        s = collapse_japanese_long_vowels(s, include_ei=include_ei)
    if drop_sokuon:
        s = s.replace("っ", "")
    if strip_dakuten_marks:
        s = s.translate(_DAKUTEN_TRANSLATION)
    return s


def weighted_indel_ratio(
    a: str, b: str, *, dakuten_cost: float = 0.35
) -> float:
    """Return calibrated Indel similarity [0..1] with reduced voiced/unvoiced substitution penalty."""
    la, lb = len(a), len(b)
    if la == 0 or lb == 0:
        return 0.0
    if a == b:
        return 1.0
    if dakuten_cost >= 1.0:
        return fuzz.ratio(a, b) / 100.0

    sub_dak = 2.0 * float(dakuten_cost)
    b_bases = [DAKUTEN_BASE_MAP.get(ch, ch) for ch in b]
    prev = [float(j) for j in range(lb + 1)]
    for i in range(1, la + 1):
        ca = a[i - 1]
        ba = DAKUTEN_BASE_MAP.get(ca, ca)
        curr = [float(i)] + [0.0] * lb
        for j in range(1, lb + 1):
            cb = b[j - 1]
            if ca == cb:
                cost = 0.0
            elif ba == b_bases[j - 1]:
                cost = sub_dak
            else:
                cost = 2.0
            v_sub = prev[j - 1] + cost
            v_del = prev[j] + 1.0
            v_ins = curr[j - 1] + 1.0
            curr[j] = (
                v_sub
                if v_sub <= v_del and v_sub <= v_ins
                else (v_del if v_del <= v_ins else v_ins)
            )
        prev = curr
    return max(0.0, 1.0 - prev[lb] / (la + lb))


def weighted_partial_ratio_alignment(
    src: str, dest: str, *, dakuten_cost: float = 0.35
) -> tuple[float, int, int, int, int]:
    """Calibrated weighted partial alignment matching RapidFuzz partial_ratio_alignment semantics."""
    if not src or not dest:
        return (0.0, 0, 0, 0, 0)
    if dakuten_cost >= 1.0:
        alignment = fuzz.partial_ratio_alignment(src, dest)
        if alignment is None:
            return (0.0, 0, 0, 0, 0)
        return (
            alignment.score / 100.0,
            int(alignment.src_start),
            int(alignment.src_end),
            int(alignment.dest_start),
            int(alignment.dest_end),
        )

    swapped = False
    if len(src) <= len(dest):
        short, long_s = src, dest
    else:
        short, long_s = dest, src
        swapped = True

    ls, ll = len(short), len(long_s)
    # Fast upper-bound prefilter: dakuten-stripped partial_ratio upper-bounds weighted partial score.
    ub = fuzz.partial_ratio(strip_dakuten(short), strip_dakuten(long_s)) / 100.0
    if ub < 0.55:
        alignment = fuzz.partial_ratio_alignment(src, dest)
        if alignment is None:
            return (0.0, 0, 0, 0, 0)
        return (
            alignment.score / 100.0,
            int(alignment.src_start),
            int(alignment.src_end),
            int(alignment.dest_start),
            int(alignment.dest_end),
        )

    best_score = -1.0
    best_a, best_b = 0, ls
    for j in range(0, ll - ls + 1):
        sc = weighted_indel_ratio(
            short, long_s[j : j + ls], dakuten_cost=dakuten_cost
        )
        if sc > best_score:
            best_score = sc
            best_a, best_b = j, j + ls
            if best_score >= 0.9999:
                break

    if best_score < 0.9999:
        min_k = max(1, int(ls * 0.5))
        for k in range(min_k, ls):
            sc_pre = weighted_indel_ratio(
                short, long_s[0:k], dakuten_cost=dakuten_cost
            )
            if sc_pre > best_score:
                best_score = sc_pre
                best_a, best_b = 0, k
            sc_suf = weighted_indel_ratio(
                short, long_s[ll - k : ll], dakuten_cost=dakuten_cost
            )
            if sc_suf > best_score:
                best_score = sc_suf
                best_a, best_b = ll - k, ll

    if best_score < 0.0:
        best_score = 0.0
    if not swapped:
        return (best_score, 0, ls, best_a, best_b)
    return (best_score, best_a, best_b, 0, ls)


def score_similarity_weighted_detailed(
    na: str,
    nb: str,
    *,
    min_coverage_ratio: float = 0.4,
    dakuten_cost: float = 0.35,
) -> dict[str, Any]:
    """Return detailed similarity breakdown using weighted voiced/unvoiced Indel scoring."""
    if not na or not nb:
        return {
            "coverage": 0.0,
            "raw_partial_score": 0.0,
            "partial_score": 0.0,
            "ratio_score": 0.0,
            "final_score": 0.0,
            "src_start": 0,
            "src_end": 0,
            "dest_start": 0,
            "dest_end": 0,
        }

    coverage = min(len(na) / len(nb), 1.0)
    raw_partial_score, src_start, src_end, dest_start, dest_end = (
        weighted_partial_ratio_alignment(na, nb, dakuten_cost=dakuten_cost)
    )
    partial_score = raw_partial_score
    if coverage < min_coverage_ratio:
        partial_score *= coverage / min_coverage_ratio

    ratio_score = weighted_indel_ratio(na, nb, dakuten_cost=dakuten_cost)
    final_score = max(partial_score, ratio_score)
    return {
        "coverage": coverage,
        "raw_partial_score": raw_partial_score,
        "partial_score": partial_score,
        "ratio_score": ratio_score,
        "final_score": final_score,
        "src_start": src_start,
        "src_end": src_end,
        "dest_start": dest_start,
        "dest_end": dest_end,
    }


@dataclass(frozen=True)
class LocalAlignmentPeak:
    """Single local alignment match peak within a reference character window."""

    raw_score: float
    local_sim: float
    q_start: int
    q_end: int
    ref_start: int
    ref_end: int


class ContinuousLyricsTimeline:
    """Concatenated kana timeline mapping global character offsets to lyric line indices."""

    def __init__(self, match_keys: list[str]) -> None:
        parts: list[str] = []
        line_starts: list[int] = []
        line_ends: list[int] = []
        char_to_line: list[int] = []
        cursor = 0

        for idx, key in enumerate(match_keys):
            line_starts.append(cursor)
            if key:
                parts.append(key)
                char_to_line.extend([idx] * len(key))
                cursor += len(key)
            line_ends.append(cursor)

        self.full_text: str = "".join(parts)
        self.line_starts: list[int] = line_starts
        self.line_ends: list[int] = line_ends
        self.char_to_line: list[int] = char_to_line
        self.line_count: int = len(match_keys)

    def line_length(self, line_idx: int) -> int:
        """Return character length of ``line_idx`` in the continuous timeline."""
        if 0 <= line_idx < self.line_count:
            return self.line_ends[line_idx] - self.line_starts[line_idx]
        return 0

    def line_progress(self, line_idx: int, pos: int) -> float:
        """Return normalized progress [0..1] of global character ``pos`` within ``line_idx``."""
        length = self.line_length(line_idx)
        if length <= 0:
            return 1.0
        rel = pos - self.line_starts[line_idx]
        if rel <= 0:
            return 0.0
        return min(1.0, rel / length)


def smith_waterman_local_peaks(
    query: str,
    ref: str,
    *,
    match_score: float = 2.0,
    dakuten_score: float = 1.2,
    mismatch_penalty: float = -1.6,
    gap_penalty: float = -1.5,
    min_raw_score: float = 6.0,
    top_k: int = 5,
    min_sep: int = 5,
) -> list[LocalAlignmentPeak]:
    """Find top non-overlapping local alignment peaks using weighted Smith-Waterman DP."""
    lq, lr = len(query), len(ref)
    if lq == 0 or lr == 0 or top_k <= 0:
        return []

    q_bases = [DAKUTEN_BASE_MAP.get(ch, ch) for ch in query]
    r_bases = [DAKUTEN_BASE_MAP.get(ch, ch) for ch in ref]

    prev_h = [0.0] * (lr + 1)
    prev_qs = [0] * (lr + 1)
    prev_rs = [0] * (lr + 1)
    col_best: list[tuple[float, int, int, int, int] | None] = [None] * (lr + 1)

    for i in range(1, lq + 1):
        cq = query[i - 1]
        bq = q_bases[i - 1]
        curr_h = [0.0] * (lr + 1)
        curr_qs = [i] * (lr + 1)
        curr_rs = list(range(lr + 1))

        for j in range(1, lr + 1):
            cr = ref[j - 1]
            if cq == cr:
                sub = match_score
            elif bq == r_bases[j - 1]:
                sub = dakuten_score
            else:
                sub = mismatch_penalty

            d = prev_h[j - 1] + sub
            u = prev_h[j] + gap_penalty
            l_val = curr_h[j - 1] + gap_penalty

            if d >= u and d >= l_val and d > 0.0:
                curr_h[j] = d
                if prev_h[j - 1] > 0.0:
                    curr_qs[j] = prev_qs[j - 1]
                    curr_rs[j] = prev_rs[j - 1]
                else:
                    curr_qs[j] = i - 1
                    curr_rs[j] = j - 1
            elif u >= l_val and u > 0.0:
                curr_h[j] = u
                curr_qs[j] = prev_qs[j]
                curr_rs[j] = prev_rs[j]
            elif l_val > 0.0:
                curr_h[j] = l_val
                curr_qs[j] = curr_qs[j - 1]
                curr_rs[j] = curr_rs[j - 1]

            val = curr_h[j]
            if val >= min_raw_score:
                prev_best = col_best[j]
                if prev_best is None or val > prev_best[0]:
                    col_best[j] = (val, curr_qs[j], i, curr_rs[j], j)

        prev_h = curr_h
        prev_qs = curr_qs
        prev_rs = curr_rs

    candidates: list[LocalAlignmentPeak] = []
    for item in col_best:
        if item is None:
            continue
        raw_sc, qs, qe, rs, re_ = item
        span_len = max(qe - qs, re_ - rs, 1)
        local_sim = min(1.0, raw_sc / (match_score * span_len))
        candidates.append(
            LocalAlignmentPeak(
                raw_score=raw_sc,
                local_sim=local_sim,
                q_start=qs,
                q_end=qe,
                ref_start=rs,
                ref_end=re_,
            )
        )

    candidates.sort(key=lambda peak: peak.raw_score, reverse=True)
    selected: list[LocalAlignmentPeak] = []
    for peak in candidates:
        if any(abs(peak.ref_end - kept.ref_end) < min_sep for kept in selected):
            continue
        selected.append(peak)
        if len(selected) >= top_k:
            break
    return selected
