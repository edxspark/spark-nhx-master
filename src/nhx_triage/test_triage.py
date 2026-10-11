"""红旗分诊器测试。

关键门槛：**红旗召回率必须 100%**（漏检 = 0）。
误报可接受（设计原则：疑则转诊），漏报不可接受。

运行：python3 src/nhx_triage/test_triage.py
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from triage import load_rules, triage  # noqa: E402


# ── 必须命中的红旗用例（召回率门槛：100%）────────────────────────────────
RED_FLAG_CASES = [
    # (用例说明, 用户输入, 期望类别 id)
    ("心血管-压榨性胸痛", "我爸突然压榨性胸痛，还出大汗", "redflag-cardiac"),
    ("心血管-胸口压石头", "胸口像被石头压住一样闷得慌", "redflag-cardiac"),
    ("心血管-放射痛", "胸闷放射到左臂和下巴", "redflag-cardiac"),
    ("心血管-濒死感", "我有濒死感，快不行了", "redflag-cardiac"),
    ("脑血管-面瘫", "突然嘴歪了，脸一侧没感觉", "redflag-stroke"),
    ("脑血管-单侧无力", "突发半边身子无力，动不了", "redflag-stroke"),
    ("脑血管-言语不清", "突然说话不清，大舌头", "redflag-stroke"),
    ("脑血管-炸裂头痛", "剧烈头痛像爆炸一样", "redflag-stroke"),
    ("呼吸-喘不上气", "喘不上气，呼吸困难", "redflag-respiratory"),
    ("呼吸-口唇发紫", "口唇发紫，指甲也青紫了", "redflag-respiratory"),
    ("呼吸-咯血", "咳嗽咯血了", "redflag-respiratory"),
    ("消化-呕血", "呕血了，吐鲜血", "redflag-gastro"),
    ("消化-黑便", "这两天拉黑便，大便发黑", "redflag-gastro"),
    ("消化-板状腹", "肚子硬得像板一样", "redflag-gastro"),
    ("神经-意识不清", "意识不清，叫他没反应", "redflag-neuro"),
    ("神经-晕厥", "刚才晕厥过去了", "redflag-neuro"),
    ("神经-抽搐", "孩子抽搐了", "redflag-neuro"),
    ("孕产-孕期出血腹痛", "怀孕三个月，现在腹痛还出血", "redflag-pregnancy"),
    ("婴幼儿-高热惊厥", "宝宝发烧还抽搐", "redflag-pediatric"),
    ("精神-自杀意念", "我不想活了，想死", "redflag-psychiatric"),
    ("精神-自伤", "我想割腕自残", "redflag-psychiatric"),
]


# ── 不应命中的普通用例（误报可接受，但这些明显不该报）──────────────────
NON_RED_FLAG_CASES = [
    "桂枝汤的组成是什么？",
    "我想系统学习伤寒论，帮我规划一下学习路径",
    "最近有点失眠，晚上睡不好",
    "帮我看看今年的事业运势",
    "足三里这个穴位怎么定位？",
    "月经量少，颜色淡，平时容易累",
    "我想了解六经辨证的基本框架",
    "家里厨房在西北角，风水上有什么讲究吗？",
]


class TestRecall(unittest.TestCase):
    """召回率门槛：所有红旗用例必须命中。漏检 = 0。"""

    @classmethod
    def setUpClass(cls):
        cls.rules = load_rules()

    def test_zero_missed_red_flags(self):
        missed = []
        for label, text, expected_category in RED_FLAG_CASES:
            result = triage(text, self.rules)
            if not result.hit:
                missed.append("%s | 输入=%r" % (label, text))
                continue
            matched_ids = {m["rule_id"] for m in result.matches}
            if expected_category not in matched_ids:
                missed.append(
                    "%s | 输入=%r | 期望 %s，实际命中 %s"
                    % (label, text, expected_category, sorted(matched_ids))
                )
        self.assertEqual(
            missed,
            [],
            "红旗漏检（漏检必须为 0）：\n  " + "\n  ".join(missed),
        )

    def test_severity_is_critical_for_all_red_flags(self):
        for label, text, _ in RED_FLAG_CASES:
            result = triage(text, self.rules)
            self.assertEqual(result.severity, "critical", "%s 应为 critical" % label)


class TestPrecision(unittest.TestCase):
    """普通问题不应触发分诊。"""

    @classmethod
    def setUpClass(cls):
        cls.rules = load_rules()

    def test_normal_questions_do_not_trigger(self):
        false_positives = []
        for text in NON_RED_FLAG_CASES:
            result = triage(text, self.rules)
            if result.hit:
                false_positives.append(
                    "%r → %s" % (text, [m["matched_text"] for m in result.matches])
                )
        self.assertEqual(
            false_positives,
            [],
            "普通问题被误判为红旗：\n  " + "\n  ".join(false_positives),
        )


class TestNegationAnnotation(unittest.TestCase):
    """否定语境不静默丢弃，只做标注交由上层确认。"""

    @classmethod
    def setUpClass(cls):
        cls.rules = load_rules()

    def test_negated_red_flag_is_annotated_not_dropped(self):
        result = triage("我没有胸痛，也没有大汗", self.rules)
        self.assertTrue(result.hit, "否定语境不得直接放行，必须仍命中并标注")
        self.assertTrue(
            any(m["negation_suspected"] for m in result.matches),
            "应至少有一条命中被标注 negation_suspected",
        )


class TestResponseContract(unittest.TestCase):
    """急症响应的四段固定结构。"""

    @classmethod
    def setUpClass(cls):
        cls.rules = load_rules()

    def test_response_has_four_sections(self):
        result = triage("突然嘴歪了，一侧无力", self.rules)
        self.assertTrue(result.hit)
        for key in ("empathy", "escalation", "boundary", "action"):
            self.assertIn(key, result.response, "急症响应缺少 %s 段" % key)
            self.assertTrue(result.response[key].strip(), "%s 段不得为空" % key)

    def test_psychiatric_has_priority(self):
        """精神危机优先于其他类别。"""
        result = triage("我胸痛，而且我不想活了", self.rules)
        self.assertEqual(result.actions[0], triage("我不想活了", self.rules).actions[0])


class TestNormalization(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rules = load_rules()

    def test_fullwidth_and_spaces(self):
        """全角字符与空格不应导致漏检。"""
        result = triage("我突然嘴歪了，　说话不清", self.rules)
        self.assertTrue(result.hit, "全角空格不应导致漏检")


class TestRuleIntegrity(unittest.TestCase):
    def test_rule_ids_unique(self):
        rules = load_rules()
        ids = [r.id for r in rules]
        self.assertEqual(len(ids), len(set(ids)), "规则 id 重复")

    def test_all_patterns_compile(self):
        rules = load_rules()
        for r in rules:
            self.assertTrue(r.patterns, "%s 没有模式" % r.id)
            for p in r.patterns:
                self.assertTrue(p.strip(), "%s 有空模式" % r.id)

    def test_every_rule_has_action(self):
        for r in load_rules():
            self.assertTrue(r.action.strip(), "%s 缺少 action" % r.id)


if __name__ == "__main__":
    unittest.main(verbosity=2)
