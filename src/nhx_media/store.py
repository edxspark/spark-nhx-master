"""倪师 AI · 媒体资源数据层

管理 `media/{视频索引,音频索引}/` 下的系列索引文件，提供：
  - 增删改查（CRUD）
  - 上线状态流转（draft → indexed → published → offline）
  - 章节（segments）编辑
  - 转录回流登记
  - 合规与完整性校验
  - 统计与 OSS 成本估算

设计要点
--------
1. **索引在 Git，媒体本体在 OSS** —— 本模块只写 JSON 索引，不搬运媒体文件。
2. **零依赖** —— 仅 Python 标准库，可直接运行。
3. **原子写入** —— 先写临时文件再 rename，避免写坏索引。
4. **下架能力** —— 每条记录保留 source_url 与 license_status，支持按来源批量下架。

索引文件格式
------------
    media/视频索引/<series>.json
    {
      "version": 1,
      "series": {"id": "renji", "name": "人纪", "kind": "video", "description": "..."},
      "items": [ {item}, ... ]
    }
"""

import json
import os
import re
import shutil
import tempfile
import time
from typing import Any, Dict, List, Optional

# ── 常量 ────────────────────────────────────────────────────────────────────

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
MEDIA_ROOT = os.path.join(REPO_ROOT, "media")

KIND_DIRS = {"video": "视频索引", "audio": "音频索引"}

# 上线状态流转
STATUSES = ["draft", "indexed", "published", "offline"]
STATUS_LABELS = {
    "draft": "草稿",
    "indexed": "已登记",
    "published": "已上线",
    "offline": "已下架",
}

LICENSE_STATUSES = ["公有领域", "主张公开", "已授权", "合理使用", "待授权", "禁止使用"]

SOURCE_TYPES = ["bilibili", "ximalaya", "local", "oss", "other"]

# OSS 成本估算参数（可按实际供应商调整）
OSS_STORAGE_CNY_PER_GB_MONTH = 0.12      # 标准存储
OSS_INFREQUENT_CNY_PER_GB_MONTH = 0.08   # 低频访问
OSS_CDN_CNY_PER_GB = 0.20                # CDN 流量

# 码率档位（字节/秒）。用于缺失实际体积时按时长估算。
# 校验依据：人纪视频约 483 集 × 平均 50 分钟，按 720p 档估算约 271 GB，
# 与调研中站方自述的 405 GB 全集量级相符 → 该档位是合理基准。
BITRATE_PRESETS_BYTES_PER_SEC = {
    "audio_128k": 16000,      # 128 kbps 纯音频
    "360p": 62500,            # ~0.5 Mbps
    "720p": 187500,           # ~1.5 Mbps  ← 默认基准
    "1080p": 312500,          # ~2.5 Mbps
    "hd_original": 625000,    # ~5 Mbps 原盘/高清
}
DEFAULT_BITRATE = "720p"

# 缺失时长时按此估算（秒）。50 分钟是课程视频的常见单集长度。
DEFAULT_EPISODE_SEC = 3000

_ID_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


# ── 工具 ────────────────────────────────────────────────────────────────────

