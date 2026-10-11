#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""整理 OCR 版 HTML 书稿的「目录」区。

问题背景
--------
`book/1针灸/24本人纪系列针灸篇B5.html` 是 PDF 经 OCR + pandoc 转换的产物。
正文标题（h1/h2）本身是完整的，但**目录区**被压成了 10 个 `<p>`
段落，一条条目录项用「……页码」串在一起，表现为：

* 一条目录项不占一行，多条挤在同一行里；
* 引点不统一（`….` / `……` / 直接跟页码）；
* 时间戳与页码顺序错乱（`第三章 …….18 (1-01:14:05)….18`）；
* 序号标点不统一（`2. 极泉穴` 与 `2、极泉穴` 混用）；
* 序号漏字（`一、足太阳膀胱经` 应为 `十、`，`四、足少阳胆经` 应为 `十四、`）；
* 序号整条丢失（`肺俞穴（俞募治疗法）` 缺 `7、`）。

本脚本做的事
------------
1. 解析目录区 → 结构化条目（层级 / 序号 / 标题 / 时间戳 / 页码）；
2. 用「序号连续性检查」自动找出序号异常，套用 `TEXT_FIXES` 修正；
3. 与正文 h1/h2 的 `id` 对齐，生成可点击锚点；
4. 渲染成三级嵌套的 `<nav class="toc">`，写回 HTML（先备份）；
5. 同时导出 `目录.md` / `目录.json` / `目录整理报告.md`。

设计约束
--------
* **不改动正文**：只替换 `<h2 id="目录">` 到下一个标题之间的区间。
* **不臆造内容**：只做格式与序号的修正，正文/标题文字保持原样；
  「疑似错字但全稿一致」的项（如 `頤会`）不做替换，只在报告里列出。
