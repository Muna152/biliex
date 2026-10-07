"""离线单元测试（只用标准库 unittest，不需要任何第三方依赖）。

覆盖三类：
1. 纯函数正确性（时间格式、分块、输入解析、字幕轨选择、官方载荷解析）；
2. **安全不变量** —— 凭据绝不能出现在 repr / 描述 / 落盘内容里；
3. 错误码映射。
"""

from __future__ import annotations

import unittest

from biliex import auth, config, target
from biliex.chunk import chunk_cues, fmt_ts, format_transcript
from biliex.content import (
    Cue,
    SubtitleOutcome,
    _coverage,
    _parse_official,
    _parse_subtitle_body,
    _pick_track,
    _relevance,
    _same_content,
    _title_keywords,
    _track_signature,
    fetch_subtitle,
)
from biliex.errors import NotAuthenticated, RateLimited, RiskControl
from biliex.http import Cookie, _classify_business_code, _classify_http_error
from biliex.render import _yaml_scalar
from biliex.wbi import get_mixin_key, sign_params, WbiKeys


class TestTimeFormat(unittest.TestCase):
    def test_under_one_hour(self):
        self.assertEqual(fmt_ts(0), "00:00")
        self.assertEqual(fmt_ts(75), "01:15")
        self.assertEqual(fmt_ts(3599), "59:59")

    def test_over_one_hour(self):
        self.assertEqual(fmt_ts(3600), "1:00:00")
        self.assertEqual(fmt_ts(3725), "1:02:05")

    def test_negative_clamped(self):
        self.assertEqual(fmt_ts(-10), "00:00")


class TestChunking(unittest.TestCase):
    def _cues(self, count: int, text: str = "这是一句测试字幕内容") -> list[Cue]:
        return [Cue(start=i * 3.0, end=i * 3.0 + 2.5, text=text) for i in range(count)]

    def test_empty(self):
        self.assertEqual(chunk_cues([]), [])

    def test_covers_all_cues(self):
        cues = self._cues(50)
        chunks = chunk_cues(cues, max_chars=120, overlap_chars=20)
        self.assertGreater(len(chunks), 1)
        # 第一块从 0 开始，最后一块覆盖到最后一条
        self.assertEqual(chunks[0].start, cues[0].start)
        self.assertEqual(chunks[-1].end, cues[-1].end)
        # 相邻块必须重叠（否则边界内容会丢）
        for previous, current in zip(chunks, chunks[1:]):
            self.assertLess(current.start, previous.end)

    def test_single_oversized_cue_does_not_loop(self):
        """单条超长字幕必须仍能切出来，不能死循环。"""
        cues = [Cue(start=0, end=1, text="x" * 5000)]
        chunks = chunk_cues(cues, max_chars=100, overlap_chars=10)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].cue_count, 1)

    def test_transcript_has_timestamps(self):
        text = format_transcript(self._cues(3))
        self.assertTrue(text.startswith("[00:00] "))
        self.assertIn("[00:06]", text)


class TestTargetParse(unittest.TestCase):
    def test_bare_bvid(self):
        self.assertEqual(target.parse("BV17x411w7KC").bvid, "BV17x411w7KC")

    def test_full_url_with_page(self):
        t = target.parse("https://www.bilibili.com/video/BV17x411w7KC?p=3")
        self.assertEqual(t.bvid, "BV17x411w7KC")
        self.assertEqual(t.page, 3)

    def test_av_number(self):
        t = target.parse("https://www.bilibili.com/video/av170001")
        self.assertEqual(t.aid, 170001)

    def test_short_link(self):
        t = target.parse("https://b23.tv/abc123")
        self.assertTrue(t.is_short_link)
        self.assertEqual(t.short_code, "abc123")

    def test_garbage_raises(self):
        with self.assertRaises(ValueError):
            target.parse("这不是一个视频")
        with self.assertRaises(ValueError):
            target.parse("   ")


