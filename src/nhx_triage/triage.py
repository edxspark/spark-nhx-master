"""倪师 AI · 红旗分诊器（Red Flag Triage）

用途
----
在进入能力闸门（L1/L2/L3）之前，对用户输入做急症筛查。
命中即中断正常流程，输出急症响应，不提供辨证、方剂或术数内容。

设计原则
--------
**疑则转诊** —— 宁可误报，不可漏报。
误报的代价是用户多跑一趟医院；漏报的代价不可逆。
因此本模块**永不静默丢弃**任何潜在红旗：即使检测到否定语境，
也只做标注（`negation_suspected`）交由上层确认，而不是直接放行。

零依赖：仅使用 Python 标准库，可直接运行与测试。
"""

import json
import os
import re
import unicodedata
from typing import Any, Dict, List, Optional

_RULES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "red_flags.json")

# 精神危机优先级最高：不带评判、直给资源，不与其他类别混排。
_PRIORITY_ORDER = [
    "redflag-psychiatric",
    "redflag-cardiac",
    "redflag-stroke",
    "redflag-respiratory",
    "redflag-gastro",
    "redflag-neuro",
    "redflag-pregnancy",
    "redflag-pediatric",
]

# 否定语境标记（出现在命中位置之前的短窗口内）
_NEGATION_MARKERS = ("没有", "不", "无", "未", "别", "非", "否认", "排除", "不会", "不太")

# 急症响应的固定四段模板
_EMERGENCY_TEMPLATE = {
    "empathy": "我理解你现在的难受和担心。",
    "escalation": "你描述的情况需要立即就医，这已经超出我作为中医知识助手能处理的范围。",
    "boundary": "这个时间点上，我不会给出辨证分析或方剂建议——那会耽误你真正需要的救治。",
}


class RedFlagRule:
    """一条红旗规则。"""

    def __init__(self, raw: Dict[str, Any]) -> None:
        self.id: str = raw["id"]
        self.category: str = raw["category"]
        self.severity: str = raw["severity"]
        self.action: str = raw["action"]
        self.note: str = raw.get("note", "")
        self.patterns: List[str] = raw["patterns"]
        self._compiled = [re.compile(p) for p in self.patterns]

    def match(self, text: str) -> List[Dict[str, Any]]:
        """返回所有命中片段（含位置与否定语境标注）。"""
        hits: List[Dict[str, Any]] = []
        for pattern, compiled in zip(self.patterns, self._compiled):
            for m in compiled.finditer(text):
                start = m.start()
                window = text[max(0, start - 6):start]
                negated = any(marker in window for marker in _NEGATION_MARKERS)
                hits.append(
                    {
                        "pattern": pattern,
                        "span": [start, m.end()],
                        "matched_text": m.group(0),
                        "negation_suspected": negated,
                    }
                )
                break  # 同一正则已命中，无需重复
        return hits


class TriageResult:
    """分诊结果。"""

    def __init__(
        self,
        hit: bool,
        severity: Optional[str],
        matches: List[Dict[str, Any]],
        actions: List[str],
        categories: List[str],
        response: Optional[Dict[str, str]],
    ) -> None:
        self.hit = hit
        self.severity = severity
        self.matches = matches
        self.actions = actions
        self.categories = categories
        self.response = response

    def to_dict(self) -> Dict[str, Any]:
        return {
            "hit": self.hit,
            "severity": self.severity,
            "categories": self.categories,
            "actions": self.actions,
            "matches": self.matches,
            "response": self.response,
            "escalated": self.hit,
            "note": "命中即中断正常流程；不提供辨证、方剂或术数内容。" if self.hit else "",
        }


def _normalize(text: str) -> str:
    """全角转半角、去除多余空白，避免因输入法差异漏检。"""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"\s+", "", text)
    return text


def load_rules(path: Optional[str] = None) -> List[RedFlagRule]:
    """从 JSON 加载规则。"""
    with open(path or _RULES_PATH, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return [RedFlagRule(raw) for raw in data["categories"]]


def _sort_key(rule: RedFlagRule) -> int:
    try:
        return _PRIORITY_ORDER.index(rule.id)
    except ValueError:
        return len(_PRIORITY_ORDER)


def triage(text: str, rules: Optional[List[RedFlagRule]] = None) -> TriageResult:
    """对一段用户输入做红旗筛查。

    返回的 `hit=True` 表示必须中断正常流程、走急症响应。
    """
    rules = rules if rules is not None else load_rules()
    normalized = _normalize(text)

    matched_rules: List[RedFlagRule] = []
    all_matches: List[Dict[str, Any]] = []

    for rule in sorted(rules, key=_sort_key):
        hits = rule.match(normalized)
        if hits:
            matched_rules.append(rule)
            for h in hits:
                h["rule_id"] = rule.id
                h["category"] = rule.category
                h["severity"] = rule.severity
                all_matches.append(h)

    if not matched_rules:
        return TriageResult(False, None, [], [], [], None)

    severity = "critical" if any(r.severity == "critical" for r in matched_rules) else "urgent"
    actions = [r.action for r in matched_rules]
    categories = [r.category for r in matched_rules]

    response = dict(_EMERGENCY_TEMPLATE)
    response["action"] = actions[0]
    response["categories"] = "、".join(categories)

    return TriageResult(True, severity, all_matches, actions, categories, response)


def _cli() -> None:
    import sys

    if len(sys.argv) < 2:
        print("用法: python3 triage.py '用户输入文本'")
        raise SystemExit(2)
    result = triage(" ".join(sys.argv[1:]))
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _cli()
