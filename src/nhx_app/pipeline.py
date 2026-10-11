"""倪师 AI · 对话管线

一次对话的完整处理链：

    分诊 → 路由 → 检索 → 提示词组装 → 生成 → 守门

设计取舍（值得说明的两处）
--------------------------
1. **急症响应不走 LLM，用确定性模板。**
   理由：急症场景**确定性优先于流畅度**。模板保证：
     - 零延迟（不用等 API）
     - 零风险（模型不可能自己加一句「可能是心绞痛」）
     - 零依赖（LLM 挂了也照常工作）
   这是刻意的「用笨办法换可靠」。

2. **守门在生成之后，不在生成之前。**
   提示词里的红线是「请求模型遵守」；守门是「不管它遵不遵守都检查」。
   两层都需要——提示词降低概率，守门兜住漏网。

本模块是纯函数式的（除了 LLM 与 KB 注入），便于单测：可以不联网测路由、
分诊、守门与提示词组装。
"""

import os
import re
import sys
from typing import Any, Dict, Iterator, List, Optional

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from nhx_triage.triage import load_rules, triage  # noqa: E402

import guard as guard_mod  # noqa: E402
import prompts  # noqa: E402

# ── 层路由 ──────────────────────────────────────────────────────────────────

_L3_PATTERNS = [
    r"算命|看命|批命|命理|紫微|斗数|八字|四柱|排盘|星盘",
    r"风水|阳宅|阴宅|堪舆|罗盘|龙穴|户型|朝向|坐向",
    r"占卜|起.{0,2}卦|卜.{0,2}卦|算.{0,2}卦|六爻|梅花易数|摇卦|抽签",
    r"易经|周易|卦象|卦辞|爻辞",
    r"手相|面相|姓名学|塔罗",
]

_L2_SELF = [
    r"(我|自己|本人|孩子|宝宝|老婆|老公|爸|妈|父亲|母亲).{0,12}(疼|痛|难受|不舒服|不适|失眠|睡不着|头晕|头疼|乏力|没劲|咳嗽|拉肚子|腹泻|便秘|恶心|反胃|心慌|胸闷|耳鸣|口苦|上火|出汗|怕冷|怕热)",
    r"(最近|这几天|这段时间|一直|老是|总是|经常).{0,10}(失眠|睡不着|疼|痛|难受|不舒服|乏力|咳嗽|便秘|腹泻|头晕)",
    r"(帮我|给我|我想).{0,6}(看看|瞧瞧|分析|诊断|辨证|开方|调理)",
    r"(我这是|我是不是|我得的是).{0,10}(什么|啥)",
    r"调理|体质|气血|湿气|寒气|肾虚|脾虚|肝火|宫寒",
    r"(月|经|痛经|月经|白带|备孕).{0,8}(不调|量少|量多|推迟|提前|痛)",
]

_L1_KNOWLEDGE = [
    r"是什么|什么意思|怎么理解|如何理解|讲解|解释一下|介绍一下|说说",
    r"区别|异同|对比|比较|关系",
    r"组成|配伍|方义|君臣佐使|剂量|用法|煎服",
    r"功效|主治|性味|归经|禁忌",
    r"第[一二三四五六七八九十百零\d]+条",
    r"原文|条文|出自哪|哪本书",
    r"怎么学|如何学|学习路径|入门|推荐书",
]


def classify_layer(text: str) -> str:
    """判定问题属于 L1 / L2 / L3。

    用规则而非模型：确定性、零延迟、可单测、可解释。
    顺序即优先级：L3 术数 → L2 自身症状 → L1 知识 → 默认 L1。
    """
    t = text or ""
    for p in _L3_PATTERNS:
        if re.search(p, t):
            return "L3"
    for p in _L2_SELF:
        if re.search(p, t):
            return "L2"
    for p in _L1_KNOWLEDGE:
        if re.search(p, t):
            return "L1"
    return "L1"


# ── 急症响应（确定性模板）────────────────────────────────────────────────────

def build_emergency_response(triage_result) -> str:
    """急症响应。**不调用 LLM**——确定性优先。"""
    r = triage_result.response or {}
    categories = r.get("categories") or "、".join(triage_result.categories)
    action = r.get("action") or (triage_result.actions[0] if triage_result.actions else "")

    lines = [
        r.get("empathy", "我理解你现在的难受和担心。"),
        "",
        "**" + r.get("escalation", "你描述的情况需要立即就医。") + "**",
        "",
        "### 现在请立刻做这些事",
        "",
        action,
        "",
        r.get("boundary", "这个时间点上，我不会给出辨证分析或方剂建议——那会耽误你真正需要的救治。"),
        "",
        "---",
        "",
        "> 本条为**急症分诊**自动响应，未经过任何中医辨证分析，也未参考知识库内容。",
        "> 命中类别：%s" % categories,
    ]
    return "\n".join(lines)


# ── 检索 ────────────────────────────────────────────────────────────────────

def _kb_sources(kb, query: str, layer: str, limit: int) -> List[Dict[str, Any]]:
    if kb is None:
        return []
    results = kb.search(query, limit=limit)
    keep = {
        "L1": {"classical", "formula", "herb", "point", "article"},
        "L2": {"classical", "formula", "herb", "article"},
        "L3": {"article", "other"},
    }.get(layer)
    # meta（README / 采集报告等说明文件）不是知识源，永不进上下文
    if keep:
        results = [r for r in results if r["kind"] in keep]
    return results


# ── 主管线 ──────────────────────────────────────────────────────────────────

