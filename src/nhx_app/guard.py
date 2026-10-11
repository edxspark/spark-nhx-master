"""倪师 AI · 输出守门（Output Guard）

**这是本产品最重要的安全机制。**

提示词里的红线是「请求模型遵守」；守门是「不管模型是否遵守，结果都被检查」。
两层都需要：提示词降低违规概率，守门兜住漏网的那一次。

检查项与对应红线
----------------
| 规则 | 红线 | 处理 |
|---|---|---|
| G-DOSE  | R2 剂量自定 | 检出面向用户的现代剂量 → 拦下并改写 |
| G-R8    | R8 否定规范治疗 | 检出劝停规范治疗 → 拦下 |
| G-R3    | R3 确诊断言 | 检出确诊式断言 → 改写为「倾向」措辞 |
| G-R5    | R5 身份冒用 | 检出自称倪海厦本人 → 拦下 |
| G-DISC  | 合规定语 | L2/L3 缺定语 → 自动补上 |
| G-TRACE | R1 溯源 | 无来源标记 → 提示（不阻断） |

剂量检测的关键设计
------------------
不能一见到「克」就拦——「汉代一两约合 13.8 克」是正当的知识讲解。
因此按**语境**分级：

- **user_directed**（你/您/建议服用/每天…）→ 高危，拦下
- **educational**（约合/相当于/换算/考古/汉代度量衡）→ 仅告警，放行

宁可多拦一次，也不要放过一条「你吃 9 克」。
"""

import re
from typing import Any, Dict, List, Optional

# ── 剂量检测 ────────────────────────────────────────────────────────────────

# 现代剂量单位
_MODERN_DOSE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(mg|毫克|g\b|克|kg|千克|ml|毫升|IU|单位)"
)

# 古籍剂量单位（出现即认为是原文引用，放行）
_CLASSIC_UNITS = (
    "两", "斤", "升", "合", "枚", "个", "分", "铢", "把", "片", "寸",
    "斗", "撮", "茎", "具", "束", "方寸匕", "钱匕", "字", "碗", "杯",
)

# 面向用户的措辞（高危）
_USER_DIRECTED = (
    "你", "您", "建议", "可以服", "每天", "每日", "日服", "一次", "分两次",
    "每次", "服用", "泡水", "煎服", "给他", "给她", "给孩子", "剂量是",
)

# 教学/换算语境（低危）
_EDUCATIONAL = (
    "约合", "相当于", "换算", "考古", "度量衡", "汉代", "东汉", "一两",
    "研究", "记载", "原书", "折算", "折合", "大约等于",
)

# 药材名（用于判断剂量是否与药相关）
#
# 两条并用：常见药名**白名单**（准）+ 形声后缀（广）。
# 只靠后缀会漏掉「桂枝」「麻黄」这类不以草/根/皮结尾的药名。
_COMMON_HERBS = (
    "桂枝", "肉桂", "芍药", "白芍", "赤芍", "麻黄", "附子", "川乌", "草乌",
    "甘草", "生姜", "干姜", "大枣", "半夏", "茯苓", "白术", "苍术", "人参",
    "党参", "黄芪", "当归", "川芎", "熟地", "生地", "柴胡", "黄芩", "黄连",
    "黄柏", "栀子", "石膏", "知母", "大黄", "芒硝", "厚朴", "枳实", "杏仁",
    "桃仁", "五味子", "酸枣仁", "远志", "龙骨", "牡蛎", "细辛", "吴茱萸",
    "升麻", "葛根", "桔梗", "陈皮", "青皮", "香附", "木香", "砂仁", "藿香",
    "金银花", "连翘", "薄荷", "桑叶", "菊花", "枸杞", "山茱萸", "山药",
    "泽泻", "猪苓", "滑石", "车前子", "牛膝", "杜仲", "续断", "阿胶",
    "马钱子", "巴豆", "甘遂", "大戟", "芫花", "水蛭", "虻虫", "斑蝥",
    "乌头", "天南星", "白附子", "雄黄", "朱砂", "蟾酥", "洋金花",
)
_HERB_HINT = re.compile(
    r"[\u4e00-\u9fa5]{1,4}(?:汤|散|丸|饮|子|草|根|皮|仁|叶|花|姜|枣|参|芪|归|芍|桂|麻|附|夏|苓|术|甘)"
)


