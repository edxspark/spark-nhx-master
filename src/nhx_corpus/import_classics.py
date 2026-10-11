"""倪师 AI · 经典原文导入器

把已下载的原始古籍数据转换为本库的结构化格式。

为什么是「导入器」而不是一次性脚本：原始数据可能更新、可能换源，
转换规则（标记解析、条号归属、简体声明）需要**可复现、可复核**。
把规则写进代码并配测试，比手工处理一次可靠。

用法：
    python3 src/nhx_corpus/import_classics.py --raw .raw-sources --out knowledge

关于「简体」的声明（重要）
--------------------------
本库以**简体**落盘，且**不做简繁转换**。

原因：古籍传本是繁体，简→繁自动转换会踩「一简对多繁」——
「干姜/乾薑」「里急/裏急」「发/髮」等，转错即篡改原文，直接违反 R1。
既然现有可交叉验证的来源都是简体，就**如实标注 `script: simplified`**，
而不是假装成繁体。

关于「异文」的处理
------------------
两个独立来源（赵开美本 / 另一 GitHub 源）比对结果分三档，全部保留：

| 档 | 含义 | 落盘字段 |
|---|---|---|
| exact | 逐字一致 | `verification.level = "verified"` |
| near | 相似度 ≥0.9，属异文/校注差异 | `level = "minor_variant"` + `similarity` |
| far  | 相似度 <0.9，差异较大 | `level = "divergent"` + `similarity` |

**不静默择一**：把差异记录下来标注清楚，比假装「这就是标准本」诚实得多，
也方便日后人工裁定。
"""

import argparse
import json
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

# 宋本 398 条所在的 section 区间（辨太阳上 ~ 辨阴阳易差后劳复）
_SHANGHAN_CANON_SECTIONS = (8, 17)

# 内嵌标记：$f{方名} $a{注} $u{药} $h{...}
_MARKER_RE = re.compile(r"\$([afuh])\{([^}]*)\}")
# 未闭合的标记残余（源数据里有 $h{$f{...}} 这类嵌套，内层先被吃掉会留下 $h{）
_DANGLING_RE = re.compile(r"\$[afuh]\{")
_NUM_RE = re.compile(r"^\s*(\d+)\s*[、.．]")

# 句读（标点）不计入「逐字」比对——不同版本句读不同，字符一致才是关键
PUNCT_RE = re.compile(r"[。，、；：！？（）〈〉《》「」『』,.!?;:()\[\]“”‘’\s]")


def normalize_for_compare(text: str) -> str:
    """去标点归一化，用于跨版本逐**字**比对。

    之所以需要它：不同版本的**句读**（断句标点）不同，但字是一样的。
    例如第 3 条，来源作「必恶寒体痛呕逆」，通行标点本作
    「必恶寒。体痛。呕逆。」——字完全相同，只是断句不同。
    比对时若把标点也算进去，会把排版差异误判成文本差异。
    """
    return PUNCT_RE.sub("", text or "")


_MARKER_KINDS = "afuh"


def _parse_markers(text: str):
    """解析 `$x{...}` 标记，支持**嵌套**。返回 `(collected, clean_text)`。

    为什么手写解析器而不用正则：
    源数据里有 nested 形式（外层 h 包内层 f）这种嵌套。
    正则匹配在从左到右扫描时，外层会把内层整个吞进 body
    （`[^}]*` 不能跨 `}`，于是恰好停在**内层**的 `}` 上），
    结果内层方名永远取不到。这类嵌套必须用能跟踪花括号深度的解析器。
    """
    collected = []
    clean = []
    i, n = 0, len(text or "")

    while i < n:
        ch = text[i]
        if ch == "$" and i + 2 < n and text[i + 1] in _MARKER_KINDS and text[i + 2] == "{":
            kind = text[i + 1]
            depth = 1
            j = i + 3
            while j < n:
                if (text[j] == "$" and j + 2 < n
                        and text[j + 1] in _MARKER_KINDS and text[j + 2] == "{"):
                    depth += 1
                    j += 3
                    continue
                if text[j] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1

            if depth == 0:
                body = text[i + 3:j]
                sub_collected, sub_clean = _parse_markers(body)
                # 先收集内层，再用**清理后的** body 作为本层内容——
                # 若用原始 body，内层标记原文会被当成方名混进来。
                collected.extend(sub_collected)
                collected.append((kind, sub_clean.strip()))
                clean.append(sub_clean)
                i = j + 1
            else:
                i += 3          # 未闭合：丢掉标记头，正文保留
            continue

        clean.append(ch)
        i += 1

    return collected, "".join(clean)