* **可重复执行**：`--check` 只读；重跑不会二次破坏（幂等）。
"""

from __future__ import annotations

import argparse
import difflib
import html
import json
from html.parser import HTMLParser
import re
import shutil
import sys
import time
from pathlib import Path

# --------------------------------------------------------------------------
# 常量：正则与词表
# --------------------------------------------------------------------------

TIME = r"[（(]\d{1,2}-\d{1,2}:\d{2}:\d{2}[）)]"
DOTS = r"[…\.]{2,}"

# 目录项的「结束标记」：`（时间戳）……页码` 或 `（时间戳）页码`
END_RE = re.compile(
    rf"(?:(?P<t1>{TIME})\s*)?(?:{DOTS})\s*(?P<p1>\d{{1,3}})"
    rf"|(?P<t2>{TIME})\s*(?P<p2>\d{{1,3}})"
)
TIME_ONLY_RE = re.compile(rf"^{TIME}$")

# 序号：第X章 / 一、 / 1、
LABEL_RE = re.compile(r"^(第[一二三四五六七八九十]+章|[一二三四五六七八九十]{1,3}[、.]|\d{1,2}\s*[、.．])\s*")
CHAP_RE = re.compile(r"^第[一二三四五六七八九十]+章$")
CN_SEC_RE = re.compile(r"^[一二三四五六七八九十]{1,3}、$")
NUM_ITEM_RE = re.compile(r"^\d{1,2}、$")

CN_DIGITS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
             "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
CN_NUM = ["零", "一", "二", "三", "四", "五", "六", "七", "八", "九", "十",
          "十一", "十二", "十三", "十四", "十五", "十六", "十七", "十八",
          "十九", "二十", "二十一", "二十二", "二十三", "二十四", "二十五"]

# --------------------------------------------------------------------------
# 修正表：只包含「结构 / 序号」层面可判定的错误，每条都写清依据
# --------------------------------------------------------------------------

TEXT_FIXES: list[tuple[str, str, str]] = [
    # (正则, 替换, 依据)
    (r"^一、足太阳膀胱经", "十、足太阳膀胱经",
     "第四章章内序号断档：前节为「九、手太阳小肠经」，正文标题亦为「十、足太阳膀胱经」"),
    (r"^四、足少阳胆经", "十四、足少阳胆经",
     "第四章章内序号断档：前节为「十三、手少阳三焦经」，其后为「十五、足厥阴肝经」"),
    (r"^2\.\s*极泉穴", "2、极泉穴", "序号标点与全稿统一为「、」"),
    (r"^肺俞穴（俞募治疗法）", "7、肺俞穴（俞募治疗法）",
     "序号整条被 OCR 吃掉：前后为「6、风门穴」「8、厥阴俞与心俞穴」"),
    (r"（足三里十临泣穴）", "（足三里加临泣穴）", "OCR 把「加」识成「十」"),
]

# 仅用于「对齐正文锚点」的等价字表，不改写原文
MATCH_VARIANTS = str.maketrans({
    "腧": "俞", "晴": "睛", "宴": "窦", "育": "肓", "痾": "痫",
    "癲": "癫", "鵲": "鹊", "頂": "顶", "會": "会", "脈": "脉",
    "經": "经", "藥": "药", "學": "学", "陰": "阴", "陽": "阳",
    "腎": "肾", "膽": "胆", "髎": "髎", "夾": "夹", "華": "华",
})
DASHES = "—–－‒―ー·、,， "

# 全稿一致、但疑为 OCR 错字：列出不替换
SUSPECT_NOTES = [
    ("頤会", "囟会", "督脉穴位应为「囟会」；但正文标题与正文均写作「頤会」，属全稿一致，未擅自替换"),
    ("华陀夹脊", "华佗夹脊", "人名通行写法为「华佗」；正文三处均作「华陀」，属全稿一致，未擅自替换"),
    ("瘈脉", "瘛脉（另一写法）", "GB/T 12346 作「瘈脉」，部分教材作「瘛脉」，两说并存，保留原样"),
]


class AlreadyRepaired(RuntimeError):
    """目录区已是整理后的结构。"""


# --------------------------------------------------------------------------
# 解析
# --------------------------------------------------------------------------

def find_toc_region(src: str) -> tuple[int, int]:
    """返回目录区（`<h2 id="目录">` … 下一个 h1/h2）在 HTML 中的起止下标。"""
    head = src.find('<h2 id="目录">')
    if head < 0:
        raise SystemExit("未找到 <h2 id=\"目录\">，无法定位目录区")
    m = re.search(r"<h[12][ >]", src[head + 20:])
    if not m:
        raise SystemExit("目录区之后未找到下一个标题")
    return head, head + 20 + m.start()


def toc_plain_text(region: str) -> str:
    """把目录区的 HTML 压成一行纯文本（保留原有顺序）。"""
    parts = []
    for p in re.findall(r"<p>(.*?)</p>", region, re.S):
        t = html.unescape(re.sub(r"<[^>]+>", "", p))
        parts.append(re.sub(r"\s+", " ", t).strip())
    return " ".join(parts)


def split_entries(full: str) -> tuple[list[dict], str]:
    """按「……页码」切分目录项。返回 (entries, trailing)。"""
    entries: list[dict] = []
    prev = 0
    for m in END_RE.finditer(full):
        raw = full[prev:m.start()].strip()
        entries.append({
            "raw": raw,
            "time": norm_time(m.group("t1") or m.group("t2")),
            "page": int(m.group("p1") or m.group("p2")),
        })
        prev = m.end()
    trailing = full[prev:].strip()
    return entries, trailing


def norm_time(t: str | None) -> str | None:
    if not t:
        return None
    return t.replace("(", "（").replace(")", "）")


def merge_stray_time(entries: list[dict], log: list[str]) -> list[dict]:
    """合并被误切开的条目：整条内容为空或只有一个时间戳时，并回上一条。

    典型样本（原稿第 16~17 行）::

        第三章 十二经纳天干地支与十二正经井荣俞原经合….18 (1-01:14:05)….18

    时间戳被 OCR 排到了页码之后，切分后会出现一个「只有时间戳」或「空」
    的碎片，这里把它并回前一条，时间戳归位。
    """
    out: list[dict] = []
    for e in entries:
        if out and (e["raw"] == "" or TIME_ONLY_RE.match(e["raw"])):
            prev = out[-1]
            log.append(f"「{prev['raw']}」时间戳 {e['raw'] or e['time'] or '（无）'} 归位；"
                       f"页码 {prev['page']} → {e['page']}")
            prev["time"] = norm_time(e["raw"]) or e["time"] or prev["time"]
            prev["page"] = e["page"]
            prev["merged"] = True
            continue
        out.append(e)
    return out


def apply_text_fixes(text: str, applied: list[tuple[str, str, str]], fixes=None) -> str:
    for pat, repl, why in (TEXT_FIXES if fixes is None else fixes):
        new = re.sub(pat, repl, text)
        if new != text:
            applied.append((text, new, why))
            text = new
    return text


def classify(entries: list[dict], applied: list[tuple[str, str, str]], fixes=None) -> tuple[list[dict], list[str]]:
    """切出 序号/标题，判定层级，并做序号连续性检查。"""
    for e in entries:
        raw = re.sub(r"\s{2,}", " ", apply_text_fixes(e["raw"], applied, fixes)).strip()
        m = LABEL_RE.match(raw)
        if m:
            label = m.group(1).strip().replace(".", "、").replace("．", "、")
            label = re.sub(r"^(\d{1,2})\s*、$", r"\1、", label)
            title = raw[m.end():].strip()
        else:
            label, title = "", raw
        e["label"] = label
        e["title"] = title
        if label and CHAP_RE.match(label):
            e["level"] = 1
        elif label and CN_SEC_RE.match(label):
            e["level"] = 2
        elif label and NUM_ITEM_RE.match(label):
            e["level"] = 3
        else:
            e["level"] = 0  # 待定：无序号条目，看它落在章内还是节内

    # 无序号条目：本章已出现过「一、二、」节 → 归入节（三级），否则直接挂章下（二级）
    cur_chap_has_sec = False
    for e in entries:
        if e["level"] == 1:
            cur_chap_has_sec = False
        elif e["level"] == 2:
            cur_chap_has_sec = True
        elif e["level"] == 0:
            e["level"] = 3 if cur_chap_has_sec else 2

    # 序号连续性检查（只报告，不自动改，自动改由 TEXT_FIXES 负责）
    issues: list[str] = []
    prev_sec = None
    prev_item = None
    for e in entries:
        if e["level"] == 1:
            prev_sec = prev_item = None
        elif e["level"] == 2 and CN_SEC_RE.match(e["label"]):
            n = cn_to_int(e["label"][:-1])
            if prev_sec is not None and n != prev_sec + 1:
                issues.append(f"章内节序号跳变：{CN_NUM[prev_sec]}、→ {e['label']}{e['title']}")
            prev_sec, prev_item = n, None
        elif e["level"] == 3 and NUM_ITEM_RE.match(e["label"]):
            n = int(e["label"][:-1])
            if prev_item is not None and n != prev_item + 1:
                issues.append(f"节内条目序号跳变：{prev_item}、→ {e['label']}{e['title']}")
            prev_item = n
    return entries, issues


def cn_to_int(s: str) -> int:
    if s in CN_DIGITS and s != "十":
        return CN_DIGITS[s]
    if s == "十":
        return 10
    if s.startswith("十"):
        return 10 + CN_DIGITS.get(s[1:], 0)
    if "十" in s:
        a, b = s.split("十")
        return CN_DIGITS.get(a, 1) * 10 + (CN_DIGITS.get(b, 0) if b else 0)
    return CN_DIGITS.get(s, 0)


def parse_nav(region: str) -> list[dict]:
    """把已生成的 `<nav class="toc">` 反解析回条目（用于 --refresh 重生成附件）。

    不能用正则取行内容：无锚点的行是 `<span class="toc-row">`，其子元素也是
    `<span>`，非贪婪匹配会停在第一个子元素的闭合标签上。这里走 DOM 解析。
    """
    nav = re.search(r"<nav class=\"toc\".*?</nav>", region, re.S)
    if not nav:
        return []

    entries: list[dict] = []
    cur: dict | None = None
    capture = None
    buf: list[str] = []
    stack: list[str] = []

    class Extractor(HTMLParser):
        def handle_starttag(self, tag, attrs):
            nonlocal cur, capture, buf
            a = dict(attrs)
            cls = a.get("class", "")
            if "toc-row" in cls and cur is None:
                lv = re.search(r"toc-l(\d)", cls)
                cur = {"label": "", "title": "", "time": None, "page": "",
                       "level": int(lv.group(1)) if lv else 3,
                       "anchor": a.get("href", "").lstrip("#") or None,
                       "match": "已整理"}
                stack.append("row")
                return
            if cur is not None:
                stack.append(tag)
                for key in ("label", "title", "time", "page", "leader"):
                    if f"toc-{key}" in cls:
                        capture, buf = key, []
                        return

        def handle_data(self, data):
            nonlocal buf
            if cur is not None and capture:
                buf.append(data)

        def handle_endtag(self, tag):
            nonlocal cur, capture, buf
            if cur is None:
                return
            if capture is not None and tag == "span":
                txt = re.sub(r"\s+", " ", "".join(buf)).strip()
                if capture == "label":
                    cur["label"] = html.unescape(txt)
                elif capture == "title":
                    cur["title"] = html.unescape(txt)
                elif capture == "time":
                    cur["time"] = f"（{txt}）" if txt else None
                elif capture == "page":
                    cur["page"] = int(txt) if txt.isdigit() else 0
                capture, buf = None, []
            if stack:
                stack.pop()
            if not stack:                       # 行元素闭合 → 收一条
                cur["raw"] = cur["label"] + cur["title"]
                entries.append(cur)
                cur = None

    Extractor(convert_charrefs=True).feed(nav.group(0))
    return entries


# --------------------------------------------------------------------------
# 与正文锚点对齐
# --------------------------------------------------------------------------

def norm_for_match(text: str) -> str:
    t = html.unescape(re.sub(r"<[^>]+>", "", text))
    t = re.sub(r"[（(][^）)]*[）)]", "", t)          # 去掉括注（如「（主治痔疮）」）
    t = t.translate(MATCH_VARIANTS)
    t = re.sub(rf"[{re.escape(DASHES)}]", "", t)     # 去破折号/标点/空白
    t = re.sub(r"[^\w\u4e00-\u9fff]+", "", t)
    t = re.sub(r"^第[一二三四五六七八九十]+章", "", t)
    t = re.sub(r"^[一二三四五六七八九十]{1,3}、", "", t)
    t = re.sub(r"^\d{1,2}、", "", t)
    return t


def build_body_index(src: str, region: tuple[int, int]) -> dict:
    """正文标题索引：key = norm(标题)，value = 候选列表。"""
    lo, hi = region
    body = src[:lo] + src[hi:]
    index: dict[str, list[dict]] = {}
    for tag, hid, content in re.findall(r'<(h[1-6]) id="([^"]*)">(.*?)</\1>', body):
        plain = html.unescape(re.sub(r"<[^>]+>", "", content))
        key = norm_for_match(plain)
        if not key:
            continue
        tm = re.search(TIME, plain)
        index.setdefault(key, []).append({
            "id": hid, "text": plain, "tag": tag,
            "time": tm.group(0) if tm else None,
            "label": (LABEL_RE.match(plain).group(1) if LABEL_RE.match(plain) else ""),
        })
    return index


def link_entries(entries: list[dict], index: dict) -> None:
    used: dict[str, int] = {}
    for e in entries:
        e["anchor"] = None
        e["match"] = None
        if e["level"] == 1:            # 章标题在正文里没有对应标题
            continue
        key = norm_for_match(e["label"] + e["title"])
        cands = index.get(key)
        if not cands:
            cands = [c for c in index.get(norm_for_match(e["title"]), [])]
        if not cands:
            pool = list(index.items())
            close = difflib.get_close_matches(key, [k for k, _ in pool], n=1, cutoff=0.80)
            if close:
                ratio = difflib.SequenceMatcher(None, key, close[0]).ratio()
                cands = index[close[0]]
                e["match"] = f"近似({ratio:.2f})"
        if not cands:
            continue
        # 同 key 多候选时，按「序号一致 → 时间戳一致 → 出现顺序」择优
        best = None
        for c in cands:
            score = 0
            if e["label"] and c["label"] and e["label"] == c["label"]:
                score += 2
            if e["time"] and c["time"] and e["time"] == c["time"]:
                score += 3
            if best is None or score > best[0]:
                best = (score, c)
        c = best[1]
        e["anchor"] = c["id"]
        e["anchor_text"] = c["text"]
        if e["match"] is None:
            e["match"] = "精确" if (e["label"] and e["label"] == c["label"]) or e["time"] == c["time"] else "标题一致"
        used[c["id"]] = used.get(c["id"], 0) + 1


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------

TOC_CSS = """
/* ---- 目录（由 scripts/repair_toc.py 生成，勿手改） ---- */
nav.toc { margin: 1.4em 0 2em; font-size: 0.95em; line-height: 1.5; }
nav.toc ol { list-style: none; margin: 0; padding: 0; }
nav.toc li { list-style: none; }
nav.toc .toc-chapter { margin: 1.15em 0 0.6em; }
nav.toc .toc-sections { margin: 0.25em 0 0 0.2em; padding-left: 0.9em; border-left: 1px solid #e8e8e8; }
nav.toc .toc-items { margin: 0.15em 0 0.35em 0.2em; padding-left: 1.1em; }
nav.toc .toc-items-top { margin: 0.3em 0 0.5em 0.2em; padding-left: 0.9em; border-left: 1px solid #e8e8e8; }
nav.toc .toc-row { display: flex; align-items: baseline; gap: 0.15em; padding: 0.1em 0; color: inherit; text-decoration: none; border-radius: 3px; }
nav.toc a.toc-row:hover { background: #f2f6f2; }
nav.toc .toc-leader { flex: 1 1 auto; min-width: 1em; margin: 0 0.35em; border-bottom: 1px dotted #c8c8c8; transform: translateY(-0.3em); }
nav.toc .toc-time { color: #8b8b8b; font-size: 0.82em; font-variant-numeric: tabular-nums; white-space: nowrap; }
nav.toc .toc-page { color: #444; font-size: 0.88em; font-variant-numeric: tabular-nums; min-width: 2.4em; text-align: right; white-space: nowrap; }
nav.toc .toc-l1 { font-weight: 700; font-size: 1.06em; margin-top: 0.1em; }
nav.toc .toc-l2 { font-weight: 500; }
nav.toc .toc-l3 { color: #3a3a3a; }
nav.toc .toc-nolink { color: #9a9a9a; }
nav.toc .toc-nolink .toc-page { color: #9a9a9a; }
nav.toc .toc-note { color: #b0b0b0; font-size: 0.78em; }
"""


def esc(s: str) -> str:
    return html.escape(s, quote=False)


def render_row(e: dict) -> str:
    cls = f"toc-row toc-l{e['level']}"
    if not e["anchor"] and e["level"] > 1:
        cls += " toc-nolink"
    inner = (f'<span class="toc-label">{esc(e["label"])}</span>' if e["label"] else "")
    inner += f'<span class="toc-title">{esc(e["title"])}</span>'
    inner += '<span class="toc-leader"></span>'
    if e["time"]:
        inner += f'<span class="toc-time">{esc(e["time"].strip("（）"))}</span>'
    inner += f'<span class="toc-page">{e["page"]}</span>'
    if e["anchor"]:
        return f'<a class="{cls}" href="#{html.escape(e["anchor"], quote=True)}">{inner}</a>'
    return f'<span class="{cls}" title="正文无对应标题（原稿此处缺页或未生成标题）">{inner}</span>'


def build_tree(entries: list[dict]) -> list[dict]:
    """把扁平条目挂成 章 → 节 → 目 的树。章下无小节时，条目直接挂在章上。"""
    chapters: list[dict] = []
    root = {"sections": chapters}
    chapter = None
    section = None
    for e in entries:
        if e["level"] == 1:
            chapter = {"entry": e, "sections": [], "items": []}
            chapters.append(chapter)
            section = None
        elif e["level"] == 2:
            if chapter is None:
                chapter = {"entry": None, "sections": [], "items": []}
                chapters.append(chapter)
            section = {"entry": e, "items": []}
            chapter["sections"].append(section)
        else:
            if section is not None:
                section["items"].append(e)
            elif chapter is not None:
                chapter["items"].append(e)
            else:
                chapter = {"entry": None, "sections": [], "items": [e]}
                chapters.append(chapter)
    return chapters


def render_nav(entries: list[dict]) -> str:
    out = ['<nav class="toc" id="toc" aria-label="目录">', '<ol class="toc-chapters">']
    for ch in build_tree(entries):
        out.append('<li class="toc-chapter">')
        if ch["entry"] is not None:
            out.append(render_row(ch["entry"]))
        if ch["sections"]:
            out.append('<ol class="toc-sections">')
            for sec in ch["sections"]:
                out.append('<li class="toc-section">')
                out.append(render_row(sec["entry"]))
                if sec["items"]:
                    out.append('<ol class="toc-items">')
                    out.extend(f'<li class="toc-item">{render_row(i)}</li>' for i in sec["items"])
                    out.append("</ol>")
                out.append("</li>")
            out.append("</ol>")
        if ch["items"]:
            out.append('<ol class="toc-items toc-items-top">')
            out.extend(f'<li class="toc-item">{render_row(i)}</li>' for i in ch["items"])
            out.append("</ol>")
        out.append("</li>")
    out.append("</ol>")
    out.append("</nav>")
    return "\n".join(out)


def render_markdown(entries: list[dict], src_name: str = "") -> str:
    lines = ["# 目录", "",
             f"> 由 `scripts/repair_toc.py` 从 `{src_name}` 自动整理生成（源文件文字未改，仅重整目录结构）。",
             "> 反引号内为该讲视频时间戳（集-时:分:秒）与原书页码；`—` 表示原稿该条缺时间戳。", ""]
    for ch in build_tree(entries):
        if ch["entry"] is not None:
            e = ch["entry"]
            lines += ["", f"## {e['label']} {e['title']}　`{e['time'].strip('（）') if e['time'] else '—'} · P{e['page']}`", ""]
        for sec in ch["sections"]:
            e = sec["entry"]
            lines.append(f"- **{e['label']}{e['title']}**　`{e['time'].strip('（）') if e['time'] else '—'} · P{e['page']}`")
            for it in sec["items"]:
                lines.append(f"  - {it['label']}{it['title']}　`{it['time'].strip('（）') if it['time'] else '—'} · P{it['page']}`")
        for it in ch["items"]:
            lines.append(f"- {it['title']}　`{it['time'].strip('（）') if it['time'] else '—'} · P{it['page']}`")
    return "\n".join(lines) + "\n"


def render_json(entries: list[dict], src_name: str) -> str:
    doc = {
        "source": src_name,
        "generator": "scripts/repair_toc.py",
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "entry_count": len(entries),
        "entries": [
            {k: e.get(k) for k in ("level", "label", "title", "time", "page", "anchor", "match")}
            for e in entries
        ],
    }
    return json.dumps(doc, ensure_ascii=False, indent=2) + "\n"


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def repair(html_path: Path, write: bool, outdir: Path, refresh: bool = False) -> dict:
    src = html_path.read_text(encoding="utf-8")
    region = find_toc_region(src)
    already = '<nav class="toc" id="toc"' in src[region[0]:region[1]]
    if already and not refresh:
        raise AlreadyRepaired('目录区已是整理后的结构（<nav class="toc">），无需重复整理')
    if already:                     # 反解析：从已生成的 nav 还原条目，保证 HTML 不被二次处理
        entries = parse_nav(src[region[0]:region[1]])
        trailing = ""
        merge_log: list[str] = []
        issues_before: list[str] = []
        applied: list[tuple[str, str, str]] = []
        issues: list[str] = []
        dropped: list[dict] = []
        if not entries:
            raise SystemExit("已有 nav 中未解析出任何条目，拒绝继续（避免把目录清空）")
        # 重新与正文对齐一次，保证 refresh 出来的 md/json 与 --write 的产物一致
        for e in entries:
            e["anchor"] = None
        link_entries(entries, build_body_index(src, region))
        result = {
            "entries": entries, "issues": issues, "issues_before": issues_before,
            "applied": applied, "merge_log": merge_log, "dropped": dropped,
            "trailing": trailing, "region": region, "new_src": src,
            "raw_plain": "", "raw_bytes": 0, "raw_paras": 0,
            "new_bytes": len(src[region[0]:region[1]].encode("utf-8")),
        }
        # 锚点重连：正文补齐新标题后，原先 toc-nolink 的条目可以接上锚点
        nav_m = re.search(r'<nav class="toc".*?</nav>', src[region[0]:region[1]], re.S)
        new_nav = render_nav(entries)
        result["relinked"] = 0
        if nav_m and new_nav != nav_m.group(0):
            before = nav_m.group(0).count("toc-nolink")
            after = new_nav.count("toc-nolink")
            result["relinked"] = max(0, before - after)
            if write:
                bakdir = html_path.parent / ".backup"
                bakdir.mkdir(exist_ok=True)
                bak = bakdir / f"{html_path.name}.{time.strftime('%Y%m%d-%H%M%S')}.bak"
                if not bak.exists():
                    shutil.copy2(html_path, bak)
                result["backup"] = str(bak)
                new_src = src[:region[0]] + src[region[0]:region[1]].replace(
                    nav_m.group(0), new_nav) + src[region[1]:]
                html_path.write_text(new_src, encoding="utf-8")
                result["new_src"] = new_src
                result["region"] = region
        if write:
            outdir.mkdir(parents=True, exist_ok=True)
            (outdir / "目录.md").write_text(render_markdown(entries, html_path.name), encoding="utf-8")
            (outdir / "目录.json").write_text(render_json(entries, html_path.name), encoding="utf-8")
        return result

    plain = toc_plain_text(src[region[0]:region[1]])
    raw_region_html = src[region[0]:region[1]]
    entries, trailing = split_entries(plain)
    merge_log: list[str] = []
    entries = merge_stray_time(entries, merge_log)
    # 首条是「目录….2」自身
    dropped = [e for e in entries if e["raw"] in ("目录", "")]
    entries = [e for e in entries if e["raw"] not in ("目录", "")]
    applied: list[tuple[str, str, str]] = []
    # 先用「空白修正表」跑一遍：作为修正前的序号自检对照
    _, issues_before = classify([dict(e) for e in entries], [], fixes=[])
    entries, issues = classify(entries, applied)
    index = build_body_index(src, region)
    link_entries(entries, index)

    if len(entries) < 50:
        raise SystemExit(f"仅解析出 {len(entries)} 条目录项，疑似源文件格式不符，拒绝写回")
    nav = render_nav(entries)
    new_region = '<h2 id="目录">目录</h2>\n' + nav + "\n"
    new_src = src[:region[0]] + new_region + src[region[1]:]

    # CSS 注入（幂等）
    if "/* ---- 目录（由 scripts/repair_toc.py 生成" not in new_src:
        new_src = new_src.replace("</style>", TOC_CSS + "</style>", 1)

    result = {
        "entries": entries, "issues": issues, "issues_before": issues_before,
        "applied": applied, "merge_log": merge_log, "dropped": dropped,
        "trailing": trailing, "region": region, "new_src": new_src,
        "raw_plain": plain, "raw_bytes": len(raw_region_html.encode("utf-8")),
        "raw_paras": len(re.findall(r"<p>", raw_region_html)),
        "new_bytes": len(nav.encode("utf-8")),
    }
    if write:
        outdir.mkdir(parents=True, exist_ok=True)
        bakdir = html_path.parent / ".backup"
        bakdir.mkdir(exist_ok=True)
        bak = bakdir / f"{html_path.name}.{time.strftime('%Y%m%d-%H%M%S')}.bak"
        if not bak.exists():
            shutil.copy2(html_path, bak)
        result["backup"] = str(bak)
        html_path.write_text(new_src, encoding="utf-8")
        (outdir / "目录.md").write_text(render_markdown(entries, html_path.name), encoding="utf-8")
        (outdir / "目录.json").write_text(render_json(entries, html_path.name), encoding="utf-8")
    return result


def build_report(res: dict, html_path: Path) -> str:
    E = res["entries"]
    linked = [e for e in E if e["anchor"]]
    unlinked = [e for e in E if not e["anchor"]]
    lv = {1: sum(1 for e in E if e["level"] == 1),
          2: sum(1 for e in E if e["level"] == 2),
          3: sum(1 for e in E if e["level"] == 3)}
    L: list[str] = []
    A = L.append
    A("# 目录整理报告")
    A("")
    A(f"- 源文件：`{html_path}`")
    A(f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    A(f"- 生成工具：`scripts/repair_toc.py`")
    A("")
    A("## 0. 整理前是什么样（原始目录区）")
    A("")
    if res.get("raw_plain"):
        A(f"原始目录区是 **{res['raw_paras']} 个 `<p>` 段落、{res['raw_bytes']} 字节**，"
          "一条条目录项用「……页码」串在一行里：")
        A("")
        A("```text")
        A(res["raw_plain"][:420] + " …")
        A("```")
        A("")
        A(f"整理后变成 `<nav class=\"toc\">` 三级嵌套结构（{res['new_bytes']} 字节）："
          "一条一行、层级缩进、页码右对齐、可点击跳正文。")
    else:
        A("（本次为 --refresh 模式，未重新解析原始目录区）")
    A("")
    A("## 1. 结果概览")
    A("")
    A("| 指标 | 数值 |")
    A("|---|---|")
    A(f"| 解析出目录项 | **{len(E)}** 条（原有段落 10 个） |")
    A(f"| 章 / 节 / 目 | {lv[1]} / {lv[2]} / {lv[3]} |")
    A(f"| 已链接到正文锚点 | **{len(linked)}** 条 |")
    A(f"| 正文无对应标题（不可跳转） | {len(unlinked)} 条 |")
    A(f"| 结构修正 | {len(res['applied'])} 处 |")
    A(f"| 时间戳错位合并 | {len(res['merge_log'])} 处 |")
    A("")
    A("## 2. 结构修正明细（改了什么、为什么）")
    A("")
    A("| 原目录文本 | 整理后 | 依据 |")
    A("|---|---|---|")
    for old, new, why in res["applied"]:
        A(f"| `{old}` | `{new}` | {why} |")
    for m in res["merge_log"]:
        A(f"| （条目被切开） | 已合并 | {m} |")
    A("")
    A("## 3. 序号连续性自检")
    A("")
    A("### 3.1 修正前（脚本自动检出）")
    A("")
    if res["issues_before"]:
        for i in res["issues_before"]:
            A(f"- ❌ {i}")
    else:
        A("- 未检出跳变")
    A("")
    A("### 3.2 修正后（自检通过才算整理完成）")
    A("")
    if res["issues"]:
        for i in res["issues"]:
            A(f"- ❌ {i}")
    else:
        A("- ✅ 章 / 节 / 目 序号连续，无跳变、无重号")
    A("")
    A("## 4. 未能定位正文锚点的条目")
    A("")
    A(f"共 {len(unlinked)} 条。这部分无法跳转，原因是 **HTML 正文在第 8 讲「五俞穴治疗与原络治疗」处即结束**，"
      "第五章后半与第六章的内容不在本文件内；另有少数条目是原稿标题本身缺失。")
    A("")
    for e in unlinked:
        A(f"- `{e['label']} {e['title']}`（P{e['page']}）")
    A("")
    A("## 5. 保留未改的可疑写法（正文全稿一致，未擅自替换）")
    A("")
    A("| 原写法 | 通行写法 | 说明 |")
    A("|---|---|---|")
    for a, b, c in SUSPECT_NOTES:
        A(f"| {a} | {b} | {c} |")
    A("")
    A("## 6. 复核方式")
    A("")
    A("```bash")
    A("python3 scripts/repair_toc.py --check          # 只读检查，不改文件")
    A(f"diff -u {html_path.parent / '.backup'}/<备份文件名> {html_path} | head -200")
    A("```")
    return "\n".join(L) + "\n"


def resolve_source(spec: str) -> Path:
    """支持直接给 html 路径，或给通配/目录，自动挑出含目录区的书稿。"""
    p = Path(spec)
    cands: list[Path] = []
    if any(ch in spec for ch in "*?["):
        cands = sorted(Path().glob(spec))
    elif p.is_dir():
        cands = sorted(p.glob("*.html"))
    elif p.exists():
        cands = [p]
    else:
        raise SystemExit(f"文件不存在：{p.resolve()}")
    for c in cands:
        try:
            if '<h2 id="目录">' in c.read_text(encoding="utf-8", errors="ignore")[:400000]:
                return c.resolve()
        except OSError:
            continue
    if len(cands) == 1:
        return cands[0].resolve()
    raise SystemExit(f"未能在 {spec} 中定位含目录区的书稿 HTML（候选：{[str(c) for c in cands]}）")


def main() -> int:
    ap = argparse.ArgumentParser(description="整理 OCR 版 HTML 书稿的目录区")
    ap.add_argument("--file", default="book/1针灸/*.html",
                    help="书稿 HTML；给目录时会取其中「含 <h2 id=\"目录\">」的那个")
    ap.add_argument("--outdir", default=None, help="目录.md / 目录.json 输出目录（默认与 html 同目录）")
    ap.add_argument("--write", action="store_true", help="写回 HTML（会先备份到 .backup/）")
    ap.add_argument("--check", action="store_true", help="只解析并打印，不写文件")
    ap.add_argument("--report", default=None, help="额外输出整理报告路径（默认随 --write 生成）")
    ap.add_argument("--relink", action="store_true",
                    help="只做锚点重连：正文补齐了新标题时，把原来不可点的目录项接上")
    ap.add_argument("--refresh", action="store_true",
                    help="目录已是整理后的结构时，从已有 <nav class=\"toc\"> 反解析，"
                         "仅重生成 目录.md / 目录.json / 报告（不动 HTML）")
    args = ap.parse_args()

    html_path = resolve_source(args.file)
    outdir = Path(args.outdir).resolve() if args.outdir else html_path.parent

    try:
        res = repair(html_path, write=args.write, outdir=outdir,
                     refresh=(args.refresh or args.relink))
    except AlreadyRepaired as exc:
        print(f"ℹ️  {exc}")
        print(f"   文件：{html_path}")
        print("   如需从备份重来：先用 .backup/ 下的原始文件覆盖，再执行 --write")
        return 0
    E = res["entries"]
    print(f"目录区字节区间：{res['region']}")
    print(f"解析条目：{len(E)} 条；章 {sum(1 for e in E if e['level'] == 1)} / "
          f"节 {sum(1 for e in E if e['level'] == 2)} / 目 {sum(1 for e in E if e['level'] == 3)}")
    print(f"链接锚点：{sum(1 for e in E if e['anchor'])} 条；"
          f"无锚点：{sum(1 for e in E if not e['anchor'])} 条")
    print(f"结构修正：{len(res['applied'])} 处；时间戳错位合并：{len(res['merge_log'])} 处")
    if res["issues"]:
        print("序号自检：")
        for i in res["issues"]:
            print("  -", i)
    else:
        print("序号自检：无跳变")
    if res["trailing"]:
        print(f"⚠ 末尾未匹配残文：{res['trailing'][:120]}")
    for e in E[:8]:
        print(f"  L{e['level']} | {e['label']}{e['title']} | {e['time'] or '-':15s} | P{e['page']} | "
              f"{'#' + e['anchor'] if e['anchor'] else '（无锚点）'}")
    print("  …")
    if args.write and (args.refresh or args.relink):
        if res.get("relinked"):
            print("✅ 锚点重连：%d 条目录项接上了正文新标题（备份：%s）"
                  % (res["relinked"], res.get("backup")))
        else:
            print("ℹ️ 锚点无需重连（没有新可匹配的正文标题）")
        print("ℹ️ refresh/relink 模式只重生成 目录.md / 目录.json；报告需从原始文件重跑 --write 才会刷新")
    elif args.write:
        report_path = Path(args.report).resolve() if args.report else outdir / "目录整理报告.md"
        report_path.write_text(build_report(res, html_path), encoding="utf-8")
        print(f"✅ 已写回：{html_path}")
        print(f"✅ 备份：{res['backup']}")
        print(f"✅ 目录：{outdir / '目录.md'}、{outdir / '目录.json'}")
        print(f"✅ 报告：{report_path}")
    else:
        if args.report:
            Path(args.report).write_text(build_report(res, html_path), encoding="utf-8")
            print(f"报告：{args.report}")
        print("（未加 --write，HTML 未改动）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
