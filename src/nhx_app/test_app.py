"""倪师 AI · 输出守门与对话管线测试

不需要联网：用 FakeLLM 替代真实模型，因此可以完整测通「分诊 → 路由 → 检索 →
提示词 → 生成 → 守门」全链路。

重点：守门是产品的安全兜底，它的**误报与漏报都要有测试**。

运行：python3 src/nhx_app/test_app.py
"""

import os
import urllib.request
import shutil
import sys
import tempfile
import time
import unittest

APP = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(APP, "..", ".."))
sys.path.insert(0, APP)
sys.path.insert(0, os.path.join(APP, ".."))
sys.path.insert(0, REPO)

import guard  # noqa: E402
import prompts  # noqa: E402
from llm import LLMClient, LLMError  # noqa: E402
from kb import KnowledgeBase  # noqa: E402
from pipeline import Pipeline, classify_layer, build_emergency_response  # noqa: E402
from sessions import SessionStore, new_session_id  # noqa: E402


# ── Fake LLM ────────────────────────────────────────────────────────────────

class FakeLLM:
    """按预设文本分片产出，模拟流式。"""

    def __init__(self, text="好的。", fail=None):
        self.text = text
        self.fail = fail
        self.calls = []

    def stream_chat(self, messages):
        self.calls.append(messages)
        if self.fail:
            raise self.fail
        for i in range(0, len(self.text), 7):
            yield self.text[i:i + 7]


class FakeConfig:
    """测试用配置。"""

    def __init__(self, **kw):
        self.kb_enabled = kw.get("kb_enabled", True)
        self.kb_max_snippets = 5
        self.guard_enabled = kw.get("guard_enabled", True)
        self.guard_action = kw.get("guard_action", "block")
        self.max_history_messages = 20
        self.auth_mode = "open"
        self.llm_model = "fake"
        self.contract_path = os.path.join(REPO, "AGENT.md")


# ════════════════════════════════════════════════════════════════════════════
# 守门
# ════════════════════════════════════════════════════════════════════════════

class TestGuardDose(unittest.TestCase):
    """R2 剂量红线。"""

    def test_user_directed_dose_is_high(self):
        f = guard.check_dose("建议你每天服用桂枝 9 克，分两次。")
        self.assertTrue(any(x["level"] == "high" for x in f),
                        "面向用户的剂量必须判为高危")

    def test_herb_adjacent_dose_is_high(self):
        f = guard.check_dose("桂枝 9g、芍药 9g 一起煎。")
        self.assertTrue(any(x["level"] == "high" for x in f))

    def test_educational_conversion_is_not_high(self):
        """教学换算不该被拦——否则知识讲解全废。"""
        f = guard.check_dose("汉代度量衡中，一两约合 13.8 克，这是考古研究的结果。")
        self.assertTrue(all(x["level"] != "high" for x in f),
                        "教学/换算语境的剂量不应判为高危")

    def test_classic_units_alone_no_finding(self):
        f = guard.check_dose("桂枝三两、芍药三两、甘草二两。")
        self.assertEqual(f, [], "纯古籍剂量不应触发剂量告警")

    def test_block_rewrites_user_dose(self):
        text = "建议你每天服用桂枝 9 克。"
        r = guard.guard_output(text, layer="L2")
        self.assertNotIn("9 克", r.text, "高危剂量必须被抹去")
        self.assertIn("执业中医师", r.text)
        self.assertTrue(r.repaired)

    def test_block_keeps_educational_text(self):
        text = "汉代一两约合 13.8 克，属度量衡研究。"
        r = guard.guard_output(text, layer="L1")
        self.assertIn("13.8 克", r.text, "教学换算不应被改写")