def strip_markers(text: str) -> str:
    """去掉内嵌标记，得到可读正文（支持嵌套与未闭合残余）。"""
    return _parse_markers(text)[1]


def extract_markers(text: str) -> Dict[str, List[str]]:
    """按类型抽取内嵌标记内容（支持嵌套）。"""
    out: Dict[str, List[str]] = {"formulas": [], "herbs": [], "annotations": []}
    key = {"f": "formulas", "u": "herbs", "a": "annotations", "h": "formulas"}

    for kind, body in _parse_markers(text)[0]:
        if body:
            out[key.get(kind, "annotations")].append(body)

    for k in out:
        seen, uniq = set(), []
        for v in out[k]:
            if v not in seen:
                seen.add(v)
                uniq.append(v)
        out[k] = uniq
    return out


def clean_text(raw: str) -> str:
    """清洗为可读正文：去标记、去条号前缀、统一空白、去零宽字符。"""
    t = strip_markers(raw or "")
    t = _NUM_RE.sub("", t, count=1)              # 去掉行首「12、」
    t = t.replace("\r", "\n").replace("\u3000", " ")
    t = t.replace("\ufeff", "").replace("\u200b", "")
    t = re.sub(r"[ \t]+", "", t)                  # 中文正文不留空格
    t = re.sub(r"\n{2,}", "\n", t)
    return t.strip()


def load_compare(raw_dir: str) -> Dict[str, Any]:
    """读取交叉比对结果；缺失则返回空结构。"""
    path = os.path.join(raw_dir, "cmp_ab.json")
    if not os.path.exists(path):
        return {"exact": 0, "near": [], "far": []}
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def classify_verification(no: int, cmp_data: Dict[str, Any]) -> Tuple[str, Optional[float]]:
    """按交叉比对结果给出该条的验证等级。"""
    near = dict(cmp_data.get("near") or [])
    far = dict(cmp_data.get("far") or [])
    if no in far:
        return "divergent", float(far[no])
    if no in near:
        return "minor_variant", float(near[no])
    return "verified", None


# ── 伤寒论 ──────────────────────────────────────────────────────────────────

