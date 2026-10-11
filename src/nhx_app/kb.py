"""倪师 AI · 知识库检索

检索 `knowledge/` 下的三类数据，为回答提供**可溯源的原文依据**。

三类数据的检索方式不同（对应 AGENT.md §5 检索原则）：

| 类别 | 方式 | 原因 |
|---|---|---|
| 经典原文（伤寒/金匮…） | **精确检索**（条文号 / 关键词） | 原文不容近似（红线 R1） |
| 方剂 / 本草 / 穴位 | **结构化查询** | 字段稳定，原样取用 |
| 汉唐文章 / 讲义 | **关键词打分** | 语义相近但用词不同 |

零依赖：不引入向量库。中小企业量级（几十 MB 文本）关键词打分足够，
且**确定性**——同样的查询给同样的结果，便于复现与审计。

索引在首次查询时惰性构建，之后常驻内存；文件变更时按 mtime 失效重建。
"""

import json
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional

# 中文按字切分，英文按词
_CJK_RUN = re.compile(r"[\u4e00-\u9fa5]+")
_WORD = re.compile(r"[A-Za-z0-9_]+")


# ── 条号解析（「原文走精确」）────────────────────────────────────────────────
#
# AGENT.md §5 检索原则一：原文走精确、讲解走语义。
# 关键词打分对「第 12 条」这种查询是**失效**的——整部伤寒论被索引成一个大文本块，
# 打分只会在块内随便挑一个片段返回，实测出现过问第 12 条却返回第 110 条的情况。
# 因此必须先识别条号引用，走 find_article 精确取原文。

_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9}

# 「第12条」「第十二条」「第 12 条」「12条」「第12條」
_ARTICLE_REF_RE = re.compile(
    r"第?\s*([0-9]{1,3}|[零一二两三四五六七八九十百]{1,5})\s*[条條]"
)


def cn_to_int(text: str) -> Optional[int]:
    """中文数字转阿拉伯数字（支持 一二三…十…百 的常见组合）。"""
    if not text:
        return None
    if text.isdigit():
        return int(text)

    total, section, number = 0, 0, 0     # 百位累计 / 十位 / 个位
    for ch in text:
        if ch in _CN_DIGITS:
            number = _CN_DIGITS[ch]
        elif ch == "十":
            section += (number or 1) * 10
            number = 0
        elif ch == "百":
            total += (number or 1) * 100
            number = 0
        else:
            return None
    return total + section + number


def extract_article_refs(query: str) -> List[int]:
    """从查询里抽出条文号引用。返回按出现顺序去重的条号列表。"""
    out: List[int] = []
    for m in _ARTICLE_REF_RE.finditer(query or ""):
        n = cn_to_int(m.group(1))
        if n and 1 <= n <= 999 and n not in out:
            out.append(n)
    return out


def tokenize(text: str) -> List[str]:
    """中文按**二元组**切分，英文按词。

    为什么不用单字：中文单字噪声极大——「口苦」会命中所有含「口」的文档。
    二元组能显著提升精度，且不需要分词词表（零依赖）。
    单字run（长度为 1）保留原字，以支持单字查询。
    """
    text = text or ""
    tokens: List[str] = []

    for run in _CJK_RUN.findall(text):
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[i:i + 2] for i in range(len(run) - 1))

    tokens.extend(w.lower() for w in _WORD.findall(text))
    return tokens

# 不参与索引的目录（结构化数据**必须**参与——find_formula / find_herb 依赖它）
_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".idea"}
_TEXT_EXTS = {".md", ".txt", ".json"}


class Document:
    __slots__ = ("path", "rel", "title", "text", "kind", "meta")

    def __init__(self, path: str, rel: str, title: str, text: str,
                 kind: str, meta: Optional[Dict[str, Any]] = None):
        self.path = path
        self.rel = rel
        self.title = title
        self.text = text
        self.kind = kind      # classical / formula / herb / article / other
        self.meta = meta or {}