class TestSubtitleLogic(unittest.TestCase):
    def test_prefers_manual_chinese_over_ai(self):
        tracks = [
            {"lan": "ai-zh", "subtitle_url": "//aisubtitle/x.json"},
            {"lan": "zh-Hans", "subtitle_url": "//i0/x.json"},
            {"lan": "en-US", "subtitle_url": "//i0/y.json"},
        ]
        track, is_ai = _pick_track(tracks)
        self.assertEqual(track["lan"], "zh-Hans")
        self.assertFalse(is_ai)

    def test_falls_back_to_ai_when_only_option(self):
        track, is_ai = _pick_track([{"lan": "ai-zh", "subtitle_url": "//a/x.json"}])
        self.assertEqual(track["lan"], "ai-zh")
        self.assertTrue(is_ai)

    def test_track_without_url_ignored(self):
        track, _ = _pick_track([{"lan": "zh-Hans", "subtitle_url": ""}])
        self.assertIsNone(track)

    def test_parse_subtitle_body_sorted(self):
        cues = _parse_subtitle_body(
            {"body": [
                {"from": 5.0, "to": 7.0, "content": "后"},
                {"from": 1.0, "to": 3.0, "content": "前"},
                {"from": 3.0, "to": 4.0, "content": "   "},
            ]}
        )
        self.assertEqual([c.text for c in cues], ["前", "后"])

    def test_coverage_detects_truncation(self):
        cues = [Cue(start=0, end=60, text="a")]
        self.assertAlmostEqual(_coverage(cues, 600), 0.1)
        self.assertAlmostEqual(_coverage(cues, 60), 1.0)
        self.assertEqual(_coverage([], 600), 0.0)
        self.assertEqual(_coverage(cues, 0), 0.0)


class TestOfficialParsing(unittest.TestCase):
    def test_parses_summary_outline_and_subtitles(self):
        payload = {
            "code": 0,
            "model_result": {
                "summary": "整段摘要",
                "outline": [
                    {
                        "title": "第一节",
                        "timestamp": 1,
                        "part_outline": [
                            {"timestamp": 1, "content": "要点一"},
                            {"timestamp": 39, "content": "要点二"},
                        ],
                    }
                ],
                "subtitle": [
                    {
                        "part_subtitle": [
                            {"content": "第一句", "start_timestamp": 0, "end_timestamp": 1},
                            {"content": "第二句", "start_timestamp": 1, "end_timestamp": 3},
                        ]
                    }
                ],
            },
        }
        summary, outline, cues = _parse_official(payload)
        self.assertEqual(summary, "整段摘要")
        self.assertEqual(outline[0].title, "第一节")
        self.assertEqual(len(outline[0].points), 2)
        self.assertEqual([c.text for c in cues], ["第一句", "第二句"])

    def test_tolerates_missing_fields(self):
        summary, outline, cues = _parse_official({"code": 0})
        self.assertEqual(summary, "")
        self.assertEqual(outline, [])
        self.assertEqual(cues, [])


class TestWbi(unittest.TestCase):
    def setUp(self):
        # 用固定密钥，保证可重复
        self.keys = WbiKeys(img_key="a" * 32, sub_key="b" * 32, fetched_at=0.0)

    def test_mixin_key_length_and_determinism(self):
        key = get_mixin_key("a" * 32, "b" * 32)
        self.assertEqual(len(key), 32)
        self.assertEqual(key, get_mixin_key("a" * 32, "b" * 32))

    def test_sign_is_deterministic_for_fixed_wts(self):
        first = sign_params({"bvid": "BV1", "cid": 1}, self.keys, wts=1700000000)
        second = sign_params({"bvid": "BV1", "cid": 1}, self.keys, wts=1700000000)
        self.assertEqual(first["w_rid"], second["w_rid"])
        self.assertEqual(len(first["w_rid"]), 32)

    def test_sign_changes_with_params(self):
        a = sign_params({"cid": 1}, self.keys, wts=1700000000)
        b = sign_params({"cid": 2}, self.keys, wts=1700000000)
        self.assertNotEqual(a["w_rid"], b["w_rid"])

    def test_special_chars_stripped_from_values(self):
        """`!'()*` 必须从参数值中剔除后再签名。"""
        with_special = sign_params({"kw": "a!b'c(d)e*f"}, self.keys, wts=1700000000)
        without = sign_params({"kw": "abcdef"}, self.keys, wts=1700000000)
        self.assertEqual(with_special["w_rid"], without["w_rid"])

    def test_none_values_dropped(self):
        signed = sign_params({"bvid": "BV1", "up_mid": None}, self.keys, wts=1700000000)
        self.assertNotIn("up_mid", signed)

    def test_freshness(self):
        self.assertFalse(WbiKeys("a", "b", fetched_at=0.0).is_fresh())