def build_shanghan(raw_dir: str) -> Dict[str, Any]:
    src = os.path.join(raw_dir, "shangHan_data.json")
    with open(src, "r", encoding="utf-8") as fh:
        data = json.load(fh)

    cmp_data = load_compare(raw_dir)

    articles: List[Dict[str, Any]] = []
    section_titles: Dict[int, str] = {}

    for sec in data:
        sec_no = sec.get("section")
        if not isinstance(sec_no, int) or not (
                _SHANGHAN_CANON_SECTIONS[0] <= sec_no <= _SHANGHAN_CANON_SECTIONS[1]):
            continue
        header = (sec.get("header") or "").replace("\r", " ").strip()
        section_titles[sec_no] = header

        for item in sec.get("data", []):
            raw = item.get("text", "") or ""
            m = _NUM_RE.match(raw)
            if not m:
                continue
            no = int(m.group(1))
            marks = extract_markers(raw)
            level, sim = classify_verification(no, cmp_data)

            art: Dict[str, Any] = {
                "no": no,
                "text": clean_text(raw),
                "section": header,
                "verification": level,
            }
            if sim is not None:
                art["similarity"] = sim
            if marks["formulas"]:
                art["formulas"] = marks["formulas"]
            if marks["herbs"]:
                art["herbs"] = marks["herbs"]
            if marks["annotations"]:
                art["annotations"] = marks["annotations"]
            art["raw_text"] = raw.strip()
            articles.append(art)

    articles.sort(key=lambda a: a["no"])
    nos = [a["no"] for a in articles]
    missing = [n for n in range(1, 399) if n not in set(nos)]
    dups = sorted({n for n in nos if nos.count(n) > 1})

    return {
        "book": "伤寒论",
        "edition": "赵开美翻刻宋板（明万历二十七年据北宋元祐三年小字本）",
        "structure": "宋本 398 条",
        "script": "simplified",
        "script_note": (
            "本库以简体落盘，未做简繁转换。古籍传本为繁体，"
            "自动简→繁会踩「一简对多繁」（干姜/乾薑、里急/裏急），属篡改原文，"
            "故保持来源原样并如实标注。"
        ),
        "license_status": "公有领域",
        "source": {
            "primary": {
                "name": "hh-in-zhuzhou/ShangHanLun（赵开美翻刻宋板）",
                "url": "https://raw.githubusercontent.com/hh-in-zhuzhou/ShangHanLun/"
                       "master/app/src/main/assets/shangHan_data.json",
                "fetched_at": time.strftime("%Y-%m-%d"),
            },
            "cross_check": {
                "name": "SH_text.json（独立第二源）",
                "url": None,
                "note": "用于逐字交叉比对；原始文件见 .raw-sources/SH_text.json",
            },
            "tertiary": {
                "name": "Kanripo KR3ef001（宋本，繁体）",
                "url": "https://raw.githubusercontent.com/kanripo/KR3ef001/master/",
                "note": "繁体独立源，约 59 条可与方证本对齐；用于字形核对",
            },
        },
        "verification": {
            "cross_checked": True,
            "sources_compared": 2,
            "exact_match": cmp_data.get("exact", 0),
            "minor_variant": len(cmp_data.get("near") or []),
            "divergent": len(cmp_data.get("far") or []),
            "note": (
                "两源逐字一致 %d 条；其余为异文/校注差异（如 讝/谵、硝/消、畜/蓄 等），"
                "已按条标注 verification 等级，未静默择一。"
                % cmp_data.get("exact", 0)
            ),
        },
        "textual_notes": [
            {
                "issue": "句读（断句标点）随版本而异",
                "example": "第 3 条：来源作「必恶寒体痛呕逆」，通行标点本作「必恶寒。体痛。呕逆。」",
                "impact": "字完全相同，仅断句不同。跨版本比对须去标点后进行，"
                          "否则会把排版差异误判为文本差异。",
                "handling": "导入时保留来源句读；比对用 normalize_for_compare() 去标点。",
            },
            {
                "issue": "简繁字形",
                "example": "干姜/乾薑、里急/裏急、发/髮",
                "impact": "自动简→繁会踩「一简对多繁」，属篡改原文。",
                "handling": "不做转换，如实标注 script=simplified。",
            },
        ],
        "coverage": {"total": 398, "collected": len(articles),
                     "missing": missing, "duplicates": dups},
        "articles": articles,
    }


# ── 金匮要略 ────────────────────────────────────────────────────────────────