class TestGuardR8(unittest.TestCase):
    """R8 不得否定规范治疗。"""

    def test_stop_chemo_is_high(self):
        f = guard.check_r8("你这种情况不要做化疗，化疗没用。")
        self.assertTrue(any(x["level"] == "high" for x in f))

    def test_stop_medication_is_high(self):
        f = guard.check_r8("先把降压药停掉，改吃中药。")
        self.assertTrue(any(x["level"] == "high" for x in f))

    def test_reporting_ni_opinion_is_not_high(self):
        """转述倪师的批评是学术观点，不该按建议处理。"""
        f = guard.check_r8("倪师在讲稿中对化疗有激烈批评，这是他的立场。")
        self.assertTrue(all(x["level"] != "high" for x in f),
                        "转述学术观点不应判为高危")

    def test_block_appends_warning(self):
        r = guard.guard_output("你不要做化疗。", layer="L2")
        self.assertIn("规范治疗", r.text)
        self.assertTrue(r.blocked)


class TestGuardOthers(unittest.TestCase):
    def test_r3_diagnosis_assertion(self):
        f = guard.check_r3("你就是肝郁脾虚证。")
        self.assertTrue(f)

    def test_r5_identity_claim(self):
        f = guard.check_r5("我就是倪海厦本人。")
        self.assertTrue(f)

    def test_disclaimer_injected_when_missing(self):
        r = guard.guard_output("桂枝汤是解表剂。", layer="L2")
        self.assertIn("不构成医疗诊断", r.text)

    def test_disclaimer_not_duplicated(self):
        text = "桂枝汤是解表剂。\n\n以上不构成医疗诊断或处方建议。"
        r = guard.guard_output(text, layer="L2")
        self.assertEqual(r.text.count("不构成医疗诊断"), 1)

    def test_l1_gets_no_l2_disclaimer(self):
        r = guard.guard_output("桂枝汤是解表剂，来自伤寒论。", layer="L1")
        self.assertNotIn("不构成医疗诊断", r.text)

    def test_guard_disabled_passes_through(self):
        text = "建议你吃桂枝 9 克。"
        r = guard.guard_output(text, layer="L2", enabled=False)
        self.assertEqual(r.text, text)
        self.assertFalse(r.repaired)
        self.assertEqual(r.findings, [])

    def test_warn_action_does_not_rewrite(self):
        text = "建议你吃桂枝 9 克。"
        r = guard.guard_output(text, layer="L2", action="warn")
        self.assertEqual(r.text, text)
        self.assertFalse(r.repaired)
        self.assertTrue(r.findings)

    def test_verdict_reported(self):
        self.assertEqual(guard.guard_output("干净的回答。", layer="L1").to_dict()["verdict"],
                         "pass")

    def test_baseline_disclaimer_does_not_raise_verdict(self):
        """补定语是例行基线，不该把 verdict 抬成 repaired。

        否则每次回答都「repaired」，这个信号就废了。
        """
        r = guard.guard_output("桂枝汤是解表剂。", layer="L2")
        self.assertTrue(r.baseline_added)
        self.assertFalse(r.repaired)
        self.assertEqual(r.to_dict()["verdict"], "pass")
        self.assertIn("不构成医疗诊断", r.text)

    def test_real_repair_does_raise_verdict(self):
        r = guard.guard_output("建议你每天服用桂枝 9 克。", layer="L2")
        self.assertTrue(r.repaired)
        self.assertEqual(r.to_dict()["verdict"], "blocked")

    def test_clean_answer_with_disclaimer_is_untouched(self):
        text = ("桂枝汤是解表剂，出自《伤寒论》。\n\n---\n\n"
                "以上为经典与学术思想的分享，不构成医疗诊断或处方建议。")
        r = guard.guard_output(text, layer="L2")
        self.assertFalse(r.baseline_added)
        self.assertFalse(r.repaired)
        self.assertEqual(r.text, text)


# ════════════════════════════════════════════════════════════════════════════
# 路由
# ════════════════════════════════════════════════════════════════════════════