class KnowledgeBase:
    def __init__(self, root: str, max_snippets: int = 5):
        self.root = root
        self.max_snippets = max_snippets
        self._docs: List[Document] = []
        self._index: Dict[str, List[int]] = {}
        self._mtime = 0.0
        self._lock = threading.Lock()

    # ── 索引 ────────────────────────────────────────────────────────────────

    def _newest_mtime(self) -> float:
        newest = 0.0
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for fn in filenames:
                if os.path.splitext(fn)[1].lower() in _TEXT_EXTS:
                    try:
                        newest = max(newest, os.path.getmtime(os.path.join(dirpath, fn)))
                    except OSError:
                        continue
        return newest

    def _classify(self, rel: str) -> str:
        base = os.path.basename(rel)
        # 说明与报告类文件不是知识源，检索时应排除
        if base.startswith("README") or "报告" in base or base.endswith(".meta.md"):
            return "meta"
        if "结构化数据/方剂库" in rel:
            return "formula"
        if "结构化数据/本草库" in rel:
            return "herb"
        if "结构化数据/穴位库" in rel:
            return "point"
        if "/原文/" in rel:
            return "classical"
        if "03-汉唐文章" in rel or "05-倪师" in rel or "/讲义/" in rel:
            return "article"
        return "other"

    def _load_doc(self, path: str) -> Optional[Document]:
        rel = os.path.relpath(path, self.root)
        ext = os.path.splitext(path)[1].lower()
        kind = self._classify(rel)

        try:
            if ext == ".json":
                with open(path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)

                # 条文型 JSON（伤寒论 / 金匮要略）——必须先判，否则会被通用分支吞掉
                if isinstance(data, dict) and ("articles" in data or "chapters" in data):
                    name = data.get("book") or os.path.basename(path)
                    chunks = []
                    for art in data.get("articles") or []:
                        chunks.append("[第 %s 条] %s" % (art.get("no"), art.get("text", "")))
                    for ch in data.get("chapters") or []:
                        chunks.append("[%s] %s" % (ch.get("title", ""), ch.get("text", "")))
                    return Document(path, rel, name, "\n".join(chunks), "classical", data)

                # 结构化条目（方剂 / 本草 / 穴位）
                if isinstance(data, dict):
                    name = data.get("name") or os.path.splitext(os.path.basename(path))[0]
                    parts = [str(v) for v in (
                        data.get("name"), data.get("original_text"),
                        data.get("preparation"),
                        " ".join(data.get("effects") or []),
                        " ".join(data.get("indications") or []),
                        " ".join(str(c) for c in (data.get("composition") or [])),
                    ) if v]
                    return Document(path, rel, name, "\n".join(parts), kind, data)

                return None
            else:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
                title = os.path.splitext(os.path.basename(path))[0]
                first = text.lstrip().split("\n", 1)[0]
                if first.startswith("#"):
                    title = first.lstrip("# ").strip()
                return Document(path, rel, title, text, kind, None)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return None

    def build(self, force: bool = False) -> int:
        """构建索引。文件未更新则复用。"""
        with self._lock:
            newest = self._newest_mtime()
            if not force and self._docs and newest <= self._mtime:
                return len(self._docs)

            docs: List[Document] = []
            if os.path.isdir(self.root):
                for dirpath, dirnames, filenames in os.walk(self.root):
                    dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
                    for fn in sorted(filenames):
                        if fn.lower() == "readme.md":
                            continue
                        if os.path.splitext(fn)[1].lower() not in _TEXT_EXTS:
                            continue
                        doc = self._load_doc(os.path.join(dirpath, fn))
                        if doc and doc.text.strip():
                            docs.append(doc)

            index: Dict[str, List[int]] = {}
            for i, doc in enumerate(docs):
                seen = set(tokenize(doc.title)) | set(tokenize(doc.text))
                for tok in seen:
                    index.setdefault(tok, []).append(i)

            self._docs, self._index, self._mtime = docs, index, newest
            return len(docs)

    @property
    def doc_count(self) -> int:
        return len(self._docs)

    # ── 检索 ────────────────────────────────────────────────────────────────

    def search(self, query: str, limit: Optional[int] = None,
               kinds: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """关键词检索。返回带评分与片段的结果。

        **默认排除 `meta` 类**（README、采集报告等说明文件）——
        它们不是知识源，进检索结果只会污染上下文，还可能被当成依据引用。
        需要时必须显式在 `kinds` 里包含 `meta`。
        """
        if not self._docs:
            self.build()
        limit = limit or self.max_snippets
        q_tokens = tokenize(query)
        if not q_tokens:
            return []

        # 去重保序
        uniq, seen = [], set()
        for t in q_tokens:
            if t not in seen:
                seen.add(t)
                uniq.append(t)

        n_docs = max(len(self._docs), 1)
        scores: Dict[int, float] = {}
        for tok in uniq:
            ids = self._index.get(tok)
            if not ids:
                continue
            # 稀有词权重更高（简化版 IDF）
            idf = max(0.2, 1.0 - (len(ids) / n_docs))
            for i in ids:
                scores[i] = scores.get(i, 0.0) + idf

        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)

        # 标题命中加权
        def title_bonus(i: int) -> float:
            t = self._docs[i].title
            return sum(3.0 for tok in uniq if tok in t)

        ranked = sorted(ranked, key=lambda kv: kv[1] + title_bonus(kv[0]), reverse=True)

        allow = set(kinds) if kinds else None
        out: List[Dict[str, Any]] = []
        for i, score in ranked:
            doc = self._docs[i]
            if doc.kind == "meta" and not (allow and "meta" in allow):
                continue
            if allow and doc.kind not in allow:
                continue
            out.append({
                "title": doc.title,
                "path": doc.rel,
                "kind": doc.kind,
                "score": round(score + title_bonus(i), 3),
                "snippet": self._snippet(doc.text, uniq),
            })
            if len(out) >= limit:
                break
        return out

    @staticmethod
    def _snippet(text: str, tokens: List[str], width: int = 220) -> str:
        """截取命中密度最高的片段。"""
        best_pos, best_hits = 0, -1
        step = max(1, width // 3)
        for pos in range(0, max(1, len(text) - width), step):
            window = text[pos:pos + width]
            hits = sum(1 for t in tokens if t in window)
            if hits > best_hits:
                best_pos, best_hits = pos, hits
                if hits == len(tokens):
                    break
        snippet = text[best_pos:best_pos + width].strip()
        return ("…" if best_pos > 0 else "") + snippet + ("…" if best_pos + width < len(text) else "")

    # ── 结构化精确查询 ──────────────────────────────────────────────────────

    def find_formula(self, name: str) -> Optional[Dict[str, Any]]:
        """按方名精确取方剂对象（原样取用，红线 R1）。"""
        if not self._docs:
            self.build()
        for doc in self._docs:
            if doc.kind == "formula" and isinstance(doc.meta, dict):
                if doc.meta.get("name") == name:
                    return doc.meta
        for doc in self._docs:
            if doc.kind == "formula" and isinstance(doc.meta, dict):
                if name in (doc.meta.get("aliases") or []):
                    return doc.meta
        return None

    def find_herb(self, name: str) -> Optional[Dict[str, Any]]:
        if not self._docs:
            self.build()
        for doc in self._docs:
            if doc.kind == "herb" and isinstance(doc.meta, dict):
                if doc.meta.get("name") == name:
                    return doc.meta
        return None

    def find_article(self, no: int) -> Optional[Dict[str, Any]]:
        """按条文号取原文（红线上：原文必须逐字，不能由模型复述）。"""
        if not self._docs:
            self.build()
        for doc in self._docs:
            if doc.kind != "classical" or not isinstance(doc.meta, dict):
                continue
            for art in doc.meta.get("articles") or []:
                if art.get("no") == no:
                    return {"book": doc.meta.get("book"), "no": no,
                            "text": art.get("text"),
                            "source": doc.meta.get("source")}
        return None

    # ── 供提示词使用 ────────────────────────────────────────────────────────

    def context_block(self, query: str, layer: str,
                      max_snippets: Optional[int] = None) -> str:
        """把检索结果拼成给模型的上下文块。无命中则返回空串。"""
        limit = max_snippets or self.max_snippets
        kinds = None
        if layer == "L1":
            kinds = ["classical", "formula", "herb", "point", "article"]
        elif layer == "L2":
            kinds = ["classical", "formula", "herb", "article"]
        elif layer == "L3":
            kinds = ["article", "other"]
        # meta（说明/报告）永不参与检索

        # ① 先解析条号引用，精确取原文（AGENT.md §5 原则一：原文走精确）
        exact_block = self._exact_articles(query)

        results = self.search(query, limit=limit, kinds=kinds)
        if not results and not exact_block:
            return ""

        # 命中条号时，**丢掉经典原文的关键词片段**：那些片段正是「问第 12 条返回第 110 条」
        # 的元凶。精确结果已权威，再混入无关条号只会误导模型（实测踩到）。
        # 结构化条目（方剂/本草）保留——它们没有条号混淆问题，且常是必要的补充。
        if exact_block:
            results = [r for r in results if r["kind"] != "classical"]

        # 结构化条目小且字段稳定，**整条给出**；
        # 若只给截断片段，模型无法逐字复述组成，就只能含糊其辞或凭记忆补——
        # 那恰恰违反 R1。实测：桂枝汤组成被截断后，模型明确说「记录是截断的」并拒绝补全。
        # 这是对的姿态，但根因在我们没给全材料。
        FULL_KINDS = {"formula", "herb", "point"}

        lines = ["## 知识库检索结果（请优先依据以下材料作答，并标注来源）", ""]

        if exact_block:
            lines.append(exact_block)
            lines.append("")

        for r in results:
            lines.append("### %s" % r["title"])
            lines.append("来源路径：`%s`（类型：%s）" % (r["path"], r["kind"]))
            lines.append("")
            if r["kind"] in FULL_KINDS:
                body = self._full_text(r)
                lines.append("**完整记录（原文原样，可直接引用）**：")
                lines.append("")
                lines.append("```json")
                lines.append(body)
                lines.append("```")
            else:
                lines.append(r["snippet"])
            lines.append("")
        lines.append("> 若以上材料不足以回答，请明确说明「知识库暂未收录」，不要凭记忆补充原文。")
        return "\n".join(lines)

    def _exact_articles(self, query: str) -> str:
        """按条号精确取原文。

        关键词打分对「第 N 条」这类查询是失效的——整部伤寒论被索引成一个大文本块，
        打分只会在块内挑一个片段，实测出现过问第 12 条却返回第 110 条的情况。
        （这正是 AGENT.md §5「原文走精确、讲解走语义」要解决的问题。）
        """
        refs = extract_article_refs(query)
        if not refs:
            return ""

        chunks = []
        for no in refs:
            art = self.find_article(no)
            if not art:
                chunks.append("### 《伤寒论》第 %d 条\n\n**知识库中未找到该条**——"
                              "请如实说明「未收录」，不要凭记忆补写原文。" % no)
                continue
            src = art.get("source") or {}
            url = src.get("url") if isinstance(src, dict) else None
            chunks.append(
                "### 《%s》第 %d 条（**原文原样，可直接引用**）\n\n"
                "%s\n\n> 来源：%s%s"
                % (art.get("book") or "伤寒论", no, art.get("text") or "",
                   (src.get("book") if isinstance(src, dict) else "伤寒论") or "伤寒论",
                   ("  " + url) if url else "")
            )
        return "\n\n".join(chunks)

    def _full_text(self, result: Dict[str, Any]) -> str:
        """取结构化条目的完整 JSON（上限放宽，防止个别超大文件撑爆上下文）。"""
        import json as _json
        for doc in self._docs:
            if doc.rel == result["path"] and isinstance(doc.meta, dict):
                text = _json.dumps(doc.meta, ensure_ascii=False, indent=2)
                if len(text) > 8000:
                    text = text[:8000] + "\n…（记录过长已截断）"
                return text
        return result["snippet"]

    def stats(self) -> Dict[str, Any]:
        if not self._docs:
            self.build()
        by_kind: Dict[str, int] = {}
        for d in self._docs:
            by_kind[d.kind] = by_kind.get(d.kind, 0) + 1
        return {"documents": len(self._docs), "by_kind": by_kind,
                "root": self.root, "indexed_at": self._mtime}