def build_jingui(raw_dir: str) -> Dict[str, Any]:
    src = os.path.join(raw_dir, "jinKui_data.json")
    with open(src, "r", encoding="utf-8") as fh:
        data = json.load(fh)

    chapters: List[Dict[str, Any]] = []
    total = 0

    for sec in data:
        header = (sec.get("header") or "").replace("\r", " ").strip()
        # header 形如「金匮要略1  脏腑经络先后病」
        m = re.match(r"^金匮要略\s*(\d+)\s*(.*)$", header)
        no = int(m.group(1)) if m else sec.get("section", 0) + 1
        title = (m.group(2).strip() if m else header) or "第 %d 篇" % no

        arts = []
        for item in sec.get("data", []):
            raw = item.get("text", "") or ""
            n = _NUM_RE.match(raw)
            marks = extract_markers(raw)
            art = {"text": clean_text(raw), "raw_text": raw.strip()}
            if n:
                art["no"] = int(n.group(1))
            if marks["formulas"]:
                art["formulas"] = marks["formulas"]
            if marks["herbs"]:
                art["herbs"] = marks["herbs"]
            if marks["annotations"]:
                art["annotations"] = marks["annotations"]
            arts.append(art)
        total += len(arts)

        chapters.append({
            "no": no,
            "title": title,
            "article_count": len(arts),
            "articles": arts,
            "text": "\n".join(a["text"] for a in arts),
        })

    chapters.sort(key=lambda c: c["no"])

    return {
        "book": "金匮要略",
        "edition": "邓珍本系统（附俞桥本、赵开美本校注）",
        "structure": "%d 篇 %d 条" % (len(chapters), total),
        "script": "simplified",
        "script_note": "同《伤寒论》：以简体落盘，不做简繁转换（见伤寒论文件说明）。",
        "license_status": "公有领域",
        "source": {
            "primary": {
                "name": "hh-in-zhuzhou/ShangHanLun（jinKui_data.json）",
                "url": "https://raw.githubusercontent.com/hh-in-zhuzhou/ShangHanLun/"
                       "master/app/src/main/assets/jinKui_data.json",
                "fetched_at": time.strftime("%Y-%m-%d"),
            },
            "cross_check": {
                "name": "the-quizzman/jinguiyaolue（分篇 Markdown）",
                "url": "https://raw.githubusercontent.com/the-quizzman/jinguiyaolue/main/chapters/",
                "note": "第二源，用于分篇比对",
            },
            "tertiary": {
                "name": "Kanripo KR3e0007（四部丛刊本，繁体）",
                "url": "https://raw.githubusercontent.com/kanripo/KR3e0007/master/",
                "note": "繁体独立源，用于字形核对",
            },
        },
        "verification": {
            "cross_checked": False,
            "note": "已下载第二源与繁体源，但逐字交叉比对尚未完成——标注为未完成，不作虚假声明。",
        },
        "coverage": {"chapters": len(chapters), "articles": total},
        "chapters": chapters,
    }


# ── 主流程 ──────────────────────────────────────────────────────────────────

def write_json(path: str, data: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def main() -> int:
    ap = argparse.ArgumentParser(description="导入经典原文")
    ap.add_argument("--raw", default=".raw-sources", help="原始数据目录")
    ap.add_argument("--out", default="knowledge", help="知识库根目录")
    args = ap.parse_args()

    if not os.path.isdir(args.raw):
        print("❌ 原始数据目录不存在：%s" % args.raw)
        return 1

    print("倪师 AI · 经典原文导入")
    print("  原始数据：%s" % args.raw)
    print()

    # 伤寒论
    sh = build_shanghan(args.raw)
    out_sh = os.path.join(args.out, "01-人纪-中医经典", "04-伤寒论", "原文", "伤寒论-条文.json")
    write_json(out_sh, sh)
    print("✅ 伤寒论：%d/%d 条 → %s" % (
        sh["coverage"]["collected"], sh["coverage"]["total"], out_sh))
    print("   交叉验证：逐字一致 %d，异文 %d，差异较大 %d，缺失 %d" % (
        sh["verification"]["exact_match"], sh["verification"]["minor_variant"],
        sh["verification"]["divergent"], len(sh["coverage"]["missing"])))

    # 金匮要略
    jg = build_jingui(args.raw)
    out_jg = os.path.join(args.out, "01-人纪-中医经典", "05-金匮要略", "原文", "金匮要略-条文.json")
    write_json(out_jg, jg)
    print("✅ 金匮要略：%d 篇 %d 条 → %s" % (
        jg["coverage"]["chapters"], jg["coverage"]["articles"], out_jg))

    print()
    print("提示：两个文件都标注 script=simplified（未做简繁转换）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
