"""经典原文导入器测试

核心验收判据（对应 AGENT.md 红线 R1「原文原样」）：
**已知条文的正文必须与通行本逐字一致。**

测试必须能独立运行——不依赖 .raw-sources（原始下载数据可能被清理），
因此对**已落盘的产物**做校验，而不是重复跑导入流程。

运行：python3 src/nhx_corpus/test_import.py
"""

import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from import_classics import (  # noqa: E402
    clean_text, classify_verification, extract_markers, normalize_for_compare,
    strip_markers,
)

REPO = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
SH_PATH = os.path.join(REPO, "knowledge", "01-人纪-中医经典", "04-伤寒论", "原文",
                       "伤寒论-条文.json")
JG_PATH = os.path.join(REPO, "knowledge", "01-人纪-中医经典", "05-金匮要略", "原文",
                       "金匮要略-条文.json")


# ── 标记解析 ────────────────────────────────────────────────────────────────

class TestMarkers(unittest.TestCase):
    def test_strip_markers(self):
        raw = "12、太阳中风。$f{桂枝汤}主之。$a{（一作纵）}"
        self.assertEqual(strip_markers(raw), "12、太阳中风。桂枝汤主之。（一作纵）")

    def test_extract_by_type(self):
        raw = "$f{桂枝汤}主之。$u{厚朴}$u{杏子}$a{注文}"
        m = extract_markers(raw)
        self.assertEqual(m["formulas"], ["桂枝汤"])
        self.assertEqual(m["herbs"], ["厚朴", "杏子"])
        self.assertEqual(m["annotations"], ["注文"])

    def test_extract_dedupes_preserving_order(self):
        m = extract_markers("$f{桂枝汤}$f{桂枝汤}$f{麻黄汤}")
        self.assertEqual(m["formulas"], ["桂枝汤", "麻黄汤"])

    def test_no_markers(self):
        m = extract_markers("太阳之为病。")
        self.assertEqual(m["formulas"], [])
        self.assertEqual(m["annotations"], [])

    def test_clean_text_removes_number_prefix(self):
        self.assertEqual(clean_text("12、太阳中风。"), "太阳中风。")
        self.assertEqual(clean_text("1. 太阳之为病。"), "太阳之为病。")

    def test_clean_text_normalizes_whitespace(self):
        self.assertEqual(clean_text("太阳  之为病。\r\n\r\n脉浮。"), "太阳之为病。\n脉浮。")

    def test_nested_marker_inner_extracted(self):
        """回归：嵌套标记 $h{$f{X}} 的内层方名必须能取到。

        正则版实现会把内层 `$f{...` 吞进外层 body，导致方名丢失。
        """
        m = extract_markers("$h{$f{桂枝加厚朴杏子汤}}")
        self.assertEqual(m["formulas"], ["桂枝加厚朴杏子汤"],
                         "嵌套标记的内层内容必须被解析出来")

    def test_nested_marker_stripped_cleanly(self):
        self.assertEqual(strip_markers("$h{$f{桂枝加厚朴杏子汤}}"), "桂枝加厚朴杏子汤")

    def test_unclosed_marker_does_not_swallow_text(self):
        """未闭合标记不得吞掉后面的正文。"""
        self.assertEqual(strip_markers("未闭合$h{桂枝汤"), "未闭合桂枝汤")
        self.assertEqual(extract_markers("未闭合$h{桂枝汤")["formulas"], [])

    def test_mixed_nested_and_flat(self):
        raw = "18、喘家作$f{桂枝汤}。加$u{厚朴}$u{杏子}佳。$h{$f{桂枝加厚朴杏子汤}}"
        m = extract_markers(raw)
        self.assertEqual(m["formulas"], ["桂枝汤", "桂枝加厚朴杏子汤"])
        self.assertEqual(m["herbs"], ["厚朴", "杏子"])
        self.assertNotIn("$", strip_markers(raw))

    def test_normalize_for_compare_ignores_punctuation(self):
        self.assertEqual(normalize_for_compare("必恶寒体痛呕逆。"),
                         normalize_for_compare("必恶寒。体痛。呕逆。"))

    def test_clean_text_removes_zero_width(self):
        self.assertNotIn("\ufeff", clean_text("\ufeff太阳"))
        self.assertNotIn("\u200b", clean_text("太\u200b阳"))


class TestVerificationClass(unittest.TestCase):
    def test_far_takes_precedence(self):
        cmp_data = {"exact": 0, "near": [[5, 0.95]], "far": [[5, 0.5]]}
        self.assertEqual(classify_verification(5, cmp_data)[0], "divergent")

    def test_near(self):
        cmp_data = {"exact": 0, "near": [[6, 0.989]], "far": []}
        self.assertEqual(classify_verification(6, cmp_data), ("minor_variant", 0.989))

    def test_verified(self):
        self.assertEqual(classify_verification(999, {"near": [], "far": []}),
                         ("verified", None))


# ── 产物校验（R1 验收）──────────────────────────────────────────────────────

