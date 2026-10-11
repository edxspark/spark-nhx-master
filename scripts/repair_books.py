#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批量整理 OCR 版书稿（人纪/天纪系列）的目录与标题。

背景
----
`book/1针灸/人纪系列针灸篇.html` 的目录曾被压成 10 个 `<p>` 段落，
一条条目录项用「……页码」串成流水。同一批 OCR 产物里还有另外 16 个 HTML，
问题分三类（详见 `book/目录整理报告.md`）：

1. **有目录块但格式乱**：金匮上/中/下册、天纪人间道（双栏交错）、
   地脉道（中文页码）、天机道（表格，页码已失联）
2. **完全没有目录**：神农本草经×3、黄帝内经×4、伤寒论×3、金匮注解
3. **标题层 OCR 乱象**：标题内换行、字间空格、重号断号、正文被误判成标题

本脚本做的事
------------
* `--survey` ：只读盘点，打印每个文件的目录状态与处理结果预览
* `--write`  ：就地整理（先备份到 `book/<dir>/.backup/`）

处理顺序：**先规范标题，再建目录** —— 目录项文字必须与锚点一致。

设计约束
--------
* **正文一个字不改**：只改标题文字（去换行/去杂空格）与目录区。
* **OCR 错字一律不改**：只进清单，不擅自替换。
* **序号只改「可证」的**：仅当 `… n、  n、  n+2、 …`（重号紧跟跳号）时，
  把第二个 n 改为 n+1 —— 前后邻号唯一确定该值；其余断号只进报告。
