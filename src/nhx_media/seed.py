"""用调研实测数据初始化媒体索引。

数据来源：`docs/research/资源清单-原始站点.md` §2.1.3（实测，非站方声明）。

用法：
    python3 src/nhx_media/seed.py            # 初始化（已存在则跳过）
    python3 src/nhx_media/seed.py --force    # 覆盖重建
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from store import ensure_dirs, list_series, save_series  # noqa: E402

FETCHED_AT = "2025-10-11"

# ── 系列定义 ────────────────────────────────────────────────────────────────
# count_measured = 实测集数（None 表示未验证，沿用站方声明 count_declared）

SERIES = [
    {
        "kind": "video", "series_id": "renji",
        "name": "人纪",
        "description": "倪师中医核心教学：针灸大成、黄帝内经、神农本草经、伤寒论、金匮要略五大经典。",
        "subjects": [
            {"subject": "针灸大成", "prefix": "zhenjiu", "count_measured": 63, "count_declared": 78,
             "source_type": "bilibili", "source_url": "https://www.bilibili.com/video/BV1NeuczHEPm",
             "note": "⚠️ 站方声明 78 集，实测 63 条（标题《【完整版】倪海厦针灸大成字幕版（人纪）》）"},
            {"subject": "黄帝内经", "prefix": "huangdi", "count_measured": 80, "count_declared": 75,
             "source_type": "bilibili", "source_url": "https://www.bilibili.com/video/BV1NM4y1M7yK",
             "note": "⚠️ 站方声明 75 集，实测 80 条"},
            {"subject": "神农本草经", "prefix": "shennong", "count_measured": None, "count_declared": 39,
             "source_type": "bilibili", "source_url": "https://www.bilibili.com/video/BV1hx411r7zs",
             "note": "集数未验证"},
            {"subject": "伤寒论", "prefix": "shanghan", "count_measured": None, "count_declared": 58,
             "source_type": "bilibili", "source_url": "https://www.bilibili.com/video/BV1H24y147L1",
             "note": "集数未验证"},
            {"subject": "金匮要略", "prefix": "jinkui", "count_measured": None, "count_declared": 77,
             "source_type": "bilibili", "source_url": "https://www.bilibili.com/video/BV1uD4y1W7FS",
             "note": "集数未验证"},
        ],
    },
    {
        "kind": "video", "series_id": "tianji",
        "name": "天纪",
        "description": "倪师术数教学：易经、紫微斗数、风水堪舆、占卜。",
        "subjects": [
            {"subject": "天纪完整", "prefix": "tianji-full", "count_measured": None, "count_declared": 15,
             "source_type": "bilibili", "source_url": "https://www.bilibili.com/video/BV11a411C7NS",
             "note": "集数未验证"},
            {"subject": "天纪重点分解", "prefix": "tianji-focus", "count_measured": None, "count_declared": 151,
             "source_type": "bilibili", "source_url": "https://www.bilibili.com/video/av674892388",
             "note": "含易经 64 卦、紫微 12 宫；集数未验证"},
            {"subject": "堪舆学-找龙穴", "prefix": "kanyu", "count_measured": None, "count_declared": 4,
             "source_type": "bilibili", "source_url": "https://www.bilibili.com/video/BV1Y34y1r71f",
             "note": "集数未验证"},
        ],
    },
    {
        "kind": "audio", "series_id": "ximalaya",
        "name": "喜马拉雅音频专辑",
        "description": "喜马拉雅平台上的倪师音频专辑（外链，站方不自托管）。",
        "albums": [
            {"subject": "伤寒论 MP3", "prefix": "shanghan-mp3", "count_declared": 58,
             "source_url": "https://www.ximalaya.com/jiankang/43391953"},
            {"subject": "人纪合辑", "prefix": "renji-mp3", "count_declared": 100,
             "source_url": "https://www.ximalaya.com/jiankang/23986315"},
            {"subject": "天纪合辑", "prefix": "tianji-mp3", "count_declared": 24,
             "source_url": "https://www.ximalaya.com/jiankang/18431421"},
            {"subject": "黄帝内经 MP3", "prefix": "huangdi-mp3", "count_declared": 75,
             "source_url": "https://www.ximalaya.com/jiankang/30999171"},
            {"subject": "神农本草经 MP3", "prefix": "shennong-mp3", "count_declared": 39,
             "source_url": "https://www.ximalaya.com/jiankang/16091265"},
            {"subject": "金匮要略 MP3", "prefix": "jinkui-mp3", "count_declared": 77,
             "source_url": "https://www.ximalaya.com/jiankang/3513916"},
            {"subject": "针灸大成 MP3", "prefix": "zhenjiu-mp3", "count_declared": 63,
             "source_url": "https://www.ximalaya.com/jiankang/3504885"},
            {"subject": "倪师演讲合集", "prefix": "speech-mp3", "count_declared": 20,
             "source_url": "https://www.ximalaya.com/jiankang/18584171"},
        ],
    },
]

# ── 已完成的示例条目（展示 published 状态的完整字段）──────────────────────

WORKED_EXAMPLES = [
    {
        "series_id": "renji", "subject_prefix": "zhenjiu", "index": 1,
        "title": "针灸大成 第 1 讲 · 针灸的起源与基本概念",
        "duration_sec": 3420,
        "oss_key": "nhx/renji/zhenjiu/001.mp4",
        "transcript_key": "nhx/renji/zhenjiu/001.transcript.json",
        "status": "published",
        "segments": [
            {"start": 0, "end": 420, "topic": "开场：人纪课程体系介绍"},
            {"start": 420, "end": 1260, "topic": "针灸的历史源流与《针灸大成》地位"},
            {"start": 1260, "end": 2100, "topic": "经络系统总论：十二正经与奇经八脉"},
            {"start": 2100, "end": 3420, "topic": "气血运行与针灸治病的原理"},
        ],
    },
    {
        "series_id": "renji", "subject_prefix": "zhenjiu", "index": 2,
        "title": "针灸大成 第 2 讲 · 十二经络循行",
        "duration_sec": 3180,
        "oss_key": "nhx/renji/zhenjiu/002.mp4",
        "transcript_key": "nhx/renji/zhenjiu/002.transcript.json",
        "status": "published",
        "segments": [
            {"start": 0, "end": 600, "topic": "复习：经络的基本概念"},
            {"start": 600, "end": 1800, "topic": "手三阴经循行路线"},
            {"start": 1800, "end": 3180, "topic": "手三阳经循行路线"},
        ],
    },
    {
        "series_id": "renji", "subject_prefix": "zhenjiu", "index": 3,
        "title": "针灸大成 第 3 讲 · 足三里与常用要穴",
        "duration_sec": 3600,
        "oss_key": "nhx/renji/zhenjiu/003.mp4",
        "transcript_key": "nhx/renji/zhenjiu/003.transcript.json",
        "status": "indexed",
        "segments": [
            {"start": 0, "end": 480, "topic": "足三里定位与取穴"},
            {"start": 480, "end": 1200, "topic": "捻转补泻手法"},
            {"start": 1200, "end": 2100, "topic": "足三里主治与配穴"},
            {"start": 2100, "end": 3600, "topic": "其他常用要穴概览"},
        ],
    },
    {
        "series_id": "tianji", "subject_prefix": "tianji-full", "index": 1,
        "title": "天纪 第 1 集 · 易经与术数总论",
        "duration_sec": 2700,
        "oss_key": "nhx/tianji/tianji-full/001.mp4",
        "status": "draft",
        "note": "示例：待转录。缺少 transcript_key 会有警告。",
        "segments": [
            {"start": 0, "end": 900, "topic": "天纪课程导论"},
            {"start": 900, "end": 2700, "topic": "易经的基本框架"},
        ],
    },
]


def _count_for(entry):
    """以实测为准；无实测则用声明值，并标注待验证。"""
    if entry.get("count_measured"):
        return entry["count_measured"], entry.get("count_declared"), None
    c = entry.get("count_declared") or 0
    return c, c, "集数未验证，采用站方声明值"


def build_series_doc(spec):
    series_meta = {
        "id": spec["series_id"],
        "name": spec["name"],
        "kind": spec["kind"],
        "description": spec.get("description", ""),
    }
    items = []

    units = spec.get("subjects") or spec.get("albums") or []
    for unit in units:
        prefix = unit["prefix"]
        count, declared, caveat = _count_for(unit)
        notes = [unit.get("note")] if unit.get("note") else []
        if caveat:
            notes.append(caveat)

        for n in range(1, count + 1):
            item = {
                "id": "%s-%s-%03d" % (spec["series_id"], prefix, n),
                "title": "%s 第 %d %s" % (
                    unit["subject"], n, "讲" if spec["kind"] == "video" else "集"),
                "subject": unit["subject"],
                "source_type": unit.get("source_type", "ximalaya"),
                "source_url": unit.get("source_url"),
                "oss_key": None,
                "duration_sec": None,
                "transcript_key": None,
                "segments": [],
                "status": "draft",
                "license_status": "主张公开",
                "fetched_at": FETCHED_AT,
                "notes": "；".join(notes) if notes else "",
            }
            items.append(item)

    # 用已完成的示例条目覆盖对应位置
    for ex in WORKED_EXAMPLES:
        if ex["series_id"] != spec["series_id"]:
            continue
        target_id = "%s-%s-%03d" % (spec["series_id"], ex["subject_prefix"], ex["index"])
        for i, item in enumerate(items):
            if item["id"] == target_id:
                merged = dict(item)
                merged.update({k: v for k, v in ex.items()
                               if k not in ("series_id", "subject_prefix", "index")})
                merged["source_type"] = item["source_type"]
                merged["source_url"] = item["source_url"]
                merged["subject"] = item["subject"]
                items[i] = merged
                break

    return {"version": 1, "series": series_meta, "items": items}


def main() -> int:
    force = "--force" in sys.argv
    ensure_dirs()

    existing = {s["series_id"] for s in list_series()}
    created = 0

    for spec in SERIES:
        if spec["series_id"] in existing and not force:
            print("跳过（已存在）：%s" % spec["series_id"])
            continue
        doc = build_series_doc(spec)
        path = save_series(spec["kind"], spec["series_id"], doc)
        created += 1
        print("已初始化：%-38s %3d 条  → %s"
              % (spec["name"], len(doc["items"]), os.path.relpath(path, os.path.dirname(os.path.dirname(
                  os.path.dirname(os.path.abspath(__file__)))))))

    print("\n完成：新建 %d 个系列" % created)
    print("提示：多为 draft 状态，需补齐 oss_key / duration_sec / segments / transcript 后方可上线。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
