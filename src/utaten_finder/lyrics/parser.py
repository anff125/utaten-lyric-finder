"""Browserless UtaTen lyrics HTML parser with strict <ruby> sanitization."""

import html
import re
from typing import Any

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from utaten_finder.lyrics.matching import normalize_match_text, normalize_to_hiragana
from utaten_finder.lyrics.web_scraper import extract_clean_page_title

_BLOCKED_TAGS = frozenset(
    {
        "script",
        "style",
        "noscript",
        "iframe",
        "object",
        "embed",
        "svg",
        "form",
        "input",
        "button",
        "textarea",
        "select",
        "link",
        "meta",
        "ins",
        "aside",
        "nav",
        "header",
        "footer",
        "template",
        "img",
        "video",
        "audio",
    }
)

_BLOCK_BREAK_TAGS = frozenset(
    {
        "p",
        "div",
        "section",
        "article",
        "li",
        "ul",
        "ol",
        "blockquote",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "pre",
    }
)

_PROTECTED_LYRIC_CLASSES = frozenset(
    {"hiragana", "ruby", "rb", "rt", "rp", "medium", "lyricBody", "lyric-line"}
)

_AD_ATTR_PATTERN = re.compile(
    r"(?:^|[-_])(ad|ads|adsense|adsbygoogle|advert|advertisement|banner|sponsor|promo|widget|sns|share|recommend)(?:$|[-_0-9])",
    re.IGNORECASE,
)


def _has_class(tag: Tag, class_name: str) -> bool:
    classes = tag.get("class") or []
    return class_name in classes


def _is_ad_or_widget_container(tag: Tag) -> bool:
    if getattr(tag, "decomposed", False) or tag.attrs is None:
        return False

    classes = tag.get("class") or []
    if isinstance(classes, str):
        classes = classes.split()

    if "manual-lyric-jump" in classes:
        return True

    if any(cls in _PROTECTED_LYRIC_CLASSES for cls in classes):
        return False

    for cls in classes:
        if _AD_ATTR_PATTERN.search(cls):
            return True

    tag_id = tag.get("id")
    if isinstance(tag_id, str) and tag_id and _AD_ATTR_PATTERN.search(tag_id):
        return True

    return False


def _sanitize_root(root: Tag) -> None:
    for comment in root.find_all(string=lambda text: isinstance(text, Comment)):
        comment.extract()

    for tag in list(root.find_all(_BLOCKED_TAGS)):
        if not getattr(tag, "decomposed", False):
            tag.decompose()

    for tag in list(root.find_all(True)):
        if tag is root:
            continue
        if _is_ad_or_widget_container(tag):
            tag.decompose()


def _clean_text_fragment(raw: str) -> str:
    if not raw:
        return ""
    if not raw.strip() and ("\n" in raw or "\r" in raw):
        # Ignore pretty-printed HTML indentation/newlines between adjacent tags.
        return ""
    collapsed = re.sub(r"[\r\n\t]+", " ", raw)
    return re.sub(r" {2,}", " ", collapsed)


def _extract_plain_text_without_rt(node: Tag) -> str:
    parts: list[str] = []
    for child in node.children:
        if isinstance(child, Comment):
            continue
        if isinstance(child, NavigableString):
            parts.append(str(child))
        elif isinstance(child, Tag):
            name = (child.name or "").lower()
            if name in {"rt", "rp"} or _has_class(child, "rt"):
                continue
            parts.append(_extract_plain_text_without_rt(child))
    return re.sub(r"[\r\n\t]+", "", "".join(parts)).strip()


def _serialize_utaten_ruby_span(
    span: Tag, original_parts: list[str], ruby_parts: list[str]
) -> None:
    rb_elem = span.select_one(".rb, rb")
    if rb_elem is not None:
        rb_text = re.sub(r"[\r\n\t]+", "", rb_elem.get_text()).strip()
    else:
        rb_text = _extract_plain_text_without_rt(span)

    rt_elem = span.select_one(".rt, rt")
    rt_text = (
        re.sub(r"[\r\n\t]+", "", rt_elem.get_text()).strip()
        if rt_elem is not None
        else ""
    )

    if not rb_text:
        return

    original_parts.append(rb_text)
    escaped_rb = html.escape(rb_text, quote=False)
    if rt_text:
        escaped_rt = html.escape(rt_text, quote=False)
        ruby_parts.append(f"<ruby>{escaped_rb}<rt>{escaped_rt}</rt></ruby>")
    else:
        ruby_parts.append(escaped_rb)


def _serialize_native_ruby(
    ruby_tag: Tag, original_parts: list[str], ruby_parts: list[str]
) -> None:
    pairs: list[tuple[str, str]] = []
    pending_base_parts: list[str] = []

    def _collect_base(node: Any) -> None:
        if isinstance(node, Comment):
            return
        if isinstance(node, NavigableString):
            pending_base_parts.append(str(node))
            return
        if not isinstance(node, Tag):
            return
        tag_name = (node.name or "").lower()
        if tag_name == "rp":
            return
        if tag_name == "rt" or _has_class(node, "rt"):
            base_text = re.sub(r"[\r\n\t]+", "", "".join(pending_base_parts)).strip()
            rt_text = re.sub(r"[\r\n\t]+", "", node.get_text()).strip()
            pending_base_parts.clear()
            if base_text:
                pairs.append((base_text, rt_text))
            return
        for child in node.children:
            _collect_base(child)

    _collect_base(ruby_tag)
    trailing_base = re.sub(r"[\r\n\t]+", "", "".join(pending_base_parts)).strip()
    if trailing_base:
        pairs.append((trailing_base, ""))

    if not pairs:
        return

    has_rt = any(rt for _, rt in pairs)
    ruby_inner: list[str] = []
    for base_text, rt_text in pairs:
        original_parts.append(base_text)
        escaped_base = html.escape(base_text, quote=False)
        if rt_text:
            ruby_inner.append(f"{escaped_base}<rt>{html.escape(rt_text, quote=False)}</rt>")
        else:
            ruby_inner.append(escaped_base)

    if has_rt:
        ruby_parts.append(f"<ruby>{''.join(ruby_inner)}</ruby>")
    else:
        ruby_parts.append("".join(ruby_inner))