def _has_herb(ctx: str) -> bool:
    return any(h in ctx for h in _COMMON_HERBS) or bool(_HERB_HINT.search(ctx))


def _context(text: str, start: int, end: int, window: int = 30) -> str:
    return text[max(0, start - window):min(len(text), end + window)]


def check_dose(text: str) -> List[Dict[str, Any]]:
    """检测现代剂量。按语境分级，避免误伤知识讲解。"""
    findings = []
    for m in _MODERN_DOSE.finditer(text):
        ctx = _context(text, m.start(), m.end())
        has_user = any(w in ctx for w in _USER_DIRECTED)
        has_edu = any(w in ctx for w in _EDUCATIONAL)
        has_herb = _has_herb(ctx)

        if has_edu and not has_user:
            level = "info"
            reason = "教学/换算语境下的剂量，属正当知识讲解"
        elif has_user or has_herb:
            level = "high"
            reason = "面向用户的剂量建议（红线 R2）"
        else:
            level = "medium"
            reason = "出现现代剂量，语境不明确"

        findings.append({
            "rule": "G-DOSE",
            "redline": "R2",
            "level": level,
            "matched": m.group(0),
            "span": [m.start(), m.end()],
            "context": ctx.strip(),
            "reason": reason,
        })
    return findings


def check_classic_units(text: str) -> List[Dict[str, Any]]:
    """确认古籍剂量是否以原文形式呈现（信息项，不阻断）。"""
    out = []
    for u in _CLASSIC_UNITS:
        if u in text:
            out.append({"rule": "G-CLASSIC", "redline": "R1", "level": "info",
                        "matched": u, "reason": "出现古籍剂量单位"})
            break
    return out


# ── R8 否定规范治疗 ─────────────────────────────────────────────────────────

_R8_PATTERNS = [
    (r"(不要|别|不能|无需|不必|建议停|赶紧停|停掉)[^。；\n]{0,10}(化疗|放疗)", "劝停化疗/放疗"),
    (r"(化疗|放疗)[^。；\n]{0,8}(没用|无效|有害|害人|别做|不要做)", "否定放化疗"),
    (r"(停|停掉|停用|别吃|不要吃|戒掉)[^。；\n]{0,10}(降压药|降糖药|胰岛素|西药)", "劝停规范用药"),
    (r"(降压药|降糖药|胰岛素|西药)[^。；\n]{0,8}(停掉|停用|停了|别吃|不要吃|可以停|该停)", "劝停规范用药"),
    (r"(胰岛素|降压药|降糖药)[^。；\n]{0,8}(是错的|有害|别用|不要用)", "否定规范用药"),
    (r"(西药|西医)[^。；\n]{0,6}(都是|全是)[^。；\n]{0,6}(毒|害|骗)", "全盘否定西医"),
]
_R8_ALLOW = ("倪师", "他认为", "他的立场", "讲稿中", "批评", "学术观点", "历史上")


def check_r8(text: str) -> List[Dict[str, Any]]:
    """检测否定规范治疗的**建议**。

    注意：讲「倪师批评过化疗」是学术观点转述，允许；
    说「你别去化疗」是对用户的建议，拦下。
    """
    findings = []
    for pattern, label in _R8_PATTERNS:
        for m in re.finditer(pattern, text):
            ctx = _context(text, m.start(), m.end(), 40)
            # 转述语境：在句子范围内出现转述标记则降级
            sentence_start = max(
                text.rfind("。", 0, m.start()), text.rfind("\n", 0, m.start())) + 1
            sentence = text[sentence_start:m.end() + 10]
            if any(w in sentence for w in _R8_ALLOW):
                level = "medium"
                reason = "疑似转述学术观点（需人工确认是否被当作建议）"
            else:
                level = "high"
                reason = label
            findings.append({
                "rule": "G-R8", "redline": "R8", "level": level,
                "matched": m.group(0), "span": [m.start(), m.end()],
                "context": ctx.strip(), "reason": reason,
            })
    return findings


# ── R3 确诊断言 ─────────────────────────────────────────────────────────────

_R3_PATTERNS = [
    r"你(就是|得的?是|确诊为)[^。；\n]{0,12}(病|证|症|炎|癌)",
    r"(确诊|断定|肯定)你[^。；\n]{0,8}(是|有|得)",
    r"你(一定|肯定)是[^。；\n]{0,10}(病|证)",
]