class Pipeline:
    def __init__(self, config, llm=None, kb=None, contract: Optional[str] = None):
        self.config = config
        self.llm = llm
        self.kb = kb
        self._rules = load_rules()
        self._contract = contract if contract is not None else prompts.load_contract(
            config.contract_path)

    @property
    def contract(self) -> str:
        return self._contract

    # ── 供 HTTP 层与测试使用 ────────────────────────────────────────────────

    def triage_only(self, text: str) -> Dict[str, Any]:
        return triage(text, self._rules).to_dict()

    def run(self, user_text: str, history: Optional[List[Dict[str, str]]] = None,
            session_id: str = "") -> Iterator[Dict[str, Any]]:
        """执行一次对话，产出事件流。

        事件类型：
          triage  —— 分诊结果（必为第一个事件）
          route   —— 层路由结果
          sources —— 知识库命中
          delta   —— 文本增量
          guard   —— 守门结果
          done    —— 结束（含最终文本）
          error   —— 失败
        """
        history = history or []

        # ① 分诊（永远第一步）
        t = triage(user_text, self._rules)
        yield {"event": "triage", "data": t.to_dict()}

        if t.hit:
            text = build_emergency_response(t)
            yield {"event": "delta", "data": {"text": text}}
            yield {"event": "guard", "data": {
                "verdict": "pass", "worst_level": "ok", "findings": [],
                "note": "急症响应为确定性模板，未经过 LLM。",
            }}
            yield {"event": "done", "data": {"text": text, "layer": "EMERGENCY",
                                             "emergency": True}}
            return

        # ② 路由
        layer = classify_layer(user_text)
        yield {"event": "route", "data": {"layer": layer}}

        # ③ 检索
        sources = _kb_sources(self.kb, user_text, layer,
                              self.config.kb_max_snippets) if self.config.kb_enabled else []
        yield {"event": "sources", "data": {"items": sources, "count": len(sources)}}

        # ④ 组装提示词
        kb_context = ""
        if sources and self.kb is not None:
            kb_context = self.kb.context_block(
                user_text, layer, self.config.kb_max_snippets)
        system = prompts.build_system_prompt(self.contract, layer, kb_context)
        messages = prompts.build_messages(
            system, history, user_text, self.config.max_history_messages)

        # ⑤ 生成
        if self.llm is None:
            yield {"event": "error", "data": {"message": "未配置 LLM 客户端"}}
            return

        parts: List[str] = []
        guard_findings: List[Dict[str, Any]] = []
        buffer = ""      # 尚未通过守门的文本
        released = ""    # 已放行文本

        try:
            for delta in self.llm.stream_chat(messages):
                parts.append(delta)
                buffer += delta

                # 按句放行：**先守门再发**，违规文本不会到达用户屏幕。
                # 若等全文生成完再守门，流式场景下其实已经晚了。
                if not self.config.guard_enabled:
                    yield {"event": "delta", "data": {"text": buffer}}
                    released += buffer
                    buffer = ""
                    continue

                while True:
                    m = guard_mod._SENTENCE_END.search(buffer)
                    if not m:
                        break
                    seg, buffer = buffer[:m.end()], buffer[m.end():]
                    safe, findings = guard_mod.guard_segment(
                        seg, context_tail=released[-40:],
                        action=self.config.guard_action)
                    guard_findings.extend(findings)
                    released += safe
                    yield {"event": "delta", "data": {"text": safe}}
        except Exception as exc:  # noqa: BLE001
            message = getattr(exc, "message", str(exc))
            yield {"event": "error", "data": {"message": message}}
            return

        # 尾部残余
        if buffer:
            if self.config.guard_enabled:
                safe, findings = guard_mod.guard_segment(
                    buffer, context_tail=released[-40:],
                    action=self.config.guard_action)
                guard_findings.extend(findings)
            else:
                safe = buffer
            released += safe
            yield {"event": "delta", "data": {"text": safe}}

        raw = "".join(parts)

        # ⑥ 文档级守门（合规定语、溯源提示、以及跨句违规兜底）
        result = guard_mod.guard_output(
            released, layer=layer,
            enabled=self.config.guard_enabled,
            action=self.config.guard_action,
        )
        # 若文档级守门又改动了文本（例如补定语），必须把增量补发出去
        if result.text != released:
            extra = result.text[len(released):] if result.text.startswith(released) \
                else "\n\n" + result.text
            yield {"event": "delta", "data": {"text": extra}}
            released = result.text

        gd = result.to_dict()

        # 文档级守门是在**已净化**的文本上跑的，因此它看不到流式段里拦下的违规。
        # 必须把两者合并，否则界面上会显示「pass」而实际刚拦过一次——观测性失真。
        all_findings = list(result.findings) + list(guard_findings)
        if guard_findings:
            gd["findings"] = ([{
                "rule": f["rule"], "redline": f.get("redline"),
                "level": f["level"], "reason": f["reason"],
                "matched": f.get("matched", ""),
            } for f in guard_findings] + gd["findings"])
        if any(f["level"] == "high" for f in guard_findings):
            gd["verdict"] = "blocked"
            gd["blocked"] = True
        elif guard_findings and gd["verdict"] == "pass":
            gd["verdict"] = "repaired"
        gd["worst_level"] = (
            max(all_findings, key=lambda f: guard_mod._LEVEL_ORDER.get(f["level"], 0))["level"]
            if all_findings else "ok")
        gd["stream_findings"] = len(guard_findings)
        gd["note"] = ("守门未发现违规" if not all_findings
                      else "发现 %d 项（流式段 %d 项），已改写"
                      % (len(all_findings), len(guard_findings)))
        yield {"event": "guard", "data": gd}

        yield {"event": "done", "data": {
            "text": released,
            "raw_text": raw if (result.repaired or guard_findings) else None,
            "layer": layer,
            "emergency": False,
        }}