@unittest.skipUnless(os.path.exists(SH_PATH), "伤寒论产物不存在，跳过")
class TestShanghanArtifact(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(SH_PATH, "r", encoding="utf-8") as fh:
            cls.doc = json.load(fh)
        cls.by_no = {a["no"]: a for a in cls.doc["articles"]}

    # ★ R1 核心验收：已知条文必须与通行本逐字一致
    KNOWN = {
        1: "太阳之为病。脉浮。头项强痛而恶寒。",
        2: "太阳病。发热。汗出。恶风。脉缓者。名为中风。",
        3: "太阳病。或已发热。或未发热。必恶寒。体痛。呕逆。脉阴阳俱紧者。名为伤寒。",
        12: "太阳中风。阳浮而阴弱。阳浮者。热自发。阴弱者。汗自出。啬啬恶寒。"
            "淅淅恶风。翕翕发热。鼻鸣干呕者。桂枝汤主之。",
        13: "太阳病。头痛。发热。汗出。恶风。桂枝汤主之。",
        263: "少阳之为病。口苦。咽干。目眩也。",
    }

    def test_known_articles_verbatim(self):
        """红线 R1：原文必须逐**字**一致，不得由模型或人工改写。

        比对前去掉标点——不同版本的**句读**不同（第 3 条来源作
        「必恶寒体痛呕逆」，通行标点本作「必恶寒。体痛。呕逆。」），
        字完全一样。把标点算进去会把排版差异误判成文本差异。
        """
        for no, expected in self.KNOWN.items():
            with self.subTest(no=no):
                self.assertIn(no, self.by_no, "缺少第 %d 条" % no)
                self.assertEqual(
                    normalize_for_compare(self.by_no[no]["text"]),
                    normalize_for_compare(expected),
                    "第 %d 条与通行本不一致" % no)

    def test_known_articles_startswith_96(self):
        """第 96 条较长，校验开头与关键句。"""
        t = normalize_for_compare(self.by_no[96]["text"])
        self.assertTrue(t.startswith("伤寒五六日中风往来寒热胸胁苦满"))
        self.assertIn("小柴胡汤主之", self.by_no[96]["text"])

    def test_punctuation_variance_documented(self):
        """句读差异必须记录在案，不能静默吞掉。"""
        notes = self.doc.get("textual_notes") or []
        self.assertTrue(any("句读" in n.get("issue", "") for n in notes),
                        "必须记录句读随版本而异这一事实")
        self.assertTrue(any("简繁" in n.get("issue", "") for n in notes),
                        "必须记录简繁处理方式")

    def test_completeness_1_to_398(self):
        nos = sorted(self.by_no)
        self.assertEqual(nos, list(range(1, 399)), "条号必须 1–398 连续无缺")

    def test_no_duplicates(self):
        nos = [a["no"] for a in self.doc["articles"]]
        self.assertEqual(len(nos), len(set(nos)))

    def test_coverage_declared(self):
        cov = self.doc["coverage"]
        self.assertEqual(cov["total"], 398)
        self.assertEqual(cov["collected"], 398)
        self.assertEqual(cov["missing"], [])

    def test_no_marker_residue_in_text(self):
        """正文不得残留 $f{} 等标记。"""
        for a in self.doc["articles"]:
            self.assertIsNone(re.search(r"\$[afuh]\{", a["text"]),
                              "第 %s 条残留标记" % a["no"])

    def test_raw_text_preserved_for_audit(self):
        """必须保留带标记的原文，便于回溯。"""
        self.assertIn("raw_text", self.doc["articles"][0])

    def test_script_declared_simplified(self):
        """必须如实声明为简体，不得假装繁体。"""
        self.assertEqual(self.doc["script"], "simplified")
        self.assertIn("简繁转换", self.doc["script_note"])

    def test_license_public_domain(self):
        self.assertEqual(self.doc["license_status"], "公有领域")

    def test_sources_recorded(self):
        src = self.doc["source"]
        self.assertIn("https://", src["primary"]["url"])
        self.assertIsNotNone(src["cross_check"])

    def test_verification_numbers_consistent(self):
        v = self.doc["verification"]
        counted = {"verified": 0, "minor_variant": 0, "divergent": 0}
        for a in self.doc["articles"]:
            counted[a["verification"]] = counted.get(a["verification"], 0) + 1
        self.assertEqual(counted["verified"], v["exact_match"])
        self.assertEqual(counted["minor_variant"], v["minor_variant"])
        self.assertEqual(counted["divergent"], v["divergent"])

    def test_divergent_articles_flagged_with_similarity(self):
        for a in self.doc["articles"]:
            if a["verification"] == "divergent":
                self.assertIn("similarity", a, "差异条目必须带相似度：第 %s 条" % a["no"])
                self.assertLess(a["similarity"], 0.9)

    def test_formulas_extracted(self):
        self.assertIn("桂枝汤", self.by_no[12].get("formulas", []))

    def test_sections_present(self):
        self.assertTrue(self.by_no[1]["section"])
        self.assertIn("太阳", self.by_no[1]["section"])


@unittest.skipUnless(os.path.exists(JG_PATH), "金匮产物不存在，跳过")
class TestJinguiArtifact(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(JG_PATH, "r", encoding="utf-8") as fh:
            cls.doc = json.load(fh)

    def test_22_chapters(self):
        self.assertEqual(self.doc["coverage"]["chapters"], 22)

    def test_chapter_numbers_continuous(self):
        nos = [c["no"] for c in self.doc["chapters"]]
        self.assertEqual(nos, list(range(1, 23)))

    def test_first_chapter_title(self):
        self.assertIn("脏腑经络先后病", self.doc["chapters"][0]["title"])

    def test_articles_present(self):
        for c in self.doc["chapters"]:
            self.assertGreater(c["article_count"], 0, "第 %d 篇无条文" % c["no"])

    def test_no_marker_residue(self):
        for c in self.doc["chapters"]:
            for a in c["articles"]:
                self.assertIsNone(re.search(r"\$[afuh]\{", a["text"]))

    def test_script_declared(self):
        self.assertEqual(self.doc["script"], "simplified")

    def test_verification_honest_about_incompleteness(self):
        """逐字比对未完成时必须如实标注，不得虚假声明已核对。"""
        v = self.doc["verification"]
        self.assertFalse(v["cross_checked"])
        self.assertIn("未完成", v["note"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