def _serialize_inline_node(
    node: Any, original_parts: list[str], ruby_parts: list[str]
) -> None:
    if isinstance(node, Comment):
        return

    if isinstance(node, NavigableString):
        cleaned = _clean_text_fragment(str(node))
        if cleaned:
            original_parts.append(cleaned)
            ruby_parts.append(html.escape(cleaned, quote=False))
        return

    if not isinstance(node, Tag):
        return

    tag_name = (node.name or "").lower()
    if tag_name in _BLOCKED_TAGS or tag_name in {"rt", "rp"} or _has_class(node, "rt"):
        return

    if _has_class(node, "ruby"):
        _serialize_utaten_ruby_span(node, original_parts, ruby_parts)
        return

    if tag_name == "ruby":
        _serialize_native_ruby(node, original_parts, ruby_parts)
        return

    for child in node.children:
        _serialize_inline_node(child, original_parts, ruby_parts)


def _serialize_line_nodes(nodes: list[Any]) -> tuple[str, str]:
    original_parts: list[str] = []
    ruby_parts: list[str] = []
    for node in nodes:
        _serialize_inline_node(node, original_parts, ruby_parts)

    original = re.sub(r" {2,}", " ", "".join(original_parts)).strip()
    ruby_html = re.sub(r" {2,}", " ", "".join(ruby_parts)).strip()
    return original, ruby_html


def _collect_segments_from_children(
    container: Tag, *, split_blocks: bool = False
) -> list[list[Any]]:
    segments: list[list[Any]] = [[]]

    def _walk(node: Any) -> None:
        if isinstance(node, Comment):
            return
        if isinstance(node, NavigableString):
            segments[-1].append(node)
            return
        if not isinstance(node, Tag):
            return

        tag_name = (node.name or "").lower()
        if tag_name in _BLOCKED_TAGS:
            return
        if tag_name == "br":
            segments.append([])
            return
        if _has_class(node, "ruby") or tag_name == "ruby":
            segments[-1].append(node)
            return

        is_block = split_blocks and (
            tag_name in _BLOCK_BREAK_TAGS or _has_class(node, "lyric-line")
        )
        if is_block:
            if segments[-1]:
                segments.append([])
            for child in node.children:
                _walk(child)
            if segments[-1]:
                segments.append([])
            return

        if node.find("br") is not None or (
            split_blocks and node.find(_BLOCK_BREAK_TAGS) is not None
        ):
            for child in node.children:
                _walk(child)
            return

        segments[-1].append(node)

    for child in container.children:
        _walk(child)

    return segments


def _extract_raw_line_segments(root: Tag) -> list[tuple[list[Any], bool]]:
    raw_segments: list[tuple[list[Any], bool]] = []
    p_tags = root.find_all("p")

    if p_tags:
        stanza_per_p = any(p_tag.find("br") is not None for p_tag in p_tags)
        for p_idx, p_tag in enumerate(p_tags):
            sub_segments = _collect_segments_from_children(p_tag, split_blocks=False)
            for seg_idx, seg in enumerate(sub_segments):
                stanza_hint = p_idx > 0 and seg_idx == 0 and stanza_per_p
                raw_segments.append((seg, stanza_hint))
        return raw_segments

    br_segments = _collect_segments_from_children(root, split_blocks=True)
    for seg in br_segments:
        raw_segments.append((seg, False))
    return raw_segments


def parse_utaten_lyrics(html_text: str) -> dict[str, Any]:
    """
    Parse UtaTen lyric HTML into structured lyric lines and whitelisted <ruby> HTML.

    Supports both <p> paragraph layouts and <br>-delimited layouts while removing
    non-lyric elements (<script>, <style>, ads, widgets) and unsafe attributes.
    """
    soup = BeautifulSoup(html_text or "", "html.parser")

    title_elem = soup.select_one(
        "h2.newLyricTitle__main, h2.lyricTitle__main, .newLyricTitle__main"
    )
    artist_elem = soup.select_one(
        "dt.newLyricWork__name, .newLyricWork__name, .lyricArtist__name"
    )
    title = extract_clean_page_title(title_elem)
    artist = artist_elem.get_text(" ", strip=True) if artist_elem else ""

    root = soup.select_one(".lyricBody .hiragana")
    if root is None:
        root = soup.select_one(".lyricBody")
        if root is not None:
            for romaji_node in root.select(".romaji"):
                romaji_node.decompose()

    if root is None:
        return {"title": title, "artist": artist, "lines": []}

    _sanitize_root(root)
    raw_segments = _extract_raw_line_segments(root)

    lines: list[dict[str, Any]] = []
    pending_stanza_break = False

    for nodes, stanza_hint in raw_segments:
        if stanza_hint and lines:
            pending_stanza_break = True

        original, ruby_html = _serialize_line_nodes(nodes)
        normalized = (
            normalize_match_text(normalize_to_hiragana(original)) if original else ""
        )

        if not normalized:
            if lines:
                pending_stanza_break = True
            continue

        lines.append(
            {
                "index": len(lines),
                "original": original,
                "normalized": normalized,
                "ruby_html": ruby_html,
                "stanza_break": bool(pending_stanza_break and lines),
            }
        )
        pending_stanza_break = False

    return {
        "title": title,
        "artist": artist,
        "lines": lines,
    }