class TestErrorMapping(unittest.TestCase):
    def test_business_codes(self):
        self.assertIsInstance(_classify_business_code(-101, ""), NotAuthenticated)
        self.assertIsInstance(_classify_business_code(-352, ""), RiskControl)

    def test_http_codes(self):
        self.assertIsInstance(_classify_http_error(412), RateLimited)


class TestSecurityInvariants(unittest.TestCase):
    """凭据绝不能出现在任何会被打印或落盘的地方。"""

    SECRET = "SECRET_SESSDATA_VALUE_1234567890"

    def test_cookie_repr_is_redacted(self):
        cookie = Cookie(sessdata=self.SECRET, bili_jct="jct_value")
        self.assertNotIn(self.SECRET, repr(cookie))
        self.assertNotIn(self.SECRET, str(cookie))
        self.assertNotIn("jct_value", repr(cookie))

    def test_cookie_header_does_contain_secret(self):
        """header() 是唯一允许携带明文的地方（要发给 B 站）。"""
        cookie = Cookie(sessdata=self.SECRET)
        self.assertIn(self.SECRET, cookie.header())

    def test_auth_describe_is_redacted(self):
        cred = auth.Credential(sessdata=self.SECRET, bili_jct="x", source="manual")
        described = auth.describe(cred)
        self.assertNotIn(self.SECRET, str(described))

    def test_redact_short_value(self):
        self.assertNotIn("abcd", config.redact("abcd"))

    def test_env_credential_not_leaked_in_status_text(self):
        cred = auth.Credential(sessdata=self.SECRET, source="env")
        info = auth.describe(cred)
        self.assertEqual(info["path"], "")  # env 来源不暴露文件路径


class TestRenderHelpers(unittest.TestCase):
    def test_yaml_scalar_escapes_quotes_and_newlines(self):
        self.assertEqual(_yaml_scalar('a"b\nc'), '"a\'b c"')

    def test_yaml_scalar_handles_empty(self):
        self.assertEqual(_yaml_scalar(""), '""')


class _FakeClient:
    """按脚本依次返回字幕内容的假 client，用来模拟上游行为。"""

    def __init__(self, reads: list[list[tuple[float, float, str]]], *, logged_in: bool = True):
        self._reads = list(reads)
        self._logged_in = logged_in
        self.downloads = 0

    def player_v2(self, bvid: str, cid: int) -> dict:
        if not self._logged_in:
            return {"login_mid": 0, "subtitle": {"subtitles": []}}
        return {
            "login_mid": 14933703,
            "subtitle": {"subtitles": [{"lan": "ai-zh", "subtitle_url": "//a/x.json"}]},
        }

    def subtitle_json(self, url: str) -> dict:
        self.downloads += 1
        cues = self._reads.pop(0) if self._reads else []
        return {
            "body": [
                {"from": start, "to": end, "content": text} for start, end, text in cues
            ]
        }


def _cues(*items: tuple[float, float, str]) -> list[tuple[float, float, str]]:
    return list(items)