def check_r3(text: str) -> List[Dict[str, Any]]:
    findings = []
    for pattern in _R3_PATTERNS:
        for m in re.finditer(pattern, text):
            findings.append({
                "rule": "G-R3", "redline": "R3", "level": "medium",
                "matched": m.group(0), "span": [m.start(), m.end()],
                "context": _context(text, m.start(), m.end()),
                "reason": "确诊式断言，应改为「倾向 / 可能性」措辞",
            })
    return findings


# ── R5 身份冒用 ─────────────────────────────────────────────────────────────

_R5_PATTERNS = [
    r"我(就)?是倪海厦",
    r"我是倪师",
    r"我(本人|就是本人)",
    r"(作为|身为)倪海厦",
]


def check_r5(text: str) -> List[Dict[str, Any]]:
    findings = []
    for pattern in _R5_PATTERNS:
        for m in re.finditer(pattern, text):
            findings.append({
                "rule": "G-R5", "redline": "R5", "level": "high",
                "matched": m.group(0), "span": [m.start(), m.end()],
                "context": _context(text, m.start(), m.end()),
                "reason": "自称倪海厦本人（红线 R5）",
            })
    return findings


# ── 合规定语 ────────────────────────────────────────────────────────────────

DISCLAIMERS = {
    "L1": "本内容为中医经典与倪海厦先生学术思想的整理分享，仅供学习研究。",
    "L2": ("以上为经典与学术思想的分享，**不构成医疗诊断或处方建议**。"
           "个体差异极大，剂量与用法请以执业中医师面诊为准。若症状持续或加重，请及时就医。"),
    "L3": "以上内容属传统文化研究与娱乐参考，不作为任何决策依据。",
}

_DISCLAIMER_MARKERS = ("不构成医疗诊断", "不构成诊疗", "仅供参考", "不构成处方建议", "仅供学习研究")

_TRACE_RE = re.compile(r"［来源[:：]|\[来源[:：]|来源[:：]")


def check_disclaimer(text: str, layer: str) -> List[Dict[str, Any]]:
    if layer not in DISCLAIMERS:
        return []
    if any(mk in text for mk in _DISCLAIMER_MARKERS):
        return []
    return [{
        "rule": "G-DISC", "redline": "-", "level": "medium",
        "matched": "", "span": None,
        "context": "",
        "reason": "%s 层回答缺少合规定语，已自动补上" % layer,
        "auto_fix": DISCLAIMERS[layer],
    }]


def check_trace(text: str) -> List[Dict[str, Any]]:
    """实质性结论应有来源标记。仅提示，不阻断。"""
    if len(text) < 200:
        return []
    if _TRACE_RE.search(text):
        return []
    return [{
        "rule": "G-TRACE", "redline": "R1", "level": "info",
        "matched": "", "span": None, "context": "",
        "reason": "较长的实质性回答未见来源标记，建议补充溯源",
    }]


# ── 主入口 ──────────────────────────────────────────────────────────────────

_LEVEL_ORDER = {"info": 0, "medium": 1, "high": 2}


class GuardResult:
    """守门结果。

    `repaired` 与 `baseline_added` 分开计数。否则「每次都补定语」会让
    `repaired` 恒为真，verdict 就失去信号价值：

      - `repaired`       内容被**实质修改**（抹去剂量、追加违规澄清）→ 值得关注
      - `baseline_added` 例行补定语 → 正常行为，不改变 verdict
    """

    def __init__(self, text: str, findings: List[Dict[str, Any]],
                 repaired: bool, blocked: bool, baseline_added: bool = False):
        self.text = text
        self.findings = findings
        self.repaired = repaired
        self.blocked = blocked
        self.baseline_added = baseline_added

    @property
    def worst_level(self) -> str:
        if not self.findings:
            return "ok"
        return max(self.findings, key=lambda f: _LEVEL_ORDER.get(f["level"], 0))["level"]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "verdict": "blocked" if self.blocked else ("repaired" if self.repaired else "pass"),
            "worst_level": self.worst_level,
            "repaired": self.repaired,
            "blocked": self.blocked,
            "baseline_added": self.baseline_added,
            "findings": [{
                "rule": f["rule"], "redline": f.get("redline"),
                "level": f["level"], "reason": f["reason"],
                "matched": f.get("matched", ""),
            } for f in self.findings],
        }


