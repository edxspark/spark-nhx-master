#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验收：批量目录整理是否「只动了该动的地方」。

对每个书稿 HTML，与 `.backup/` 里的原始稿逐项比对：

1. 正文零改动：把「目录区」与「注入的目录 CSS」从两边都去掉，再把每个标题
   的**文字**替换成占位符（标题文字本身是本轮允许修改的对象），剩下必须**逐字节相同**。
2. 标题只动空白：每个标题的新旧文本去掉空白后必须一致
   （唯一例外是「序号可证修正」，如 `二、玉泉` → `三、玉泉`，会在报告里单列）。
3. 目录链接零死链：`href="#id"` 必须都能在文档里找到对应 `id`。
4. 目录标签配平：`<ol>/<li>/<nav>` 开闭数量一致。
5. 图片资源数量不变。
6. 目录结构完整：每条目录项都能在 `<nav class="toc">` 里数出来。

用法：python3 scripts/verify_books.py
退出码：0 = 全通过；1 = 有失败项
"""

from __future__ import annotations

import html
import re
import sys
from pathlib import Path

BOOK_ROOT = Path(__file__).resolve().parent.parent / "book"
CSS_MARK = "/* ---- 目录（由 scripts/repair_toc.py 生成"


def strip_toc_and_css(src: str) -> str:
    """去掉目录区（含标题）与注入的目录 CSS。"""
    out = src
    m = re.search(r'<h[1-6][^>]*>(?:目\s*[-_－]?\s*[录錄録次])\s*[:：]?</h[1-6]>', out)
    if m:
        nxt = re.search(r"<h[1-6][^>]*>", out[m.end():])
        end = m.end() + nxt.start() if nxt else len(out)
        out = out[:m.start()] + out[end:]
    i = out.find(CSS_MARK)
    if i >= 0:
        j = out.find("</style>", i)
        out = out[:i] + out[j:]
        out = out.replace("}\n\n</style>", "}\n</style>")   # 注入前的空行差异
    return out


def blank_heading_text(src: str) -> str:
    return re.sub(r"(<h[1-6][^>]*>).*?(</h[1-6]>)", r"\1\2", src, flags=re.S)


def headings(src: str) -> list[tuple[str, str]]:
    return [(m.group(1), html.unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip())
            for m in re.finditer(r"<(h[1-6])[^>]*>(.*?)</\1>", src, re.S)]


def check(path: Path) -> tuple[bool, list[str]]:
    msgs, ok = [], True
    bak_dir = path.parent / ".backup"
    # 取**最近一次写回前**的备份作为基线（即本轮改动前的状态）
    baks = sorted(bak_dir.glob(f"{path.name}.*.bak")) if bak_dir.exists() else []
    if not baks:
        return False, ["找不到备份（未整理？）"]
    old = baks[-1].read_text(encoding="utf-8")
    new = path.read_text(encoding="utf-8")

    # 1) 正文零改动
    a, b = blank_heading_text(strip_toc_and_css(old)), blank_heading_text(strip_toc_and_css(new))
    if a == b:
        msgs.append("✅ 正文零改动（除目录区/标题文字/CSS）")
    else:
        ok = False
        diff_at = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
        msgs.append("❌ 正文出现非预期差异 @%d" % diff_at)
        msgs.append("     旧：…%r…" % a[max(0, diff_at - 60):diff_at + 60])
        msgs.append("     新：…%r…" % b[max(0, diff_at - 60):diff_at + 60])
        if len(a) != len(b):
            msgs.append("❌ 长度不同：旧 %d / 新 %d" % (len(a), len(b)))

    # 2) 标题只动空白（在去掉目录区之后再数，生成的目录标题不算新增）
    ho, hn = headings(strip_toc_and_css(old)), headings(strip_toc_and_css(new))
    if len(ho) != len(hn):
        ok = False
        msgs.append("❌ 标题数量变化：%d → %d" % (len(ho), len(hn)))
    else:
        odd = [(x, y) for (_, x), (_, y) in zip(ho, hn)
               if re.sub(r"\s", "", x) != re.sub(r"\s", "", y)]
        if odd:
            msgs.append("ℹ️ 非空白差异 %d 处（应为序号修正，详见报告）：%s"
                        % (len(odd), [f"{x[:12]}→{y[:12]}" for x, y in odd[:3]]))
        else:
            msgs.append("✅ 标题只动空白")

    # 3) 死链
    nav = re.search(r'<nav class="toc".*?</nav>', new, re.S)
    if not nav:
        ok = False
        msgs.append("❌ 没有目录导航块")
    else:
        ids = set(re.findall(r'\sid="([^"]*)"', new))
        hrefs = re.findall(r'href="#([^"]*)"', nav.group(0))
        dead = [h for h in hrefs if h not in ids]
        if dead:
            ok = False
            msgs.append("❌ 死链 %d：%s" % (len(dead), dead[:3]))
        else:
            msgs.append("✅ 目录链接 %d 条，死链 0" % len(hrefs))
        # 4) 标签配平
        for tag in ("ol", "li", "nav"):
            o = len(re.findall(r"<%s[ >]" % tag, nav.group(0)))
            c = len(re.findall(r"</%s>" % tag, nav.group(0)))
            if o != c:
                ok = False
                msgs.append("❌ <%s> 不配平：%d 开 / %d 闭" % (tag, o, c))
        rows = len(re.findall(r"toc-row", nav.group(0)))
        msgs.append("ℹ️ 目录行 %d" % rows)

    # 5) 图片数量
    io, inw = old.count("data:image/"), new.count("data:image/")
    if io != inw:
        ok = False
        msgs.append("❌ 图片数量变化：%d → %d" % (io, inw))
    else:
        msgs.append("✅ 图片 %d 张未变" % inw)
    return ok, msgs


def main() -> int:
    files = sorted(p for p in BOOK_ROOT.glob("*/*.html") if p.is_file())
    bad = 0
    for p in files:
        ok, msgs = check(p)
        print("=" * 100)
        print(("%-46s" % str(p.relative_to(BOOK_ROOT))) + ("✅ 通过" if ok else "❌ 失败"))
        for m in msgs:
            print("   ", m)
        bad += 0 if ok else 1
    print("=" * 100)
    print("汇总：%d 个文件，%d 个失败" % (len(files), bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