class TestSubtitleDualRead(unittest.TestCase):
    """双读一致性校验 —— 依据实测：上游对同一 cid 会返回互不相同的内容。"""

    def test_signature_and_same_content(self):
        a = [Cue(0, 1, "第一句"), Cue(1, 3, "第二句")]
        b = [Cue(0, 1, "第一句"), Cue(1, 3, "第二句")]
        c = [Cue(0, 1, "第一句")]
        self.assertEqual(_track_signature(a), _track_signature(b))
        self.assertTrue(_same_content(a, b))
        self.assertFalse(_same_content(a, c))

    def test_identical_reads_with_full_coverage_is_stable(self):
        body = _cues((0, 30, "开头"), (30, 60, "结尾"))
        client = _FakeClient([body, body])
        outcome = fetch_subtitle(client, "BV1", 1, 60, attempts=1)
        self.assertIsInstance(outcome, SubtitleOutcome)
        self.assertTrue(outcome.stable)
        self.assertTrue(outcome.usable)
        self.assertAlmostEqual(outcome.coverage, 1.0)
        self.assertEqual([c.text for c in outcome.cues], ["开头", "结尾"])

    def test_inconsistent_reads_are_flagged_unstable(self):
        first = _cues((0, 30, "甲甲甲甲"), (30, 60, "甲尾"))
        second = _cues((0, 15, "乙乙乙乙"))  # 完全不同的一份
        client = _FakeClient([first, second])
        outcome = fetch_subtitle(client, "BV1", 1, 60, attempts=1)
        self.assertFalse(outcome.stable)
        self.assertFalse(outcome.usable)
        self.assertTrue(any("不一致" in w for w in outcome.warnings))
        self.assertTrue(any("可能不完整" in w for w in outcome.warnings))
        # 应保留覆盖率更高的那一份
        self.assertEqual(outcome.coverage, 1.0)

    def test_low_coverage_but_consistent_is_still_unstable(self):
        body = _cues((0, 6, "只有开头"))  # 覆盖率 10%
        client = _FakeClient([body, body])
        outcome = fetch_subtitle(client, "BV1", 1, 60, attempts=1)
        self.assertFalse(outcome.stable)
        self.assertLess(outcome.coverage, 0.5)
        self.assertTrue(any("截断" in w for w in outcome.warnings))

    def test_not_logged_in_fails_fast_with_accurate_reason(self):
        client = _FakeClient([], logged_in=False)
        outcome = fetch_subtitle(client, "BV1", 1, 60, attempts=4)
        self.assertEqual(outcome.cues, [])
        self.assertTrue(any("需要登录" in w for w in outcome.warnings))
        # 未登录不应触发任何字幕下载，也不应重试多轮
        self.assertEqual(client.downloads, 0)
        self.assertEqual(len(outcome.warnings), 1)

    def test_logged_in_without_tracks_reports_missing(self):
        client = _FakeClient([], logged_in=True)
        # player_v2 有登录态但仍返回空轨时，走另一条分支
        client.player_v2 = lambda bvid, cid: {"login_mid": 1, "subtitle": {"subtitles": []}}
        outcome = fetch_subtitle(client, "BV1", 1, 60, attempts=1)
        self.assertEqual(outcome.cues, [])
        self.assertTrue(any("确实没有字幕" in w for w in outcome.warnings))


class TestRelevanceAgainstRealData(unittest.TestCase):
    """回归测试：用实测抓到的**真实文本**验证相关性判据能区分对错内容。

    背景：对 BV1VCHY6BEcn，`conclusion/get` 返回的是正确内容（讲新疆农业），
    而 `player/v2` 返回的却是**另一个视频**的字幕（讲"顶层设计"）。
    下面两段都是当时真实抓到的开头文本。
    """

    TITLE = "我记得课本里的新疆，不是这样的啊？？"
    CORRECT = (
        "新疆竟然有海传出去有红海海南海怎么他也在新疆啊羊也不太正常"
        "在沙漠里吃沙漠中国这么大物产丰富无奇不有"
    )
    WRONG = (
        "大老师前面视频看多了你就发现大家各有千秋指点江山真变时弊"
        "但是今儿巫师这带你们进入真上帝视角"
    )

    def test_content_words_exclude_function_ngrams(self):
        keywords = _title_keywords(self.TITLE)
        self.assertIn("新疆", keywords)
        self.assertIn("课本", keywords)
        # 这些是虚词组合，必须被滤掉，否则"零命中"判据会被它们污染
        for junk in ("是这样", "这样的", "是这", "不是这", "样的啊"):
            self.assertNotIn(junk, keywords)

    def test_correct_content_is_not_flagged(self):
        matched, total, _ = _relevance(self.TITLE, [Cue(0, 9, self.CORRECT)])
        self.assertGreaterEqual(total, 2)
        self.assertGreater(matched, 0)

    def test_wrong_content_is_flagged(self):
        matched, total, missing = _relevance(self.TITLE, [Cue(0, 9, self.WRONG)])
        self.assertGreaterEqual(total, 2)
        self.assertEqual(matched, 0)
        self.assertTrue(missing)

    def test_function_words_alone_never_count_as_match(self):
        """错误内容里堆满虚词组合，也不能算命中。"""
        text = "是这样的，是这样，是这，不是这，样的啊" * 50
        matched, total, _ = _relevance(self.TITLE, [Cue(0, 9, text)])
        self.assertGreaterEqual(total, 2)
        self.assertEqual(matched, 0)

    def test_too_few_keywords_is_unjudgeable(self):
        self.assertEqual(_relevance("??？", [Cue(0, 1, "x")]), (0, 0, []))


if __name__ == "__main__":
    unittest.main()
