"""Unit tests for UtaTen browserless fetcher and sanitized <ruby> parser."""

from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch

import requests

_SRC_DIR = str(Path(__file__).resolve().parents[1] / "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from utaten_finder.lyrics.matching import LyricSyncEngine  # noqa: E402
from utaten_finder.lyrics.parser import (  # noqa: E402
    extract_ruby_reading,
    parse_utaten_lyrics,
)
from utaten_finder.lyrics.web_scraper import fetch_utaten_lyrics_html  # noqa: E402


class TestUtaTenParser(unittest.TestCase):
    def test_parses_br_layout_and_ignores_romaji_sibling(self) -> None:
        html = """
        <html>
          <body>
            <h2 class="newLyricTitle__main">
              Sorrows <span class="newLyricTitle_afterTxt">歌詞</span>
            </h2>
            <dt class="newLyricWork__name">King Gnu</dt>
            <div class="lyricBody">
              <div class="medium">
                <div class="hiragana">
                  <span class="ruby"><span class="rb">優</span><span class="rt">やさ</span></span>しい<span class="ruby"><span class="rb">嘘</span><span class="rt">うそ</span></span>をついてくれよ<br/>
                  <span class="ruby"><span class="rb">現実</span><span class="rt">げんじつ</span></span>は<span class="ruby"><span class="rb">残酷</span><span class="rt">ざんこく</span></span>だもの<br/>
                  <br/>
                  "Why don't you come back to me?"<br/>
                </div>
                <div class="romaji">Yasashii uso wo tsuite kure yo</div>
              </div>
            </div>
          </body>
        </html>
        """
        result = parse_utaten_lyrics(html)
        self.assertEqual(result["title"], "Sorrows")
        self.assertEqual(result["artist"], "King Gnu")

        lines = result["lines"]
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0]["index"], 0)
        self.assertEqual(lines[0]["original"], "優しい嘘をついてくれよ")
        self.assertEqual(
            lines[0]["ruby_html"],
            "<ruby>優<rt>やさ</rt></ruby>しい<ruby>嘘<rt>うそ</rt></ruby>をついてくれよ",
        )
        self.assertFalse(lines[0]["stanza_break"])

        self.assertEqual(lines[1]["index"], 1)
        self.assertEqual(lines[1]["original"], "現実は残酷だもの")
        self.assertFalse(lines[1]["stanza_break"])

        self.assertEqual(lines[2]["index"], 2)
        self.assertEqual(lines[2]["original"], '"Why don\'t you come back to me?"')
        self.assertTrue(lines[2]["stanza_break"])
        self.assertNotIn("Yasashii", " ".join(line["original"] for line in lines))

    def test_parses_paragraph_layouts_both_line_and_stanza_styles(self) -> None:
        html_single_p = """
        <div class="lyricBody">
          <div class="hiragana">
            <p><span class="ruby"><span class="rb">夜</span><span class="rt">よる</span></span>が<span class="ruby"><span class="rb">明</span><span class="rt">あ</span></span>ける</p>
            <p>   </p>
            <p><span class="ruby"><span class="rb">光</span><span class="rt">ひかり</span></span>がさす</p>
          </div>
        </div>
        """
        parsed_single = parse_utaten_lyrics(html_single_p)
        self.assertEqual(len(parsed_single["lines"]), 2)
        self.assertEqual(parsed_single["lines"][0]["original"], "夜が明ける")
        self.assertEqual(
            parsed_single["lines"][0]["ruby_html"],
            "<ruby>夜<rt>よる</rt></ruby>が<ruby>明<rt>あ</rt></ruby>ける",
        )
        self.assertTrue(parsed_single["lines"][1]["stanza_break"])

        html_stanza_p = """
        <div class="lyricBody">
          <p>いちぎょうめ<br/>にぎょうめ</p>
          <p>さんぎょうめ</p>
        </div>
        """
        parsed_stanza = parse_utaten_lyrics(html_stanza_p)
        self.assertEqual(
            [line["original"] for line in parsed_stanza["lines"]],
            ["いちぎょうめ", "にぎょうめ", "さんぎょうめ"],
        )
        self.assertFalse(parsed_stanza["lines"][0]["stanza_break"])
        self.assertFalse(parsed_stanza["lines"][1]["stanza_break"])
        self.assertTrue(parsed_stanza["lines"][2]["stanza_break"])

    def test_parses_native_ruby_and_plain_english_with_html_escaping(self) -> None:
        html = """
        <div class="lyricBody">
          <div class="hiragana">
            <ruby><rb>東京</rb><rp>(</rp><rt>とうきょう</rt><rp>)</rp></ruby>へ<ruby>行<rt>い</rt>く</ruby><br/>
            <span>Love &amp; Peace &lt;3</span><br/>
            ひらがなだけ<br/>
          </div>
        </div>
        """
        parsed = parse_utaten_lyrics(html)
        lines = parsed["lines"]
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0]["original"], "東京へ行く")
        self.assertEqual(
            lines[0]["ruby_html"],
            "<ruby>東京<rt>とうきょう</rt></ruby>へ<ruby>行<rt>い</rt>く</ruby>",
        )
        self.assertEqual(lines[1]["original"], "Love & Peace <3")
        self.assertEqual(lines[1]["ruby_html"], "Love &amp; Peace &lt;3")
        self.assertEqual(lines[2]["original"], "ひらがなだけ")
        self.assertEqual(lines[2]["ruby_html"], "ひらがなだけ")

    def test_sanitizes_scripts_ads_and_unsafe_attributes(self) -> None:
        html = """
        <div class="lyricBody">
          <div class="hiragana">
            <!-- hidden comment -->
            <script>alert('xss')</script>
            <style>.x { color: red; }</style>
            <iframe src="https://evil.example"></iframe>
            <div class="adsbygoogle">BUY NOW</div>
            <div id="ad-slot-1">SPONSORED</div>
            <button class="manual-lyric-jump">●</button>
            <a href="javascript:alert(1)" onclick="steal()">
              <span class="ruby" onclick="x()"><span class="rb">夢</span><span class="rt">ゆめ</span></span>の<b onmouseover="y()">中</b>
            </a><br/>
            つぎの行<br/>
          </div>
        </div>
        """
        parsed = parse_utaten_lyrics(html)
        lines = parsed["lines"]
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["original"], "夢の中")
        self.assertEqual(lines[0]["ruby_html"], "<ruby>夢<rt>ゆめ</rt></ruby>の中")
        joined_html = "\n".join(line["ruby_html"] for line in lines)
        for forbidden in ("<script", "<style", "<iframe", "onclick", "javascript:", "BUY NOW", "SPONSORED"):
            self.assertNotIn(forbidden, joined_html)

    def test_missing_lyric_body_returns_empty_lines(self) -> None:
        parsed = parse_utaten_lyrics("<html><body><p>no lyrics here</p></body></html>")
        self.assertEqual(parsed["lines"], [])

    def test_sync_engine_integration_matches_parsed_indices(self) -> None:
        html = """
        <div class="lyricBody">
          <div class="hiragana">
            <span class="ruby"><span class="rb">夢</span><span class="rt">ゆめ</span></span>の<span class="ruby"><span class="rb">中</span><span class="rt">なか</span></span><br/>
            <span class="ruby"><span class="rb">君</span><span class="rt">きみ</span></span>を<span class="ruby"><span class="rb">探</span><span class="rt">さが</span></span>してる<br/>
            <span class="ruby"><span class="rb">夜</span><span class="rt">よる</span></span>が<span class="ruby"><span class="rb">明</span><span class="rt">あ</span></span>けるまで<br/>
          </div>
        </div>
        """
        parsed = parse_utaten_lyrics(html)
        engine = LyricSyncEngine()
        engine.reset_with_lyrics(parsed["lines"])

        jump = engine.apply_manual_jump(0)
        self.assertIsNotNone(jump)
        assert jump is not None
        self.assertEqual(jump["current_line_text"], "夢の中")

        match = engine.process_recognized_text("君を探してる")
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match["matched_index"], 1)
        self.assertEqual(match["display_index"], 2)

    def test_ateji_and_gikun_rt_readings_prioritized_over_pykakasi(self) -> None:
        html = """
        <div class="lyricBody">
          <div class="hiragana">
            <span class="ruby"><span class="rb">明日</span><span class="rt">あした</span></span>は<span class="ruby"><span class="rb">本気</span><span class="rt">マジ</span></span>だ<br/>
            <span class="ruby"><span class="rb">宇宙</span><span class="rt">そら</span></span>の<span class="ruby"><span class="rb">運命</span><span class="rt">さだめ</span></span>と<span class="ruby"><span class="rb">私</span><span class="rt">あたし</span></span><br/>
            <span class="ruby"><span class="rb">氷菓</span><span class="rt">アイス</span></span>の<span class="ruby"><span class="rb">行方</span><span class="rt">ゆくえ</span></span>は<span class="ruby"><span class="rb">如何</span><span class="rt">どう</span></span>か<br/>
            <span class="ruby"><span class="rb">明後日</span><span class="rt">あさって</span></span>にしよう<br/>
          </div>
        </div>
        """
        lines = parse_utaten_lyrics(html)["lines"]
        self.assertEqual(len(lines), 4)

        self.assertEqual(lines[0]["original"], "明日は本気だ")
        self.assertEqual(lines[0]["reading"], "あしたはまじだ")
        self.assertEqual(lines[0]["normalized"], "あしたはまじだ")
        self.assertEqual(
            lines[0]["ruby_html"],
            "<ruby>明日<rt>あした</rt></ruby>は<ruby>本気<rt>マジ</rt></ruby>だ",
        )

        self.assertEqual(lines[1]["original"], "宇宙の運命と私")
        self.assertEqual(lines[1]["reading"], "そらのさだめとあたし")
        self.assertEqual(lines[1]["normalized"], "そらのさだめとあたし")

        self.assertEqual(lines[2]["original"], "氷菓の行方は如何か")
        self.assertEqual(lines[2]["reading"], "あいすのゆくえはどうか")
        self.assertEqual(lines[2]["normalized"], "あいすのゆくえはどうか")
        self.assertIn("<rt>アイス</rt>", lines[2]["ruby_html"])

        self.assertEqual(lines[3]["original"], "明後日にしよう")
        self.assertEqual(lines[3]["reading"], "あさってにしよう")
        self.assertEqual(lines[3]["normalized"], "あさってにしよう")

    def test_rt_reading_fallbacks_and_edge_cases(self) -> None:
        html = """
        <div class="lyricBody">
          <div class="hiragana">
            <span class="ruby"><span class="rb">今日</span><span class="rt">きょう</span></span>の<span>手</span><span>紙</span><br/>
            <ruby>本気<rp>(</rp><rt>マジ</rt><rp>)</rp>で</ruby><ruby>宇宙<rt>そら</rt></ruby>へ<br/>
            <span class="ruby"><span class="rb">手</span><span class="rt">   </span></span>紙と<span class="ruby"><span class="rb">運命</span><span class="rt">・・・</span></span><br/>
            <span class="ruby"><span class="rb">氷菓</span><span class="rt">ｱｲｽ</span></span>とﾊﾟﾋﾟﾌﾟﾍﾟﾎﾟ<br/>
            <span class="ruby"><span class="rb">愛</span><span class="rt">LOVE</span></span>を歌う<br/>
          </div>
        </div>
        """
        lines = parse_utaten_lyrics(html)["lines"]
        self.assertEqual(len(lines), 5)

        # Consecutive non-<rt> tokens are coalesced before pykakasi conversion (手 + 紙 -> てがみ).
        self.assertEqual(lines[0]["original"], "今日の手紙")
        self.assertEqual(lines[0]["reading"], "きょうのてがみ")
        self.assertEqual(lines[0]["normalized"], "きょうのてがみ")

        # Native <ruby> with <rp> and trailing okurigana.
        self.assertEqual(lines[1]["original"], "本気で宇宙へ")
        self.assertEqual(lines[1]["reading"], "まじでそらへ")
        self.assertEqual(lines[1]["normalized"], "まじでそらへ")

        # Empty/whitespace <rt> and emphasis-dot-only <rt> fall back to base text.
        self.assertEqual(lines[2]["original"], "手紙と運命")
        self.assertEqual(lines[2]["reading"], "てがみとうんめい")
        self.assertEqual(lines[2]["normalized"], "てがみとうんめい")
        self.assertIn("<rt>・・・</rt>", lines[2]["ruby_html"])

        # Half-width katakana in both <rt> and plain text converts cleanly to hiragana.
        self.assertEqual(lines[3]["reading"], "あいすとぱぴぷぺぽ")
        self.assertEqual(lines[3]["normalized"], "あいすとぱぴぷぺぽ")

        # English <rt> preserves case in reading and lowercases in normalized.
        self.assertEqual(lines[4]["original"], "愛を歌う")
        self.assertEqual(lines[4]["reading"], "LOVEをうたう")
        self.assertEqual(lines[4]["normalized"], "loveをうたう")

    def test_extract_ruby_reading_matches_parse_utaten_lyrics(self) -> None:
        html = """
        <div class="lyricBody">
          <div class="hiragana">
            <span class="ruby"><span class="rb">本気</span><span class="rt">マジ</span></span>で<span class="ruby"><span class="rb">氷菓</span><span class="rt">アイス</span></span><br/>
            <ruby><rb>東京</rb><rp>(</rp><rt>とうきょう</rt><rp>)</rp></ruby>へ<ruby>行<rt>い</rt>く</ruby><br/>
            <span>Love &amp; Peace &lt;3</span><br/>
            ひらがなだけ<br/>
          </div>
        </div>
        """
        lines = parse_utaten_lyrics(html)["lines"]
        self.assertEqual(len(lines), 4)
        for line in lines:
            reading, normalized = extract_ruby_reading(line["ruby_html"])
            self.assertEqual((reading, normalized), (line["reading"], line["normalized"]))

        self.assertEqual(extract_ruby_reading(""), ("", ""))
        self.assertEqual(extract_ruby_reading("   "), ("", ""))

    def test_sync_engine_matches_ateji_using_rt_reading(self) -> None:
        html = """
        <div class="lyricBody">
          <div class="hiragana">
            <span class="ruby"><span class="rb">本気</span><span class="rt">マジ</span></span>で<span class="ruby"><span class="rb">宇宙</span><span class="rt">そら</span></span>を<span class="ruby"><span class="rb">飛</span><span class="rt">と</span></span>ぶ<br/>
          </div>
        </div>
        """
        parsed = parse_utaten_lyrics(html)
        engine = LyricSyncEngine(emit_events=False)
        engine.reset_with_lyrics(parsed["lines"])

        match = engine.process_recognized_text("マジでそらをとぶ")
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match["matched_index"], 0)
        self.assertAlmostEqual(match["score"], 1.0, places=4)
        self.assertEqual(match["target_normalized"], "まじでそらをとぶ")