def _atomic_write_json(path: str, data: Any) -> None:
    """原子写入 JSON，避免中断导致索引损坏。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def kind_dir(kind: str) -> str:
    if kind not in KIND_DIRS:
        raise ValueError("未知媒体类型：%r（可选 %s）" % (kind, list(KIND_DIRS)))
    return os.path.join(MEDIA_ROOT, KIND_DIRS[kind])


def series_path(kind: str, series_id: str) -> str:
    return os.path.join(kind_dir(kind), "%s.json" % series_id)


def _safe_series_id(series_id: str) -> str:
    """防目录穿越：series id 必须是 kebab-case。"""
    if not series_id or not _ID_RE.match(series_id):
        raise ValueError("series id 必须是 kebab-case：%r" % series_id)
    return series_id


# ── 读取 ────────────────────────────────────────────────────────────────────

def list_series(kind: Optional[str] = None) -> List[Dict[str, Any]]:
    """列出全部系列（含统计）。"""
    kinds = [kind] if kind else list(KIND_DIRS.keys())
    out: List[Dict[str, Any]] = []

    for k in kinds:
        d = kind_dir(k)
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".json") or fn.startswith("_"):
                continue
            path = os.path.join(d, fn)
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, json.JSONDecodeError):
                continue
            s = data.get("series") or {}
            items = data.get("items") or []
            out.append({
                "kind": k,
                "series_id": s.get("id") or fn[:-5],
                "name": s.get("name") or fn[:-5],
                "description": s.get("description", ""),
                "item_count": len(items),
                "total_duration_sec": sum(int(i.get("duration_sec") or 0) for i in items),
                "with_oss": sum(1 for i in items if i.get("oss_key")),
                "with_transcript": sum(1 for i in items if i.get("transcript_key")),
                "published": sum(1 for i in items if i.get("status") == "published"),
                "path": os.path.relpath(path, REPO_ROOT),
            })
    return out


def load_series(kind: str, series_id: str) -> Dict[str, Any]:
    path = series_path(kind, _safe_series_id(series_id))
    if not os.path.exists(path):
        raise FileNotFoundError("系列不存在：%s/%s" % (kind, series_id))
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def save_series(kind: str, series_id: str, data: Dict[str, Any]) -> str:
    path = series_path(kind, _safe_series_id(series_id))
    data.setdefault("version", 1)
    data.setdefault("series", {})
    data["series"]["id"] = series_id
    data["series"].setdefault("kind", kind)
    data.setdefault("items", [])
    data["updated_at"] = _now()
    _atomic_write_json(path, data)
    return path


def list_items(
    kind: Optional[str] = None,
    series_id: Optional[str] = None,
    status: Optional[str] = None,
    query: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """列出条目，支持按类型/系列/状态/关键词筛选。"""
    out: List[Dict[str, Any]] = []
    for s in list_series(kind):
        if series_id and s["series_id"] != series_id:
            continue
        data = load_series(s["kind"], s["series_id"])
        for item in data.get("items", []):
            if status and item.get("status") != status:
                continue
            if query:
                hay = " ".join(str(item.get(f, "")) for f in
                               ("id", "title", "subject", "topic", "tags"))
                if query.lower() not in hay.lower():
                    continue
            item = dict(item)
            item["_kind"] = s["kind"]
            item["_series_id"] = s["series_id"]
            item["_series_name"] = s["name"]
            out.append(item)
    return out


def find_item(item_id: str) -> Dict[str, Any]:
    """按条目 id 定位，返回 (kind, series_id, item)。"""
    for s in list_series():
        data = load_series(s["kind"], s["series_id"])
        for item in data.get("items", []):
            if item.get("id") == item_id:
                return {"kind": s["kind"], "series_id": s["series_id"], "item": item}
    raise FileNotFoundError("条目不存在：%s" % item_id)


# ── 写入 ────────────────────────────────────────────────────────────────────

def upsert_item(kind: str, series_id: str, item: Dict[str, Any]) -> Dict[str, Any]:
    """新增或更新一个条目。"""
    if not item.get("id"):
        raise ValueError("条目必须有 id")
    if not _ID_RE.match(item["id"]):
        raise ValueError("条目 id 必须是 kebab-case：%r" % item["id"])

    data = load_series(kind, series_id)
    items = data.setdefault("items", [])

    item.setdefault("status", "draft")
    item.setdefault("license_status", "主张公开")
    item.setdefault("segments", [])
    item.setdefault("source_type", "other")
    item.setdefault("fetched_at", time.strftime("%Y-%m-%d"))
    item["updated_at"] = _now()

    for i, existing in enumerate(items):
        if existing.get("id") == item["id"]:
            merged = dict(existing)
            merged.update(item)
            items[i] = merged
            save_series(kind, series_id, data)
            return merged

    items.append(item)
    save_series(kind, series_id, data)
    return item


def delete_item(item_id: str) -> Dict[str, Any]:
    """删除条目。"""
    found = find_item(item_id)
    kind, series_id = found["kind"], found["series_id"]
    data = load_series(kind, series_id)
    data["items"] = [i for i in data.get("items", []) if i.get("id") != item_id]
    save_series(kind, series_id, data)
    return {"deleted": item_id, "series": series_id}


def _segment_problems(segments: List[Dict[str, Any]],
                      duration: Optional[int]) -> List[str]:
    """章节合法性检查（写入时与校验时共用，避免两处规则漂移）。"""
    problems: List[str] = []
    prev_end = -1
    for idx, seg in enumerate(segments):
        try:
            s = int(seg.get("start"))
            e = int(seg.get("end"))
        except (TypeError, ValueError):
            problems.append("segments[%d] 的 start/end 必须是整数" % idx)
            continue
        if e <= s:
            problems.append("segments[%d] 结束时间必须大于开始时间（%s → %s）" % (idx, s, e))
        if s < prev_end:
            problems.append("segments[%d] 与上一章节重叠（起点 %s < 上一段结束 %s）"
                            % (idx, s, prev_end))
        if duration and e > duration:
            problems.append("segments[%d] 超出时长（%s > %s）" % (idx, e, duration))
        prev_end = max(prev_end, e)
    return problems


def set_status(item_id: str, status: str) -> Dict[str, Any]:
    """流转上线状态。上线前按**目标状态**校验，不完整则拒绝。"""
    if status not in STATUSES:
        raise ValueError("未知状态：%r（可选 %s）" % (status, STATUSES))
    found = find_item(item_id)
    kind, series_id = found["kind"], found["series_id"]
    data = load_series(kind, series_id)

    for item in data["items"]:
        if item.get("id") != item_id:
            continue

        if status == "published":
            # 关键：在目标状态上校验，而不是当前状态——
            # 否则 published 专属规则永远不会触发。
            candidate = dict(item)
            candidate["status"] = "published"
            problems = [f for f in validate_item(candidate) if f["level"] == "error"]
            if problems:
                raise ValueError(
                    "无法上线，存在 %d 项阻塞问题：%s"
                    % (len(problems), "；".join(p["message"] for p in problems[:3]))
                )

        item["status"] = status
        item["updated_at"] = _now()
        save_series(kind, series_id, data)
        return item

    raise FileNotFoundError("条目不存在：%s" % item_id)


def set_segments(item_id: str, segments: List[Dict[str, Any]]) -> Dict[str, Any]:
    """整体替换章节标注。写入即校验（重叠 / 越界 / 顺序）。"""
    cleaned = []
    for seg in segments:
        try:
            start = int(seg.get("start") or 0)
            end = int(seg.get("end") or 0)
        except (TypeError, ValueError):
            raise ValueError("章节 start/end 必须是整数：%r" % seg)
        topic = (seg.get("topic") or "").strip()
        cleaned.append({"start": start, "end": end, "topic": topic})
    cleaned.sort(key=lambda s: s["start"])

    found = find_item(item_id)
    kind, series_id = found["kind"], found["series_id"]
    duration = found["item"].get("duration_sec")

    problems = _segment_problems(cleaned, duration)
    if problems:
        raise ValueError("章节不合法：%s" % "；".join(problems))

    data = load_series(kind, series_id)
    for item in data["items"]:
        if item.get("id") == item_id:
            item["segments"] = cleaned
            item["updated_at"] = _now()
            save_series(kind, series_id, data)
            return {"id": item_id, "segments": cleaned}
    raise FileNotFoundError("条目不存在：%s" % item_id)


def bulk_offline(license_status: Optional[str] = None,
                 source_type: Optional[str] = None) -> Dict[str, Any]:
    """批量下架——合规应急能力。

    按授权状态或来源类型批量把条目置为 offline。
    """
    if not license_status and not source_type:
        raise ValueError("必须指定 license_status 或 source_type，避免误伤全库")

    changed: List[str] = []
    for s in list_series():
        data = load_series(s["kind"], s["series_id"])
        dirty = False
        for item in data.get("items", []):
            if license_status and item.get("license_status") != license_status:
                continue
            if source_type and item.get("source_type") != source_type:
                continue
            if item.get("status") != "offline":
                item["status"] = "offline"
                item["updated_at"] = _now()
                changed.append(item["id"])
                dirty = True
        if dirty:
            save_series(s["kind"], s["series_id"], data)
    return {"offlined": changed, "count": len(changed)}


# ── 校验 ────────────────────────────────────────────────────────────────────

def validate_item(item: Dict[str, Any]) -> List[Dict[str, str]]:
    """单条校验。返回 findings 列表（level: error | warning）。"""
    findings: List[Dict[str, str]] = []
    iid = item.get("id", "?")

    def err(msg: str) -> None:
        findings.append({"level": "error", "item_id": iid, "message": msg})

    def warn(msg: str) -> None:
        findings.append({"level": "warning", "item_id": iid, "message": msg})

    # 必填
    for field in ("id", "title", "subject", "source_type", "license_status", "fetched_at"):
        if not item.get(field):
            err("缺少必填字段 `%s`" % field)

    if item.get("license_status") and item["license_status"] not in LICENSE_STATUSES:
        err("`license_status` 取值非法：%r" % item["license_status"])
    if item.get("source_type") and item["source_type"] not in SOURCE_TYPES:
        err("`source_type` 取值非法：%r" % item["source_type"])
    if item.get("status") and item["status"] not in STATUSES:
        err("`status` 取值非法：%r" % item["status"])

    # 溯源：下架能力的前提
    if not item.get("source_url") and not item.get("oss_key"):
        warn("既无 `source_url` 也无 `oss_key`，无法定位来源（影响下架能力）")

    # 验收判据：已上线必须有 ossKey + duration + 至少 1 个 segment
    if item.get("status") == "published":
        if not item.get("oss_key"):
            err("已上线但缺少 `oss_key`（媒体本体必须在 OSS）")
        if not item.get("duration_sec"):
            err("已上线但缺少 `duration_sec`")
        if not item.get("segments"):
            err("已上线但缺少 `segments`（至少 1 个章节标注）")

    # 章节合法性（与写入时共用同一套规则，避免两处漂移）
    segs = item.get("segments") or []
    dur = item.get("duration_sec")
    for msg in _segment_problems(segs, dur):
        err(msg)
    for idx, seg in enumerate(segs):
        if not (seg.get("topic") or "").strip():
            warn("segments[%d] 缺少 topic" % idx)

    # OSS 命名规范
    key = item.get("oss_key")
    if key and not key.startswith("nhx/"):
        warn("`oss_key` 未遵循 `nhx/...` 命名规范：%r" % key)

    # 转录回流
    if item.get("status") == "published" and not item.get("transcript_key"):
        warn("已上线但无 `transcript_key`，视频内容无法被全文检索")

    return findings


def validate_all() -> Dict[str, Any]:
    """全库校验 + 统计。"""
    findings: List[Dict[str, str]] = []
    items = list_items()
    for item in items:
        findings.extend(validate_item(item))

    errors = [f for f in findings if f["level"] == "error"]
    warnings = [f for f in findings if f["level"] == "warning"]

    return {
        "items_checked": len(items),
        "errors": errors,
        "warnings": warnings,
        "passed": not errors,
    }


# ── 统计 ────────────────────────────────────────────────────────────────────

def stats(size_map: Optional[Dict[str, int]] = None,
          bitrate: str = DEFAULT_BITRATE) -> Dict[str, Any]:
    """统计与 OSS 成本估算。

    size_map: {oss_key 或 item_id: 字节数}。有实际体积时优先使用实际值。
    bitrate:  视频的码率档位，见 BITRATE_PRESETS_BYTES_PER_SEC。

    **音频条目一律按 audio_128k 估算**，不套用视频档位——否则音频体积会被严重高估。

    估算口径会明确回报（`estimate` 字段），避免把估算当成实测。
    """
    if bitrate not in BITRATE_PRESETS_BYTES_PER_SEC:
        raise ValueError("未知码率档位：%r（可选 %s）"
                         % (bitrate, sorted(BITRATE_PRESETS_BYTES_PER_SEC)))
    audio_bps = BITRATE_PRESETS_BYTES_PER_SEC["audio_128k"]
    video_bps = BITRATE_PRESETS_BYTES_PER_SEC[bitrate]
    size_map = size_map or {}

    items = list_items()
    total_sec = 0
    total_bytes = 0
    duration_known = 0
    duration_estimated = 0
    bytes_actual = 0
    bytes_estimated = 0
    by_series: Dict[str, Dict[str, Any]] = {}
    by_status: Dict[str, int] = {s: 0 for s in STATUSES}
    by_license: Dict[str, int] = {}
    per_kind_sec: Dict[str, int] = {}

    for item in items:
        kind = item.get("_kind", "video")
        bps = audio_bps if kind == "audio" else video_bps

        sec = int(item.get("duration_sec") or 0)
        if sec:
            duration_known += 1
        else:
            sec = DEFAULT_EPISODE_SEC
            duration_estimated += 1
        total_sec += sec
        per_kind_sec[kind] = per_kind_sec.get(kind, 0) + sec

        key = item.get("oss_key")
        nbytes = None
        if key and key in size_map:
            nbytes = size_map[key]
        elif item["id"] in size_map:
            nbytes = size_map[item["id"]]

        if nbytes is not None:
            bytes_actual += nbytes
        else:
            nbytes = sec * bps
            bytes_estimated += nbytes
        total_bytes += nbytes

        sname = item.get("_series_name", "?")
        bucket = by_series.setdefault(sname, {"count": 0, "duration_sec": 0, "bytes": 0, "kind": kind})
        bucket["count"] += 1
        bucket["duration_sec"] += sec
        bucket["bytes"] += nbytes

        st = item.get("status", "draft")
        by_status[st] = by_status.get(st, 0) + 1
        lic = item.get("license_status", "未标注")
        by_license[lic] = by_license.get(lic, 0) + 1

    gb = total_bytes / (1024 ** 3)

    # 多码率场景对比：仅对视频时长套用视频档位，音频恒为 audio_128k
    video_sec = per_kind_sec.get("video", 0)
    audio_sec = per_kind_sec.get("audio", 0)
    scenarios = {}
    for name, b in BITRATE_PRESETS_BYTES_PER_SEC.items():
        if name == "audio_128k":
            bytes_ = total_sec * audio_bps
        else:
            bytes_ = video_sec * b + audio_sec * audio_bps
        scenarios[name] = round(bytes_ / (1024 ** 3), 2)

    return {
        "total_items": len(items),
        "total_duration_sec": total_sec,
        "total_duration_hours": round(total_sec / 3600.0, 2),
        "estimated_gb": round(gb, 2),
        "by_series": by_series,
        "by_status": by_status,
        "by_license": by_license,
        "duration_by_kind": per_kind_sec,
        "with_oss": sum(1 for i in items if i.get("oss_key")),
        "with_transcript": sum(1 for i in items if i.get("transcript_key")),
        "transcript_coverage": (
            round(sum(1 for i in items if i.get("transcript_key")) / len(items), 3)
            if items else 0.0
        ),
        "estimate": {
            "bitrate_preset": bitrate,
            "bitrate_bytes_per_sec": video_bps,
            "audio_bitrate_bytes_per_sec": audio_bps,
            "duration_known": duration_known,
            "duration_estimated": duration_estimated,
            "default_episode_sec": DEFAULT_EPISODE_SEC,
            "bytes_from_actual": bytes_actual,
            "bytes_from_estimate": bytes_estimated,
            "confidence": (
                "high" if duration_estimated == 0 and bytes_estimated == 0
                else "low" if duration_known == 0
                else "medium"
            ),
            "caveat": (
                "%d / %d 条缺少实际时长，按每集 %d 秒估算；"
                "%d 条缺少实际文件体积，视频按 %s 档、音频按 audio_128k 估算。"
                "补齐实测值后估算才会精确。"
                % (duration_estimated, len(items), DEFAULT_EPISODE_SEC,
                   sum(1 for i in items if not i.get("oss_key")), bitrate)
            ),
        },
        "storage_gb_by_bitrate": scenarios,
        "cost_estimate_cny": {
            "storage_standard_month": round(gb * OSS_STORAGE_CNY_PER_GB_MONTH, 2),
            "storage_infrequent_month": round(gb * OSS_INFREQUENT_CNY_PER_GB_MONTH, 2),
            "cdn_full_play_once_1000": round(gb * 1000 * OSS_CDN_CNY_PER_GB, 2),
            "note": "按 OSS/CDN 单价折算；实际以账单为准。CDN 流量通常是主要成本项。",
        },
    }


def ensure_dirs() -> None:
    for k in KIND_DIRS:
        os.makedirs(kind_dir(k), exist_ok=True)
