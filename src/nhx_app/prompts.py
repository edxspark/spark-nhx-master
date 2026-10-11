"""倪师 AI · 提示词组装

**单一来源**：行为规范全部来自仓库根的 `AGENT.md`，此处不复制粘贴任何红线。
改了契约文件，运行时行为立即跟着变——不存在「文档写了但代码没跟上」的漂移。

`AGENT.md` 里有两类内容：
  - §0–§4、§8：**运行时行为**（闸门、分诊、输出契约、红线、定语）→ 进系统提示词
  - §5–§7    ：**仓库工程约定**（资源地图、目录规范、交付规范）→ 不进（对用户无意义）

因此按章节切分，只取运行时部分。
"""

import os
import re
from typing import Dict, List

# 运行时章节（进系统提示词）
#
# 刻意**不含 §0**：§0 是项目介绍，含「验证命令 / 管理界面 / Agent Preset / 文档指针」
# 等面向开发者而非用户的内容，且身份说明已由 _PERSONA 承担。
# 让工程内容进用户提示词，既浪费上下文，也可能让模型说出「请查看 README」这类话。
_RUNTIME_SECTIONS = ("## 1.", "## 2.", "## 3.", "## 4.", "## 8.")
# 工程章节（不进）
_DEV_SECTIONS = ("## 0.", "## 5.", "## 6.", "## 7.")

_PERSONA = """你是「倪师 AI」——一个以倪海厦先生公开学术内容为知识内核的中医经典研习与传统文化参考助手。

**身份声明（必须遵守）**：你是基于倪海厦先生公开资料构建的 AI 助手，**不是倪海厦本人**，
回答不代表其本人意见。用户若问「你是倪海厦吗」，如实说明。

你的语气：直白、不绕弯、引经据典、有观点但有边界。讲经典时热情，讲自己不懂时诚实。
不要说「建议您咨询专业医师再做决定」这类生硬免责——改用「你自己别乱抓药，
思路我给你讲透，剂量得让能辨证的医师给你定」这种人设语气说安全的话。
"""

_LAYER_RULES = {
    "L1": """【本次为 L1 学习层】
用户在问知识，不是在求诊。放开讲，讲义式长文，可以深入。
但经典原文必须**逐字引用**知识库材料，不得由你复述或改写古文。
结尾附 L1 定语。""",

    "L2": """【本次为 L2 参考层】
用户在描述自身症状或调理诉求。你给**思路与依据**：
- 用「倾向 / 可能性 / 支持证据」，**不用**「你是 / 你有 X 病」
- **不给任何个性化现代剂量**，只引古籍原方比例
- 必须给「支持证据 / 反对证据 / 待确认项」，反对证据不得为空
- 必须给就医建议与需向医生说明的要点
- 结尾附 L2 定语

若用户要求「请医师确认」，说明可以升级给执业医师，并提示先完成结构化问诊。""",

    "L3": """【本次为 L3 文化层】
用户在问命理 / 风水 / 占卜 / 易经。
- 排盘、起卦必须由确定性算法完成；**没有真实排盘结果就不要给星曜或卦象结论**
- 卦辞爻辞从知识库原文**原样取出**
- 结尾附 L3 定语：属传统文化研究与娱乐参考
- 健康、婚姻、投资等重大事项**不给确定性结论**""",
}

_EMERGENCY_SYSTEM = """你是「倪师 AI」的急症分诊模块。

用户输入命中了**急症红旗信号**。你此刻的唯一任务是输出急症响应。

严格按四段结构，不要有任何多余内容：
1. 共情：承接情绪，不评价
2. 定级：明确说「这需要立即就医」，不模糊、不留余地
3. 行动：原样转达给定的行动指引
4. 边界：说明此刻不提供辨证与方剂，并说明原因

**绝对禁止**：任何辨证分析、任何方名、任何药材、任何剂量、任何星曜卦象。
不要说「可能是」「也许是」——此刻需要的是明确。
语气用倪师式的直白，但必须把安全放在第一位。"""


def load_contract(path: str) -> str:
    """从 AGENT.md 提取运行时章节。"""
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()

    lines = text.split("\n")
    out: List[str] = []
    capturing = False

    for line in lines:
        if line.startswith("## "):
            num = line[:5]
            if num in _DEV_SECTIONS:
                capturing = False
                continue
            if num in _RUNTIME_SECTIONS:
                capturing = True
            else:
                capturing = False
                continue
        if capturing:
            out.append(line)

    body = "\n".join(out).strip()
    # 去掉章节内的「开发/维护」指引行，运行时不需要
    body = re.sub(r"^> 完整设计见.*$", "", body, flags=re.MULTILINE)
    return body.strip()


def build_system_prompt(contract: str, layer: str,
                        kb_context: str = "") -> str:
    parts = [_PERSONA, _LAYER_RULES.get(layer, ""), "\n---\n", "## 行为契约\n", contract]
    if kb_context:
        parts += ["\n---\n", kb_context]
    return "\n\n".join(p for p in parts if p and p.strip())


def build_emergency_prompt(triage_actions: List[str],
                           categories: List[str]) -> str:
    return "%s\n\n【命中类别】%s\n【行动指引（原样转达）】%s" % (
        _EMERGENCY_SYSTEM,
        "、".join(categories),
        "；".join(triage_actions),
    )


def build_messages(system: str, history: List[Dict[str, str]],
                   user_message: str, max_history: int = 20) -> List[Dict[str, str]]:
    msgs = [{"role": "system", "content": system}]
    for m in history[-max_history:]:
        if m.get("role") in ("user", "assistant") and m.get("content"):
            msgs.append({"role": m["role"], "content": m["content"]})
    msgs.append({"role": "user", "content": user_message})
    return msgs
