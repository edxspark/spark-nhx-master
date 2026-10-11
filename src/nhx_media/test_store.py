"""媒体资源数据层测试。

重点锁定本次开发中实际发现并修复的三个 bug，防止回归：
  BUG-1 上线闸门校验了「当前状态」而非「目标状态」→ published 专属规则永不触发
  BUG-2 set_segments 未校验重叠 / 越界
  BUG-3 音频条目套用了视频码率 → 体积被高估约 12 倍

运行：python3 src/nhx_media/test_store.py
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import store  # noqa: E402


def _item(iid, **kw):
    base = {
        "id": iid,
        "title": "测试条目 %s" % iid,
        "subject": "测试科目",
        "source_type": "bilibili",
        "source_url": "https://example.com/x",
        "license_status": "主张公开",
        "fetched_at": "2025-10-11",
        "status": "draft",
        "segments": [],
        "oss_key": None,
        "duration_sec": None,
        "transcript_key": None,
    }
    base.update(kw)
    return base


class MediaStoreTestCase(unittest.TestCase):
    """所有测试在临时 MEDIA_ROOT 上跑，不触碰真实索引。"""

    def setUp(self):
        self._orig_root = store.MEDIA_ROOT
        self.tmp = tempfile.mkdtemp(prefix="nhx-media-test-")
        store.MEDIA_ROOT = self.tmp
        store.ensure_dirs()
        store.save_series("video", "tests", {
            "series": {"id": "tests", "name": "测试系列", "kind": "video"},
            "items": [],
        })
        store.save_series("audio", "testaudio", {
            "series": {"id": "testaudio", "name": "测试音频", "kind": "audio"},
            "items": [],
        })

    def tearDown(self):
        store.MEDIA_ROOT = self._orig_root
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ── CRUD ────────────────────────────────────────────────────────────
    def test_upsert_and_find(self):
        store.upsert_item("video", "tests", _item("t-001"))
        found = store.find_item("t-001")
        self.assertEqual(found["item"]["id"], "t-001")
        self.assertEqual(found["series_id"], "tests")

    def test_upsert_updates_existing(self):
        store.upsert_item("video", "tests", _item("t-001"))
        store.upsert_item("video", "tests", _item("t-001", title="改过的标题"))
        items = store.list_items(series_id="tests")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "改过的标题")

    def test_delete(self):
        store.upsert_item("video", "tests", _item("t-001"))
        store.delete_item("t-001")
        self.assertEqual(store.list_items(series_id="tests"), [])

    def test_invalid_id_rejected(self):
        for bad in ["Bad_ID", "has space", "../escape", "", "UPPER"]:
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    store.upsert_item("video", "tests", _item(bad))

    def test_series_id_traversal_rejected(self):
        for bad in ["../etc", "a/b", ".."]:
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    store.load_series("video", bad)

    # ── BUG-1：上线闸门 ─────────────────────────────────────────────────
    def test_publish_blocked_when_incomplete(self):
        """BUG-1 回归：不完整条目必须无法上线。"""
        store.upsert_item("video", "tests", _item("t-001"))
        with self.assertRaises(ValueError) as ctx:
            store.set_status("t-001", "published")
        msg = str(ctx.exception)
        self.assertIn("无法上线", msg)
        self.assertIn("oss_key", msg)

    def test_publish_blocked_without_duration(self):
        store.upsert_item("video", "tests", _item(
            "t-001", oss_key="nhx/tests/001.mp4",
            segments=[{"start": 0, "end": 100, "topic": "A"}]))
        with self.assertRaises(ValueError) as ctx:
            store.set_status("t-001", "published")
        self.assertIn("duration_sec", str(ctx.exception))

    def test_publish_blocked_without_segments(self):
        store.upsert_item("video", "tests", _item(
            "t-001", oss_key="nhx/tests/001.mp4", duration_sec=1000))
        with self.assertRaises(ValueError) as ctx:
            store.set_status("t-001", "published")
        self.assertIn("segments", str(ctx.exception))

    def test_publish_succeeds_when_complete(self):
        store.upsert_item("video", "tests", _item(
            "t-001", oss_key="nhx/tests/001.mp4", duration_sec=1000,
            transcript_key="nhx/tests/001.transcript.json",
            segments=[{"start": 0, "end": 500, "topic": "A"}]))
        result = store.set_status("t-001", "published")
        self.assertEqual(result["status"], "published")

    def test_status_not_mutated_on_rejection(self):
        """被拒后状态必须保持原样，不能半途改掉。"""
        store.upsert_item("video", "tests", _item("t-001"))
        with self.assertRaises(ValueError):
            store.set_status("t-001", "published")
        self.assertEqual(store.find_item("t-001")["item"]["status"], "draft")

    # ── BUG-2：章节校验 ─────────────────────────────────────────────────
    def test_overlapping_segments_rejected(self):
        """BUG-2 回归：重叠章节必须被拒。"""
        store.upsert_item("video", "tests", _item("t-001", duration_sec=1000))
        with self.assertRaises(ValueError) as ctx:
            store.set_segments("t-001", [
                {"start": 0, "end": 500, "topic": "A"},
                {"start": 400, "end": 900, "topic": "B"},
            ])
        self.assertIn("重叠", str(ctx.exception))

    def test_segment_beyond_duration_rejected(self):
        """BUG-2 回归：超出时长的章节必须被拒。"""
        store.upsert_item("video", "tests", _item("t-001", duration_sec=1000))
        with self.assertRaises(ValueError) as ctx:
            store.set_segments("t-001", [{"start": 0, "end": 9999, "topic": "A"}])
        self.assertIn("超出时长", str(ctx.exception))

    def test_end_before_start_rejected(self):
        store.upsert_item("video", "tests", _item("t-001", duration_sec=1000))
        with self.assertRaises(ValueError):
            store.set_segments("t-001", [{"start": 500, "end": 100, "topic": "A"}])

    def test_valid_segments_accepted_and_sorted(self):
        store.upsert_item("video", "tests", _item("t-001", duration_sec=1000))
        r = store.set_segments("t-001", [
            {"start": 600, "end": 900, "topic": "B"},
            {"start": 0, "end": 500, "topic": "A"},
        ])
        self.assertEqual([s["start"] for s in r["segments"]], [0, 600])

    def test_no_mutation_on_rejected_segments(self):
        """被拒后原章节必须保持原样。"""
        store.upsert_item("video", "tests", _item(
            "t-001", duration_sec=1000,
            segments=[{"start": 0, "end": 500, "topic": "原始"}]))
        with self.assertRaises(ValueError):
            store.set_segments("t-001", [{"start": 0, "end": 9999, "topic": "坏"}])
        after = store.find_item("t-001")["item"]["segments"]
        self.assertEqual(after[0]["topic"], "原始")

    # ── BUG-3：成本估算 ─────────────────────────────────────────────────
    def test_audio_not_estimated_with_video_bitrate(self):
        """BUG-3 回归：音频必须按 audio_128k 估算，不能套用视频码率。"""
        store.upsert_item("video", "tests", _item(
            "v-001", duration_sec=3600, oss_key="nhx/tests/v001.mp4"))
        store.upsert_item("audio", "testaudio", _item(
            "a-001", duration_sec=3600, source_type="ximalaya"))

        st = store.stats(bitrate="720p")
        v_bytes = st["by_series"]["测试系列"]["bytes"]
        a_bytes = st["by_series"]["测试音频"]["bytes"]

        audio_bps = store.BITRATE_PRESETS_BYTES_PER_SEC["audio_128k"]
        self.assertEqual(a_bytes, 3600 * audio_bps,
                         "音频体积必须用 audio_128k 码率")
        self.assertLess(a_bytes, v_bytes,
                        "同样时长下音频体积必须小于视频")

    def test_actual_size_overrides_estimate(self):
        store.upsert_item("video", "tests", _item(
            "v-001", duration_sec=3600, oss_key="nhx/tests/v001.mp4"))
        st = store.stats(size_map={"nhx/tests/v001.mp4": 123456})
        self.assertEqual(st["estimate"]["bytes_from_actual"], 123456)
        self.assertEqual(st["estimate"]["bytes_from_estimate"], 0)
        self.assertEqual(st["estimate"]["confidence"], "high")

    def test_unknown_bitrate_rejected(self):
        with self.assertRaises(ValueError):
            store.stats(bitrate="8k_nonexistent")

    # ── 批量下架 ────────────────────────────────────────────────────────
    def test_bulk_offline_requires_filter(self):
        with self.assertRaises(ValueError):
            store.bulk_offline()

    def test_bulk_offline_by_license(self):
        store.upsert_item("video", "tests", _item("t-001", license_status="待授权"))
        store.upsert_item("video", "tests", _item("t-002", license_status="主张公开"))
        r = store.bulk_offline(license_status="待授权")
        self.assertEqual(r["count"], 1)
        self.assertEqual(store.find_item("t-001")["item"]["status"], "offline")
        self.assertEqual(store.find_item("t-002")["item"]["status"], "draft")

    def test_bulk_offline_by_source_type(self):
        store.upsert_item("video", "tests", _item("t-001", source_type="bilibili"))
        store.upsert_item("video", "tests", _item("t-002", source_type="local"))
        r = store.bulk_offline(source_type="bilibili")
        self.assertEqual(r["count"], 1)

    # ── 校验 ────────────────────────────────────────────────────────────
    def test_validate_catches_bad_license(self):
        store.upsert_item("video", "tests", _item("t-001", license_status="瞎写的"))
        v = store.validate_all()
        self.assertTrue(any("license_status" in e["message"] for e in v["errors"]))

    def test_validate_warns_on_missing_source(self):
        store.upsert_item("video", "tests", _item("t-001", source_url=None))
        v = store.validate_all()
        self.assertTrue(any("无法定位来源" in w["message"] for w in v["warnings"]))

    def test_validate_passes_on_clean_data(self):
        store.upsert_item("video", "tests", _item(
            "t-001", oss_key="nhx/tests/001.mp4", duration_sec=1000,
            transcript_key="nhx/tests/001.transcript.json",
            segments=[{"start": 0, "end": 900, "topic": "A"}],
            status="published"))
        v = store.validate_all()
        self.assertEqual(v["errors"], [])
        self.assertEqual(v["warnings"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