class TestRouting(unittest.TestCase):
    def test_l3_divination(self):
        for q in ["帮我看看八字", "我家风水怎么样", "起一卦测测", "紫微斗数排盘"]:
            self.assertEqual(classify_layer(q), "L3", q)

    def test_l2_self_symptom(self):
        for q in ["我最近总是失眠", "帮我看看口苦怎么办", "孩子老是咳嗽", "我月经量少"]:
            self.assertEqual(classify_layer(q), "L2", q)

    def test_l1_knowledge(self):
        for q in ["伤寒论第 12 条是什么", "桂枝汤的组成", "介绍一下六经辨证"]:
            self.assertEqual(classify_layer(q), "L1", q)

    def test_default_is_l1(self):
        self.assertEqual(classify_layer("你好"), "L1")

    def test_l3_beats_l2(self):
        """术数优先——「我最近失眠，帮我算一卦」应走 L3。"""
        self.assertEqual(classify_layer("我最近失眠，帮我算一卦"), "L3")


# ════════════════════════════════════════════════════════════════════════════
# 急症响应
# ════════════════════════════════════════════════════════════════════════════

class TestEmergency(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pipe = Pipeline(FakeConfig(), llm=FakeLLM(), kb=None)

    def test_emergency_does_not_call_llm(self):
        """急症响应必须是确定性的，不经过模型。"""
        llm = FakeLLM("这段话绝不该出现")
        pipe = Pipeline(FakeConfig(), llm=llm, kb=None)
        events = list(pipe.run("我爸突然压榨性胸痛，还出大汗"))
        self.assertEqual(llm.calls, [], "急症场景绝不能调用 LLM")
        done = [e for e in events if e["event"] == "done"][0]
        self.assertTrue(done["data"]["emergency"])
        self.assertNotIn("这段话绝不该出现", done["data"]["text"])

    def test_emergency_has_four_sections(self):
        events = list(self.pipe.run("突然嘴歪了，一侧无力"))
        text = [e for e in events if e["event"] == "done"][0]["data"]["text"]
        self.assertIn("120", text)
        self.assertIn("立即就医", text)
        # 不得出现具体方名/药名/星曜（「不给方剂建议」这类声明性表述是允许的）
        for bad in ["桂枝汤", "麻黄汤", "小柴胡", "附子", "麻黄", "卦象", "命盘"]:
            self.assertNotIn(bad, text, "急症响应不得包含具体方药/术数内容：%s" % bad)

    def test_emergency_first_event_is_triage(self):
        events = list(self.pipe.run("我胸痛而且不想活了"))
        self.assertEqual(events[0]["event"], "triage")
        self.assertTrue(events[0]["data"]["hit"])


# ════════════════════════════════════════════════════════════════════════════
# 管线
# ════════════════════════════════════════════════════════════════════════════

class TestPipeline(unittest.TestCase):
    def test_event_sequence_normal(self):
        pipe = Pipeline(FakeConfig(), llm=FakeLLM("桂枝汤出自伤寒论。"), kb=None)
        events = [e["event"] for e in pipe.run("桂枝汤的组成是什么")]
        self.assertEqual(events[0], "triage")
        self.assertIn("route", events)
        self.assertIn("sources", events)
        self.assertIn("delta", events)
        self.assertIn("guard", events)
        self.assertEqual(events[-1], "done")

    def test_llm_failure_yields_error_event(self):
        from llm import LLMError
        pipe = Pipeline(FakeConfig(), llm=FakeLLM(fail=LLMError("模型炸了")), kb=None)
        events = list(pipe.run("桂枝汤是什么"))
        names = [e["event"] for e in events]
        self.assertIn("error", names)
        self.assertNotIn("done", names)
        err = [e for e in events if e["event"] == "error"][0]
        self.assertIn("模型炸了", err["data"]["message"])

    def test_guard_runs_on_output(self):
        pipe = Pipeline(FakeConfig(), llm=FakeLLM("建议你每天服用桂枝 9 克。"), kb=None)
        events = list(pipe.run("我失眠怎么办"))
        done = [e for e in events if e["event"] == "done"][0]
        self.assertNotIn("9 克", done["data"]["text"], "输出守门必须生效")
        self.assertIsNotNone(done["data"]["raw_text"], "改写后应保留原文供复核")

    def test_prompt_contains_contract(self):
        pipe = Pipeline(FakeConfig(), llm=FakeLLM("ok"), kb=None)
        list(pipe.run("桂枝汤是什么"))
        system = pipe.llm.calls[0][0]["content"]
        self.assertIn("红线", system)
        self.assertIn("R1", system)

    def test_prompt_excludes_dev_sections(self):
        """工程章节不该进系统提示词。"""
        pipe = Pipeline(FakeConfig(), llm=FakeLLM("ok"), kb=None)
        self.assertNotIn("命名与目录", pipe.contract)
        self.assertNotIn("Agent Preset", pipe.contract)

    def test_layer_specific_rules_injected(self):
        for q, expect in [("桂枝汤的组成", "L1 学习层"),
                          ("我最近失眠", "L2 参考层"),
                          ("帮我看看八字", "L3 文化层")]:
            pipe = Pipeline(FakeConfig(), llm=FakeLLM("ok"), kb=None)
            list(pipe.run(q))
            system = pipe.llm.calls[0][0]["content"]
            self.assertIn(expect, system, q)

    def test_history_is_passed(self):
        pipe = Pipeline(FakeConfig(), llm=FakeLLM("ok"), kb=None)
        hist = [{"role": "user", "content": "上一句"},
                {"role": "assistant", "content": "上一答"}]
        list(pipe.run("桂枝汤是什么", history=hist))
        msgs = pipe.llm.calls[0]
        self.assertEqual(msgs[1]["content"], "上一句")
        self.assertEqual(msgs[2]["content"], "上一答")
        self.assertEqual(msgs[-1]["role"], "user")


# ════════════════════════════════════════════════════════════════════════════
# 知识库
# ════════════════════════════════════════════════════════════════════════════

class TestKnowledgeBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kb = KnowledgeBase(os.path.join(REPO, "knowledge"), 5)
        cls.kb.build(force=True)

    def test_indexes_structured_data(self):
        """回归：结构化数据目录必须被索引，否则精确查询全失效。"""
        kinds = self.kb.stats()["by_kind"]
        self.assertIn("formula", kinds, "方剂库未被索引")
        self.assertIn("herb", kinds, "本草库未被索引")

    def test_find_formula_exact(self):
        f = self.kb.find_formula("桂枝汤")
        self.assertIsNotNone(f)
        self.assertEqual(len(f["composition"]), 5)
        self.assertEqual(f["composition"][0]["original_amount"], "三两")

    def test_find_formula_withholds_dosage(self):
        f = self.kb.find_formula("桂枝汤")
        self.assertTrue(f.get("dosage_withheld"))

    def test_find_herb_with_toxicity(self):
        h = self.kb.find_herb("附子")
        self.assertIsNotNone(h)
        self.assertEqual(h["toxicity"]["level"], "大毒")
        self.assertTrue(h["toxicity"]["requires_physician"])
        self.assertTrue(h["toxicity"]["dosage_withheld"])

    def test_missing_lookup_returns_none(self):
        self.assertIsNone(self.kb.find_formula("不存在的方"))
        self.assertIsNone(self.kb.find_herb("不存在的药"))

    def test_search_finds_formula(self):
        r = self.kb.search("桂枝汤 组成", limit=3)
        self.assertTrue(r)
        self.assertEqual(r[0]["title"], "桂枝汤")

    def test_search_no_false_positive_on_truly_absent_term(self):
        """二元组切分后，**确实不在库里**的词不该命中。

        注意：早先这个用例用的是「少阳/口苦」——那时库里只有 2 篇示例数据，
        现在《伤寒论》398 条已入库，第 263 条正是「少阳之为病。口苦。咽干。目眩也。」，
        命中是**正确**的。因此改用真正无关的词。
        """
        for q in ["量子纠缠", "阿司匹林 副作用", "股票 投资组合"]:
            with self.subTest(q=q):
                self.assertEqual(self.kb.search(q, limit=3), [],
                                 "库里没有的内容不应有命中：%s" % q)

    def test_meta_files_excluded_from_search_by_default(self):
        """README / 采集报告等说明文件不是知识源，不得进检索结果。

        否则模型可能把「采集报告」当成经典原文依据引用。
        """
        for r in self.kb.search("少阳 采集 报告 原文", limit=10):
            self.assertNotEqual(r["kind"], "meta",
                                "meta 文件不应出现在默认检索结果中：%s" % r["title"])

    def test_meta_only_when_explicitly_requested(self):
        r = self.kb.search("采集报告", limit=5, kinds=["classical", "meta"])
        self.assertTrue(any(x["kind"] == "meta" for x in r),
                        "显式请求时应能取到 meta")

    def test_search_finds_classical_after_import(self):
        """经典原文入库后，相关词必须能命中。"""
        r = self.kb.search("少阳 口苦", limit=3)
        self.assertTrue(r, "少阳/口苦 应能命中第 263 条")
        self.assertTrue(any(x["kind"] == "classical" for x in r))

    def test_structured_records_given_in_full(self):
        """回归：方剂/本草必须**整条**给模型，不能只给截断片段。

        只给片段时，模型无法逐字复述组成，只能含糊或凭记忆补——
        那违反 R1。实测：桂枝汤组成被截断后模型明确拒绝补全。
        姿态是对的，但根因是我们没给全材料。
        """
        block = self.kb.context_block("桂枝汤的组成", "L1")
        self.assertIn("完整记录", block)
        # 组成里的全部五味都应出现
        for herb in ["桂枝", "芍药", "甘草", "生姜", "大枣"]:
            self.assertIn(herb, block, "方剂整条给出时不应缺 %s" % herb)
        # 煎服法原文也应在
        self.assertIn("微火煮取三升", block)

    def test_herb_toxicity_field_reaches_model(self):
        """毒性药字段必须进上下文，否则 R9 无从落实。"""
        block = self.kb.context_block("附子", "L1")
        self.assertIn("toxicity", block)
        self.assertIn("requires_physician", block)

    def test_classical_uses_snippet_not_full(self):
        """经典原文体量大，仍用片段（避免撑爆上下文）。"""
        block = self.kb.context_block("太阳病", "L1")
        self.assertNotIn("完整记录", block.split("### 伤寒论")[1][:200]
                         if "### 伤寒论" in block else "")

    def test_article_refs_parsed(self):
        from kb import extract_article_refs
        self.assertEqual(extract_article_refs("伤寒论第 12 条讲什么"), [12])
        self.assertEqual(extract_article_refs("第十二条"), [12])
        self.assertEqual(extract_article_refs("第96条"), [96])
        self.assertEqual(extract_article_refs("我最近失眠"), [])
        self.assertEqual(extract_article_refs("第 110 条和第 12 条"), [110, 12])

    def test_cn_to_int(self):
        from kb import cn_to_int
        for text, expect in [("十二", 12), ("九十六", 96), ("一百", 100),
                             ("三百九十八", 398), ("三", 3), ("12", 12)]:
            self.assertEqual(cn_to_int(text), expect, text)

    def test_exact_article_beats_keyword_search(self):
        """回归：问第 12 条必须给第 12 条。

        关键词打分对条号查询是失效的——整部伤寒论是一个大文本块，
        打分只会在块内挑片段，实测问第 12 条返回了第 110～112 条。
        """
        block = self.kb.context_block("伤寒论第 12 条讲什么", "L1")
        self.assertIn("《伤寒论》第 12 条", block)
        self.assertIn("太阳中风。阳浮而阴弱。", block)
        # 不得混入其它条号的原文片段
        self.assertNotIn("第 111 条", block)
        self.assertNotIn("第 112 条", block)

    def test_chinese_numeral_article_ref(self):
        block = self.kb.context_block("第十二条", "L1")
        self.assertIn("《伤寒论》第 12 条", block)

    def test_missing_article_tells_model_not_to_fabricate(self):
        block = self.kb.context_block("第 999 条", "L1")
        self.assertIn("未找到", block)
        self.assertIn("不要凭记忆补写原文", block)

    def test_structure_hits_kept_alongside_exact(self):
        """条号命中时，方剂/本草等结构化条目应保留（是有用补充）。"""
        block = self.kb.context_block("第十二条", "L1")
        self.assertIn("桂枝汤", block)
        self.assertIn("完整记录", block)

    def test_context_block_mentions_no_fabrication_rule(self):
        block = self.kb.context_block("桂枝汤", "L1")
        self.assertIn("知识库暂未收录", block)


# ════════════════════════════════════════════════════════════════════════════
# 会话
# ════════════════════════════════════════════════════════════════════════════

class TestSessions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nhx-sess-")
        self.store = SessionStore(self.tmp, ttl_sec=3600)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_create_and_get(self):
        doc = self.store.create("u1")
        self.assertIsNotNone(self.store.get(doc["session_id"]))

    def test_append_and_history(self):
        doc = self.store.create("u1")
        sid = doc["session_id"]
        self.store.append_message(sid, "user", "你好")
        self.store.append_message(sid, "assistant", "你好，我是倪师 AI")
        h = self.store.history_for_llm(sid)
        self.assertEqual(len(h), 2)
        self.assertEqual(h[0]["role"], "user")

    def test_invalid_session_id_rejected(self):
        for bad in ["../etc/passwd", "a", "x" * 100, "has space"]:
            with self.assertRaises(ValueError):
                self.store.get(bad)

    def test_missing_session_returns_none(self):
        self.assertIsNone(self.store.get("s-" + "a" * 20))

    def test_expiry(self):
        store = SessionStore(self.tmp, ttl_sec=0)
        doc = store.create("u1")
        time.sleep(0.01)
        self.assertIsNone(store.get(doc["session_id"]))

    def test_delete(self):
        doc = self.store.create("u1")
        sid = doc["session_id"]
        self.assertTrue(self.store.delete(sid))
        self.assertIsNone(self.store.get(sid))

    def test_message_truncation(self):
        doc = self.store.create("u1")
        self.store.append_message(doc["session_id"], "user", "x" * 50000)
        h = self.store.history_for_llm(doc["session_id"])
        self.assertLessEqual(len(h[0]["content"]), 20000)

    def test_purge_expired(self):
        store = SessionStore(self.tmp, ttl_sec=0)
        store.create("u1")
        store.create("u2")
        time.sleep(0.01)
        self.assertEqual(store.purge_expired(), 2)

    def test_session_id_shape(self):
        self.assertTrue(new_session_id().startswith("s-"))


# ════════════════════════════════════════════════════════════════════════════
# 提示词
# ════════════════════════════════════════════════════════════════════════════

class TestPrompts(unittest.TestCase):
    def test_contract_loaded(self):
        c = prompts.load_contract(os.path.join(REPO, "AGENT.md"))
        self.assertIn("红线", c)
        self.assertIn("R10", c)
        self.assertIn("分诊", c)

    def test_contract_drops_dev_sections(self):
        c = prompts.load_contract(os.path.join(REPO, "AGENT.md"))
        for dev in ["命名与目录", "媒体资源管理", "交付规范"]:
            self.assertNotIn(dev, c, "工程章节 %s 不应进提示词" % dev)

    def test_missing_contract_returns_empty(self):
        self.assertEqual(prompts.load_contract("/nonexistent/AGENT.md"), "")

    def test_system_prompt_has_identity(self):
        sp = prompts.build_system_prompt("契约内容", "L2", "")
        self.assertIn("不是倪海厦本人", sp)
        self.assertIn("L2 参考层", sp)

    def test_emergency_prompt_forbids_diagnosis(self):
        p = prompts.build_emergency_prompt(["立即拨打 120"], ["心血管急症"])
        self.assertIn("绝对禁止", p)
        self.assertIn("心血管急症", p)


class TestLLMClient(unittest.TestCase):
    """回归：操作系统代理导致 LLM 调用失败（实测踩到的坑）。

    macOS 装了 Clash/Surge 时 `urllib.getproxies()` 会返回本地代理，
    urllib **默认**把所有请求（含 127.0.0.1）塞进代理，表现为莫名的
    RemoteDisconnected；更严重的是 API Key 会经过第三方进程。
    因此改用 http.client（不读系统代理，行为可预测）。
    """

    def test_parses_base_url(self):
        from llm import parse_base_url
        self.assertEqual(parse_base_url("https://api.deepseek.com/v1"),
                         ("https", "api.deepseek.com", 443, "/v1"))
        self.assertEqual(parse_base_url("http://127.0.0.1:8799/v1"),
                         ("http", "127.0.0.1", 8799, "/v1"))
        self.assertEqual(parse_base_url("http://host"), ("http", "host", 80, ""))

    def test_rejects_bad_scheme(self):
        from llm import parse_base_url
        for bad in ["ftp://x/v1", "api.deepseek.com/v1", ""]:
            with self.subTest(bad=bad):
                with self.assertRaises(LLMError):
                    parse_base_url(bad)

    def test_client_does_not_use_urllib_urlopen(self):
        """确保不再依赖 urllib.request.urlopen —— 它会读系统代理。"""
        import llm
        src = open(llm.__file__, encoding="utf-8").read()
        self.assertNotIn("urllib.request.urlopen(", src,
                         "不得调用 urllib.request.urlopen（会走系统代理）")
        self.assertIn("http.client", src)

    def test_missing_key_raises_not_configured(self):
        c = LLMClient("http://127.0.0.1:9/v1", "", "m")
        with self.assertRaises(LLMError) as ctx:
            list(c.stream_chat([{"role": "user", "content": "x"}]))
        self.assertIn("NHX_LLM_API_KEY", str(ctx.exception))

    def test_unreachable_host_is_retryable(self):
        c = LLMClient("http://127.0.0.1:9/v1", "k", "m", timeout=1, max_retries=0)
        with self.assertRaises(LLMError) as ctx:
            c.chat([{"role": "user", "content": "x"}])
        self.assertTrue(ctx.exception.retryable)

    def test_sse_parser(self):
        """SSE 解析：注释行、坏 JSON、空 choices、[DONE] 之后的内容。"""
        payload = (
            b": keepalive\n"
            b'data: {"choices":[{"delta":{"content":"\xe6\xa1\x82"}}]}\n'
            b"data: not-json\n"
            b'data: {"choices":[]}\n'
            b'data: {"choices":[{"delta":{"content":"\xe6\x9e\x9d"}}]}\n'
            b"data: [DONE]\n"
            b'data: {"choices":[{"delta":{"content":"XXX"}}]}\n'
        )

        class FakeResp:
            def __init__(self, data):
                self.data = data
                self.i = 0

            def read(self, n=1):
                if self.i >= len(self.data):
                    return b""
                out = self.data[self.i:self.i + n]
                self.i += n
                return out

        parts = list(LLMClient._iter_sse(FakeResp(payload)))
        self.assertEqual(parts, ["\u6842", "\u679d"], "[DONE] 之后的内容必须丢弃")


class TestStreamingGuard(unittest.TestCase):
    """**流式守门**：违规文本必须在到达用户之前被拦下。

    这是一个真实缺陷的回归测试：最初实现是「全文生成完再守门」，
    结果流式场景下违规文本早已逐字发到用户屏幕，守门形同虚设。
    """

    def _run(self, reply, **cfg):
        pipe = Pipeline(FakeConfig(**cfg), llm=FakeLLM(reply), kb=None)
        seen = ""
        events = []
        for e in pipe.run("给我一个剂量建议"):
            events.append(e)
            if e["event"] == "delta":
                seen += e["data"]["text"]
        return seen, events

    def test_dose_never_reaches_user(self):
        seen, _ = self._run("建议你每天服用桂枝 9 克，分两次温服。")
        self.assertNotIn("9 克", seen, "违规剂量绝不能出现在用户看到的文本里")
        self.assertIn("执业中医师面诊确定", seen)

    def test_verdict_reflects_stream_block(self):
        """文档级守门跑在已净化文本上，看不到流式段拦下的违规——必须合并。"""
        _, events = self._run("建议你每天服用桂枝 9 克。")
        g = [e for e in events if e["event"] == "guard"][0]["data"]
        self.assertEqual(g["verdict"], "blocked")
        self.assertGreater(g["stream_findings"], 0)
        self.assertEqual(g["worst_level"], "high")

    def test_r8_gets_immediate_counter_notice(self):
        seen, _ = self._run("你不要做化疗，把降压药停掉。")
        self.assertIn("请勿据此停用", seen)

    def test_clean_text_passes_through_unchanged(self):
        seen, events = self._run("桂枝汤出自《伤寒论》。")
        self.assertIn("桂枝汤出自", seen)
        g = [e for e in events if e["event"] == "guard"][0]["data"]
        self.assertEqual(g["stream_findings"], 0)

    def test_context_across_sentence_boundary(self):
        """药材名在前一句、剂量在后一句时仍要检出。"""
        seen, _ = self._run("这方子里有桂枝。每天 9 克。")
        self.assertNotIn("9 克", seen, "跨句上下文不应导致漏检")

    def test_guard_disabled_streams_raw(self):
        seen, events = self._run("建议你每天服用桂枝 9 克。", guard_enabled=False)
        self.assertIn("9 克", seen, "关闭守门时应原样输出")
        g = [e for e in events if e["event"] == "guard"][0]["data"]
        self.assertEqual(g["stream_findings"], 0)

    def test_no_text_lost_across_boundaries(self):
        """守门不得吞字。"""
        reply = "第一句。第二句！第三句？最后没有标点"
        seen, _ = self._run(reply)
        for frag in ["第一句", "第二句", "第三句", "最后没有标点"]:
            self.assertIn(frag, seen, "丢失片段：%s" % frag)


class TestBrowsableUrls(unittest.TestCase):
    """回归：把**绑定地址**当成**访问地址**打印，导致用户拿到打不开的 URL。

    实测现象：按启动日志访问 http://0.0.0.0:18790 页面全白——
    Chrome 128+ 屏蔽对 0.0.0.0 的导航与 fetch（防 DNS rebinding），
    服务器日志却显示 200，极难排查。
    """

    def test_wildcard_becomes_localhost(self):
        from server import browsable_urls
        urls = browsable_urls("0.0.0.0", 18790)
        self.assertEqual(urls[0], "http://127.0.0.1:18790")
        self.assertNotIn("0.0.0.0", urls[0], "绝不能把绑定地址当访问地址")

    def test_ipv6_wildcard(self):
        from server import browsable_urls
        self.assertEqual(browsable_urls("::", 8790)[0], "http://127.0.0.1:8790")

    def test_specific_host_kept(self):
        from server import browsable_urls
        self.assertEqual(browsable_urls("192.168.1.5", 8790),
                         ["http://192.168.1.5:8790"])

    def test_all_returned_urls_are_browsable(self):
        from server import browsable_urls
        for host in ("0.0.0.0", "::", "127.0.0.1", "10.0.0.2"):
            for u in browsable_urls(host, 8080):
                self.assertNotIn("0.0.0.0", u)
                self.assertTrue(u.startswith("http://"))

    def test_loopback_not_duplicated(self):
        from server import browsable_urls
        urls = browsable_urls("0.0.0.0", 8790)
        self.assertEqual(len(urls), len(set(urls)), "地址不得重复")


if __name__ == "__main__":
    unittest.main(verbosity=2)