def _strip_user_doses(text: str, findings: List[Dict[str, Any]]) -> str:
    """把高危剂量的具体数字抹去，换成指引语。

    不是简单删除——用户需要一个可读的句子，而不是一个空洞。
    """
    high = [f for f in findings if f["rule"] == "G-DOSE" and f["level"] == "high"]
    if not high:
        return text
    out = text
    for f in sorted(high, key=lambda x: x["span"][0], reverse=True):
        s, e = f["span"]
        out = out[:s] + "【具体剂量须由执业中医师面诊确定】" + out[e:]
    return out


def guard_output(text: str, layer: str = "L2",
                 enabled: bool = True,
                 action: str = "block") -> GuardResult:
    """对**完整**输出做后置校验。用于非流式路径与最终交付物。"""
    if not enabled or not text:
        return GuardResult(text, [], False, False)

    findings: List[Dict[str, Any]] = []
    findings += check_dose(text)
    findings += check_r8(text)
    findings += check_r3(text)
    findings += check_r5(text)
    findings += check_disclaimer(text, layer)
    findings += check_trace(text)

    if action != "block":
        return GuardResult(text, findings, False, False)

    out = text
    repaired = False          # 实质改写
    baseline_added = False    # 例行补定语

    # R2：抹去面向用户的剂量
    new = _strip_user_doses(out, findings)
    if new != out:
        out, repaired = new, True

    # R8 / R5：高危项追加明确澄清，并保留原文供人工复核
    notes = _counter_notes(findings)
    if notes:
        out = out + "\n\n" + "\n".join(notes)
        repaired = True

    # 合规定语：例行基线，不算「改写」
    for f in findings:
        if f["rule"] == "G-DISC" and f.get("auto_fix"):
            out = out.rstrip() + "\n\n---\n\n" + f["auto_fix"]
            baseline_added = True
            break

    blocked = any(f["level"] == "high" for f in findings)
    return GuardResult(out, findings, repaired, blocked, baseline_added)


def _counter_notes(findings: List[Dict[str, Any]]) -> List[str]:
    high = [f for f in findings if f["level"] == "high" and f["rule"] in ("G-R8", "G-R5")]
    notes = []
    if any(f["rule"] == "G-R8" for f in high):
        notes.append(
            "> ⚠️ 提示：以上内容如涉及对现代医学治疗的评价，属学术观点转述。"
            "**请勿据此停用任何正在进行的规范治疗**，用药调整请咨询主治医师。"
        )
    if any(f["rule"] == "G-R5" for f in high):
        notes.append(
            "> ⚠️ 说明：我是基于倪海厦先生公开资料构建的 AI 助手，**不是倪海厦本人**。"
        )
    return notes


# ── 流式分段守门 ────────────────────────────────────────────────────────────
#
# 为什么必须有这个：流式输出下，等模型说完再守门是**无效的**——
# 违规文本已经逐字流到用户屏幕上了。
# 因此按句切分，**先守门再放行**，把违规拦在它到达用户之前。

_SENTENCE_END = re.compile(r"[。！？!?；;\n]")


def guard_segment(segment: str, context_tail: str = "",
                  action: str = "block") -> tuple:
    """守门一个句子分段。返回 `(放行文本, findings)`。

    `context_tail` 是已放行文本的尾部，用于补足剂量检测所需的上下文窗口
    （否则「桂枝」在上一句、「9 克」在下一句时会漏检）。
    """
    if not segment:
        return segment, []

    combined = (context_tail or "") + segment
    offset = len(context_tail or "")

    findings: List[Dict[str, Any]] = []
    for f in check_dose(combined):
        f = dict(f)
        if f.get("span"):
            f["span"] = [f["span"][0] - offset, f["span"][1] - offset]
        findings.append(f)
    findings += check_r8(segment)
    findings += check_r5(segment)
    findings += check_r3(segment)

    if action != "block":
        return segment, findings

    out = segment

    # 高危剂量：就地抹去数字
    high_dose = [f for f in findings
                 if f["rule"] == "G-DOSE" and f["level"] == "high" and f.get("span")]
    for f in sorted(high_dose, key=lambda x: x["span"][0], reverse=True):
        s, e = f["span"]
        if s < 0 or e > len(out) or e <= s:
            continue
        out = out[:s] + "【具体剂量须由执业中医师面诊确定】" + out[e:]

    # R8 / R5：无法改写原句，就紧随其后插入澄清
    notes = _counter_notes(findings)
    if notes:
        out = out + "\n\n" + "\n".join(notes)

    return out, findings