class TestFetchUtaTenHtml(unittest.TestCase):
    def test_rejects_non_lyric_detail_url(self) -> None:
        with self.assertRaises(ValueError):
            fetch_utaten_lyrics_html("https://utaten.com/lyric/ranking/")
        with self.assertRaises(ValueError):
            fetch_utaten_lyrics_html("https://example.com/lyric/123/")

    @patch("utaten_finder.lyrics.web_scraper.time.sleep", return_value=None)
    @patch("utaten_finder.lyrics.web_scraper.requests.get")
    def test_retries_and_decodes_utf8_despite_iso8859_header(
        self, mock_get: MagicMock, _mock_sleep: MagicMock
    ) -> None:
        html_bytes = "<html><body><div class='hiragana'>夜明け</div></body></html>".encode(
            "utf-8"
        )
        ok_response = MagicMock()
        ok_response.content = html_bytes
        ok_response.encoding = "ISO-8859-1"
        ok_response.apparent_encoding = "utf-8"
        ok_response.raise_for_status.return_value = None

        mock_get.side_effect = [
            requests.Timeout("timed out"),
            ok_response,
        ]

        decoded = fetch_utaten_lyrics_html(
            "https://www.utaten.com/lyric/ac19030854/",
            retries=1,
        )
        self.assertIn("夜明け", decoded)
        self.assertEqual(mock_get.call_count, 2)


if __name__ == "__main__":
    unittest.main()