* 幂等：目录区已有 `<nav class="toc">` 的文件不会被二次处理。
"""

from __future__ import annotations

import argparse
import difflib
import html
import json
import re
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import repair_toc as rt  # noqa: E402  （复用：目录 CSS、标题归一化、锚点索引）

BOOK_ROOT = Path(__file__).resolve().parent.parent / "book"

TOC_HEAD_RE = re.compile(r"^目\s*[录錄録次]\s*[:：]?$")
PAGE_ASCII_RE = re.compile(r"(?<![0-9\-])(\d{1,3})(?![0-9\-])")
PAGE_CJK_RE = re.compile(r"[壹貳參肆伍陸柒捌玖拾]+")

CHAP_RE = re.compile(r"^第[一二三四五六七八九十百]+(章|篇)")
SEC_RE = re.compile(r"^第[一二三四五六七八九十百]+(节|節|式|講|讲)")
CJK_ITEM_RE = re.compile(r"^([一二三四五六七八九十百]+)、")
NUM_ITEM_RE = re.compile(r"^(\d{1,2})[、.]")
PAREN_ITEM_RE = re.compile(r"^（([一二三四五六七八九十]+)）")
# 条文序号：`一、` `二：` `一〇一：` `二六 O：` 等混写形式
NUM_PREFIX_RE = re.compile(r"^[一二三四五六七八九十百千〇零O0-9\s]{1,8}[、：:，,.]")
LEAD_NUM_RE = re.compile(r"^\s*([一二三四五六七八九十百千〇零O0-9]+"
                         r"(?:\s+[一二三四五六七八九十百千〇零O0-9]+)*)\s*([、：:，,.])")
# 顶层标志：以「篇/章/经」结尾，或「第N章/篇」，或前言类
TOP_RE = re.compile(r"(篇|章|上经|中经|下经|上經|中經|下經)$"
                    r"|篇第[一二三四五六七八九十百]+$"
                    r"|^第[一二三四五六七八九十百]+(章|篇)"
                    r"|(序言|前言|自序|導言|导言|結語|结语|後記|后记)$")
FORMULA_TAIL_RE = re.compile(r"(方|湯|汤|散|丸|煎|丹|饮|飲|膏|酒)$")

CN_DIGITS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
             "十": 10, "百": 100, "壹": 1, "貳": 2, "參": 3, "肆": 4, "伍": 5, "陸": 6,
             "柒": 7, "捌": 8, "玖": 9, "拾": 10}

GARBAGE_RE = re.compile(r"^[A-Za-z0-9\s.,:;\-_'\"()\[\]/\\]+$")


def cn2int(s: str) -> int:
    """中文数字 → int。支持 一/十/十一/二十/壹拾壹 等写法。"""
    s = s.strip()
    if not s:
        return 0
    for ten in ("十", "拾"):
        if ten in s:
            a, _, b = s.partition(ten)
            hi = CN_DIGITS.get(a, 1) if a else 1
            lo = CN_DIGITS.get(b, 0) if b else 0
            return hi * 10 + lo
    return CN_DIGITS.get(s, 0)


# --------------------------------------------------------------------------
# 标题规范化
# --------------------------------------------------------------------------

def normalize_heading_text(text: str) -> str:
    """把 OCR 标题理成可读的一行。"""
    t = re.sub(r"\s*\n\s*", " ", text)
    nonspace = len(re.sub(r"\s", "", t))
    gaps = len(re.findall(r"(?<=\S)\s+(?=\S)", t))
    has_ascii = bool(re.search(r"[0-9A-Za-z]", t))
    if nonspace and gaps and gaps <= 8 and gaps / nonspace >= 0.3 and not has_ascii:
        t = re.sub(r"\s+", "", t)          # 竖排留白：`倪 海 厦 《 人 間 道 》`
    else:
        # 含数字/字母的标题保留空格：`阿胶 5 炮附子 1 甘草 2.5` 的空格是分隔符
        t = re.sub(r"\s+", " ", t).strip()
    # 条文序号被 OCR 拆成多段（`一 O 一：`）时先并成一段（只并空白，不改字符）
    m = LEAD_NUM_RE.match(t)
    if m and re.search(r"\s", m.group(1)):
        t = re.sub(r"\s+", "", m.group(1)) + t[m.end(1):]
    t = re.sub(r"([、：:，,。])\s+", r"\1", t)
    t = re.sub(r"\s+([）」』》])", r"\1", t)
    t = re.sub(r"([）」』》])\s+(?=[\u4e00-\u9fff「『《（])", r"\1", t)
    t = re.sub(r"([（「『《])\s+", r"\1", t)
    return t.strip()


def is_structured(text: str) -> bool:
    """标题是否带结构序号（第N篇/一、/1、/（一）/二：…）。"""
    t = text.strip()
    return bool(CHAP_RE.match(t) or SEC_RE.match(t) or CJK_ITEM_RE.match(t)
                or NUM_ITEM_RE.match(t) or PAREN_ITEM_RE.match(t)
                or NUM_PREFIX_RE.match(t))


def is_garbage_heading(text: str) -> bool:
    """判断「其实是正文/表格碎片，被 OCR 误判成标题」。

    带序号的标题放宽长度与句末标点：伤寒论的标题就是条文原文
    （`一、太阳之为病，脉浮，头项强痛而恶寒。`），不能被误杀；
    而黄帝内经注解里 `帝曰，法阴阳奈何？` 这类对话没有序号，应被剔除。
    """
    t = text.strip()
    if len(t) < 2:
        return True
    if GARBAGE_RE.match(t) or re.fullmatch(r"[\d\s.、]+", t):
        return True
    if re.search(r"[：:]$", t):          # `倪海廈：`、`4、字词考：` 是引导句
        return True
    # 方剂组成行（`人参 甘草 白术 干姜各三两`、`矾石二两`）：量词紧跟数字，
    # 且数字前是药名汉字。这样「百合」「五分法」这类不会误伤。
    if re.search(r"(?<=[\u4e00-\u9fff])[一二三四五六七八九十百千半\d]{1,4}"
                 r"(两|兩|枚|升|斤|个|個|分|钱|錢|铢|銖)(?=\s|，|,|。|$)", t):
        return True
    if re.match(r"^第[一二三四五六七八九十百]+个", t):
        return True                       # `第二个是大便` = 正文列举，不是标题
    if is_structured(t):
        return len(t) > 200
    if len(t) > 36:
        return True
    if re.search(r"[。！？；]$", t):
        return True
    return False


def collect_headings(src: str) -> list[dict]:
    out = []
    for m in re.finditer(r"<(h[1-6])([^>]*)>(.*?)</\1>", src, re.S):
        raw = html.unescape(re.sub(r"<[^>]+>", "", m.group(3))).strip()
        idm = re.search(r'id="([^"]*)"', m.group(2))
        out.append({"tag": m.group(1), "attrs": m.group(2), "start": m.start(),
                    "end": m.end(), "id": idm.group(1) if idm else None,
                    "raw": raw, "text": raw})
    return out


def apply_heading_fixes(src: str, headings: list[dict]) -> tuple[str, list[tuple]]:
    """从后往前替换标题文本，避免位置漂移；只改文字，保留原属性与 id。"""
    changes = []
    for h in reversed(headings):
        if h["raw"] == h["text"]:
            continue
        new_html = (f'<{h["tag"]}{h["attrs"]}>'
                    f'{html.escape(h["text"], quote=False)}</{h["tag"]}>')
        src = src[:h["start"]] + new_html + src[h["end"]:]
        changes.append((h["raw"], h["text"]))
    return src, changes


# --------------------------------------------------------------------------
# 目录区：定位与解析
# --------------------------------------------------------------------------

def find_toc_heading(src: str):
    for m in re.finditer(r"<(h[1-6])([^>]*)>(.*?)</\1>", src, re.S):
        txt = re.sub(r"\s+", "", html.unescape(re.sub(r"<[^>]+>", "", m.group(3))).strip())
        if TOC_HEAD_RE.match(txt):
            return m
    return None


def toc_region_end(src: str, after: int) -> int:
    m = re.search(r"<h[1-6][^>]*>", src[after:])
    return after + m.start() if m else len(src)


def parse_ascii_toc(plain: str) -> list[dict]:
    """切「标题 + 页码」。页码 = 独立 1~3 位数字（不与数字/连字符相邻），
    这样 `09-1 ….. 97`、`10-2 ….. 142` 这类含数字的标题不会被误切。"""
    entries, prev = [], 0
    for m in PAGE_ASCII_RE.finditer(plain):
        title = _clean_title(plain[prev:m.start()])
        prev = m.end()
        if title:
            entries.append({"title": title, "page": int(m.group(1)),
                            "page_raw": m.group(1)})
    return entries


def parse_cjk_toc(plain: str) -> list[dict]:
    """地脉道页码用大写中文（壹貳參…），条目序号用普通数字（一、二、），两套字符集不重叠。"""
    entries, prev = [], 0
    for m in PAGE_CJK_RE.finditer(plain):
        title = _clean_title(plain[prev:m.start()])
        prev = m.end()
        if title:
            entries.append({"title": title, "page": cn2int(m.group(0)),
                            "page_raw": m.group(0)})
    return entries


def _clean_title(t: str) -> str:
    t = re.sub(r"^[\s…．.：:、,，]+", "", t)
    t = re.sub(r"[\s…．.：:]+$", "", t)
    t = re.sub(r"[（(]\s*$", "", t)          # OCR 留下的孤立左括号
    return t.strip()


def parse_table_toc(src: str, start: int, end: int) -> list[dict]:
    """天机道的目录是 <table>：首行是标题，其余全是点导线残渣；页码与标题已失联。

    只取标题 —— 页码不可配对就**不配对**，在报告里如实说明。
    """
    seg = src[start:end]
    table = re.search(r"<table>.*?</table>", seg, re.S)
    if not table:
        return []
    row = re.search(r"<tr>(.*?)</tr>", table.group(0), re.S)
    if not row:
        return []
    out = []
    for cell in re.findall(r"<td>(.*?)</td>", row.group(1), re.S):
        txt = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", cell))).strip()
        if txt and txt not in (":", ".", "-"):
            out.append({"title": txt, "page": None, "page_raw": None})
    return out


# --------------------------------------------------------------------------
# 层级
# --------------------------------------------------------------------------

def level_for_generated(title: str, seen_top: bool) -> int:
    t = title.strip()
    if TOP_RE.search(t) or CHAP_RE.match(t):
        return 1
    if SEC_RE.match(t):
        return 2 if seen_top else 1
    if CJK_ITEM_RE.match(t) or PAREN_ITEM_RE.match(t):
        return 2 if seen_top else 1
    if NUM_ITEM_RE.match(t):
        return 3 if seen_top else 2
    if NUM_PREFIX_RE.match(t):
        return 2 if seen_top else 1
    return 2 if seen_top else 1


def is_top_like(title: str) -> bool:
    t = title.strip()
    return bool(TOP_RE.search(t) or CHAP_RE.match(t) or SEC_RE.match(t))


def demote_lonely_first(entries: list[dict]) -> bool:
    """`一二九、蠡鱼` 这种「文件从半途开始」的情况：首条会被回退成 L1，
    导致整份目录只剩一个父节点。这里把它降回条目级，保持列表平铺。"""
    tops = [i for i, e in enumerate(entries) if e["level"] == 1]
    if len(tops) == 1 and tops[0] == 0 and not is_top_like(entries[0]["title"]):
        entries[0]["level"] = 2
        return True
    return False


def assign_levels(entries: list[dict], top_test=None) -> None:
    seen_top = False
    for e in entries:
        lv = top_test(e["title"]) if top_test else level_for_generated(e["title"], seen_top)
        e["level"] = lv
        if lv == 1:
            seen_top = True


def top_level_jinkui(title: str) -> int:
    """金匮目录：篇名（…第一 / …篇 / 前言）为顶层，方剂与专题为条目。"""
    t = title.strip()
    if re.search(r"第[一二三四五六七八九十]+$", t) or re.search(r"(篇|章)$", t):
        return 1
    if re.search(r"(前言|導言|导言|結語|结语|案例篇)$", t):
        return 1
    return 2


def top_level_numbered(title: str) -> int:
    """地脉道：无序号的结构名（序言/地理篇/风水篇/结语）为顶层，带序号的为条目。"""
    t = title.strip()
    if CJK_ITEM_RE.match(t) or PAREN_ITEM_RE.match(t):
        return 2
    return 1


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------

def render_row(e: dict) -> str:
    cls = f"toc-row toc-l{e['level']}" + ("" if e.get("anchor") else " toc-nolink")
    inner = f'<span class="toc-title">{html.escape(e["title"], quote=False)}</span>'
    if e.get("page_raw"):
        inner += ('<span class="toc-leader"></span>'
                  f'<span class="toc-page">{html.escape(str(e["page_raw"]))}</span>')
    if e.get("anchor"):
        return f'<a class="{cls}" href="#{html.escape(e["anchor"], quote=True)}">{inner}</a>'
    return f'<span class="{cls}" title="正文无对应标题（原稿此处无此标题）">{inner}</span>'


def build_tree(entries: list[dict]) -> dict:
    root = {"level": 0, "children": [], "e": None}
    stack = [root]
    for e in entries:
        node = {"level": e["level"], "children": [], "e": e}
        while stack[-1]["level"] >= e["level"]:
            stack.pop()
        stack[-1]["children"].append(node)
        stack.append(node)
    return root


def render_nodes(nodes: list[dict], depth: int = 1) -> str:
    if not nodes:
        return ""
    parts = ['<ol class="toc-items%s">' % ("-top" if depth == 1 else "")]
    for n in nodes:
        parts.append(f'<li>{render_row(n["e"])}')
        parts.append(render_nodes(n["children"], depth + 1))
        parts.append("</li>")
    parts.append("</ol>")
    return "".join(parts)


def render_nav(entries: list[dict]) -> str:
    return ('<nav class="toc" id="toc" aria-label="目录">\n'
            + render_nodes(build_tree(entries)["children"]) + "\n</nav>")


# --------------------------------------------------------------------------
# 锚点
# --------------------------------------------------------------------------

def link_entries(entries: list[dict], index: dict) -> None:
    """按标题给条目找正文锚点。已有锚点（生成型）原样保留。"""
    used = {}
    for e in entries:
        if e.get("anchor"):
            e["match"] = "标题本身"
            used[e["anchor"]] = used.get(e["anchor"], 0) + 1
    for e in entries:
        if e.get("anchor"):
            continue
        e["match"] = None
        key = rt.norm_for_match(e["title"])
        cands = index.get(key)
        if not cands:
            close = difflib.get_close_matches(key, list(index), n=1, cutoff=0.86)
            if close:
                cands = index[close[0]]
                e["match"] = "近似(%.2f)" % difflib.SequenceMatcher(None, key, close[0]).ratio()
        if not cands:
            continue
        pick = next((c for c in cands if used.get(c["id"], 0) == 0), cands[0])
        e["anchor"] = pick["id"]
        e["match"] = e["match"] or "标题一致"
        used[pick["id"]] = used.get(pick["id"], 0) + 1


# --------------------------------------------------------------------------
# 单文件处理
# --------------------------------------------------------------------------

def process(path: Path, write: bool) -> dict:
    src = path.read_text(encoding="utf-8")
    name = path.name
    res = {"file": str(path.relative_to(BOOK_ROOT)), "heading_fixes": [], "toc_mode": None,
           "entries": [], "dupes": [], "numbering": [], "notes": [], "written": False}

    # 1) 标题规范化
    hs = collect_headings(src)
    for h in hs:
        h["text"] = normalize_heading_text(h["raw"])
        if is_garbage_heading(h["text"]):
            h["text"] = h["raw"]              # 疑似正文被误判成标题的文字：不动
    src, res["heading_fixes"] = apply_heading_fixes(src, hs)

    # 2) 序号自检：只修「重号紧跟跳号」这一种可证错误
    hs = collect_headings(src)
    for k in range(1, len(hs) - 1):
        m1 = CJK_ITEM_RE.match(hs[k]["text"])
        m0 = CJK_ITEM_RE.match(hs[k - 1]["text"])
        m2 = CJK_ITEM_RE.match(hs[k + 1]["text"])
        if not (m0 and m1 and m2):
            continue
        v0, v1, v2 = cn2int(m0.group(1)), cn2int(m1.group(1)), cn2int(m2.group(1))
        if v0 == v1 and v2 == v1 + 2:
            new = re.sub(r"^[一二三四五六七八九十百]+、", rt.CN_NUM[v1 + 1] + "、",
                         hs[k]["text"])
            old_html = (f'<{hs[k]["tag"]}{hs[k]["attrs"]}>'
                        f'{html.escape(hs[k]["text"], quote=False)}</{hs[k]["tag"]}>')
            new_html = (f'<{hs[k]["tag"]}{hs[k]["attrs"]}>'
                        f'{html.escape(new, quote=False)}</{hs[k]["tag"]}>')
            src = src.replace(old_html, new_html, 1)
            res["numbering"].append({"旧": hs[k]["text"], "新": new,
                                     "依据": f"前后为「{m0.group(1)}、」「{m2.group(1)}、」，"
                                             f"该条只能是「{rt.CN_NUM[v1 + 1]}、」"})
            hs[k]["text"] = new

    # 3) 目录
    toc_h = find_toc_heading(src)
    if toc_h:
        reg_end = toc_region_end(src, toc_h.end())
        region = src[toc_h.start():reg_end]
        if '<nav class="toc" id="toc"' in region:
            res["toc_mode"] = "已整理（跳过）"
            return res
        toc_title = re.sub(r"\s+", "", html.unescape(
            re.sub(r"<[^>]+>", "", toc_h.group(3)))).strip().rstrip(":：")
        body_region = src[toc_h.end():reg_end]      # 标题本身不算条目
        if "<table>" in body_region:
            res["toc_mode"] = "重建目录（表格型：点导线成 <td>:</td>，页码失联）"
            entries = parse_table_toc(src, toc_h.end(), reg_end)
            tail = html.unescape(re.sub(r"<[^>]+>", " ", body_region))
            orphan = [x for x in re.findall(r"[一二三四五六七八九十百〇零]+", tail) if len(x) > 0]
            res["notes"].append("原表格里的页码无法与标题配对，未采用；原始残值："
                                + "、".join(orphan[-12:]))
            assign_levels(entries, top_test=lambda t: 1)
        else:
            plain = re.sub(r"\s+", " ",
                           html.unescape(re.sub(r"<[^>]+>", " ", body_region))).strip()
            if PAGE_CJK_RE.search(plain) and len(PAGE_ASCII_RE.findall(plain)) < 3:
                res["toc_mode"] = "重建目录（页码为中文数字）"
                entries = parse_cjk_toc(plain)
                assign_levels(entries, top_test=top_level_numbered)
            else:
                entries = parse_ascii_toc(plain)
                flat_top = bool(entries) and not any(
                    top_level_jinkui(e["title"]) == 1 for e in entries)
                two_col = _looks_two_column(entries)
                if two_col:
                    res["toc_mode"] = "重建目录（OCR 双栏交错 → 按页码归序）"
                    entries.sort(key=lambda e: e["page"])
                else:
                    res["toc_mode"] = "重建目录（页码为阿拉伯数字）"
                if flat_top:
                    res["toc_mode"] += " · 全篇平级"
                assign_levels(entries, top_test=(lambda t: 1) if flat_top
                              else top_level_jinkui)
        deduped = []
        for e in entries:
            if deduped and deduped[-1]["title"] == e["title"] and deduped[-1]["page"] == e["page"]:
                res["dupes"].append(f'{e["title"]}（P{e["page"]}）')
                continue
            deduped.append(e)
        entries = deduped
        # 先把正文锚点接上，**再**渲染 —— 否则渲染出来的行没有 href
        index = rt.build_body_index(src, (toc_h.start(), reg_end))
        link_entries(entries, index)
        new_region = (f'<h2 id="目录">{html.escape(toc_title or "目录")}</h2>\n'
                      + render_nav(entries) + "\n")
        src = src[:toc_h.start()] + new_region + src[reg_end:]
    else:
        res["toc_mode"] = "生成目录（原稿没有目录）"
        m2 = re.search(r"<h[2-6][^>]*>", src)
        insert_at = (m2.start() if m2 else
                     (src.find("</h1>") + 5 if "</h1>" in src else src.find("<body>") + 6))
        entries = []
        for h in collect_headings(src):
            if h["start"] < insert_at:
                continue
            if TOC_HEAD_RE.match(re.sub(r"\s+", "", h["text"])):
                continue
            if is_garbage_heading(h["text"]):
                res["notes"].append("跳过疑似非标题：" + re.sub(r"\s+", " ", h["text"])[:20])
                continue
            entries.append({"title": h["text"], "page": None, "page_raw": None,
                            "anchor": h["id"]})   # 生成型：条目就是标题本身，锚点直接可得
        if not entries:
            res["notes"].append("没有可用标题，未生成目录")
            return res
        assign_levels(entries)
        if demote_lonely_first(entries):
            res["notes"].append("首条无上级，已降级为条目（原稿从半途开始）")
        index = rt.build_body_index(src, (0, 0))
        link_entries(entries, index)
        src = (src[:insert_at] + '<h2 id="目录">目录</h2>\n' + render_nav(entries) + "\n"
               + src[insert_at:])

    res["entries"] = entries

    # 5) CSS
    if "/* ---- 目录（由 scripts/repair_toc.py 生成" not in src:
        src = src.replace("</style>", rt.TOC_CSS + "</style>", 1)

    if write:
        bak = path.parent / ".backup"
        bak.mkdir(exist_ok=True)
        b = bak / f"{path.name}.{time.strftime('%Y%m%d-%H%M%S')}.bak"
        if not b.exists():
            shutil.copy2(path, b)
        res["backup"] = str(b)
        path.write_text(src, encoding="utf-8")
        res["written"] = True
        write_artifacts(path, entries, res)
    return res


def _looks_two_column(entries: list[dict]) -> bool:
    pages = [e["page"] for e in entries]
    if len(pages) < 10:
        return False
    drops = sum(1 for a, b in zip(pages, pages[1:]) if b < a)
    return drops >= 3


def write_artifacts(path: Path, entries: list[dict], res: dict) -> None:
    stem = path.stem
    md = [f"# {stem} · 目录", "",
          "> 由 `scripts/repair_books.py` 自动整理生成（正文文字未改，只重整标题与目录结构）。",
          f"> 目录状态：{res['toc_mode']}", ""]
    for e in entries:
        pad = "  " * (e["level"] - 1)
        pg = f"　`P{e['page_raw']}`" if e.get("page_raw") else ""
        link = f"[{e['title']}](#{e['anchor']})" if e.get("anchor") else e["title"]
        md.append(f"{pad}- {link}{pg}")
    (path.parent / f"{stem}-目录.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    (path.parent / f"{stem}-目录.json").write_text(json.dumps({
        "source": path.name, "generator": "scripts/repair_books.py",
        "toc_mode": res["toc_mode"], "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "entry_count": len(entries),
        "entries": [{k: e.get(k) for k in ("level", "title", "page_raw", "anchor", "match")}
                    for e in entries],
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="批量整理人纪/天纪书稿的目录与标题")
    ap.add_argument("--write", action="store_true", help="就地写回（先备份到 .backup/）")
    ap.add_argument("--only", default=None, help="只处理路径匹配该正则的文件")
    args = ap.parse_args()

    files = sorted(p for p in BOOK_ROOT.glob("*/*.html") if p.is_file())
    if args.only:
        rx = re.compile(args.only)
        files = [p for p in files if rx.search(str(p))]
    if not files:
        raise SystemExit("没有匹配的文件")

    results = []
    print("%-44s %-40s %5s %5s %5s" % ("文件", "目录处理", "条目", "可跳转", "标题修"))
    print("-" * 108)
    for p in files:
        try:
            r = process(p, write=args.write)
        except Exception as exc:                       # noqa: BLE001
            print("❌ %-42s %s" % (p.relative_to(BOOK_ROOT), exc))
            results.append({"file": str(p.relative_to(BOOK_ROOT)), "error": str(exc)})
            continue
        results.append(r)
        n_link = sum(1 for e in r.get("entries", []) if e.get("anchor"))
        print("%-44s %-40s %5d %5d %5d%s"
              % (str(p.relative_to(BOOK_ROOT)), r.get("toc_mode") or "-",
                 len(r.get("entries", [])), n_link, len(r.get("heading_fixes", [])),
                 ("  ⚠ " + re.sub(r"\s+", " ", "; ".join(r["notes"]))[:150])
                 if r.get("notes") else ""))

    out = BOOK_ROOT / ".audit" / ("repair_applied.json" if args.write else "repair_survey.json")
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n明细：%s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
