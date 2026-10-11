"""合规校验器的**对抗性测试**：证明闸门真的会拦，而不是只会放行。

做法：在临时目录里构造**故意违规**的数据，确认校验器报错。
不触碰真实知识库。

运行：python3 src/nhx_schema/test_validate.py
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from validate import validate_all  # noqa: E402


def _write_json(root, subdir, name, data):
    d = os.path.join(root, subdir)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, name), "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)


VALID_FORMULA = {
    "id": "test-tang", "name": "测试汤",
    "source": {"book": "伤寒论", "article_no": 1, "license_status": "公有领域",
               "original_text": "太阳之为病，脉浮，头项强痛而恶寒。"},
    "composition": [{"herb": "桂枝", "original_amount": "三两", "role": "君"}],
    "preparation": "以水七升，煮取三升。",
    "indications": ["太阳病"], "contraindications": ["无汗表实"],
}

VALID_HERB = {
    "id": "guizhi", "name": "桂枝",
    "source": {"book": "神农本草经", "grade": "上品", "license_status": "公有领域"},
    "original_text": "牡桂，味辛温。主上气咳逆。",
    "nature_flavor": {"nature": "温", "flavor": "辛甘"},
    "meridians": ["心"], "effects": ["发汗解肌"],
}

TOXIC_HERB_BARE = {
    "id": "fuzi", "name": "附子",
    "source": {"book": "神农本草经", "grade": "下品", "license_status": "公有领域"},
    "original_text": "附子，味辛温。",
    "nature_flavor": {"nature": "大热", "flavor": "辛甘"},
    "meridians": ["心"], "effects": ["回阳救逆"],
}

TOXIC_HERB_GOOD = dict(TOXIC_HERB_BARE)
TOXIC_HERB_GOOD["toxicity"] = {
    "level": "大毒",
    "risk": "乌头碱口服 0.2mg 即可中毒，2–4mg 可致死，可致致命性心律失常。",
    "requires_physician": True,
    "dosage_withheld": True,
}


class ValidatorTestCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="nhx-validate-")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _errors(self):
        findings, _ = validate_all(self.root)
        return [f for f in findings if f.level == "error"]

    # ── 正例：合法数据必须通过 ──────────────────────────────────────────
    def test_valid_data_passes(self):
        _write_json(self.root, "方剂库", "test.json", VALID_FORMULA)
        _write_json(self.root, "本草库", "guizhi.json", VALID_HERB)
        self.assertEqual(self._errors(), [], "合法数据不应报错")

    # ── 反例 1：现代剂量落地（红线 R2）────────────────────────────────
    def test_modern_dose_in_formula_is_rejected(self):
        bad = json.loads(json.dumps(VALID_FORMULA, ensure_ascii=False))
        bad["composition"][0]["original_amount"] = "9g"
        _write_json(self.root, "方剂库", "bad.json", bad)
        errs = self._errors()
        self.assertTrue(errs, "composition 写现代剂量「9g」必须被拦下（红线 R2）")
        self.assertIn("现代剂量单位", errs[0].message)

    def test_modern_dose_variants_are_rejected(self):
        for dose in ["9g", "9克", "500毫克", "10 ml", "0.5mg"]:
            with self.subTest(dose=dose):
                self.root = tempfile.mkdtemp(prefix="nhx-v-")
                bad = json.loads(json.dumps(VALID_FORMULA, ensure_ascii=False))
                bad["composition"][0]["original_amount"] = dose
                _write_json(self.root, "方剂库", "bad.json", bad)
                self.assertTrue(self._errors(), "剂量 %r 必须被拦下" % dose)

    # ── 反例 2：毒性药缺 toxicity 字段（红线 R9）──────────────────────
    def test_toxic_herb_without_toxicity_field_is_rejected(self):
        _write_json(self.root, "本草库", "fuzi.json", TOXIC_HERB_BARE)
        errs = self._errors()
        self.assertTrue(errs, "附子缺 toxicity 字段必须被拦下（红线 R9）")
        self.assertTrue(any("toxicity" in e.message for e in errs))

    def test_toxic_herb_with_toxicity_passes(self):
        _write_json(self.root, "本草库", "fuzi.json", TOXIC_HERB_GOOD)
        self.assertEqual(self._errors(), [], "带完整 toxicity 的毒性药应通过")

    # ── 反例 3：毒性药的 dosage_withheld 被置 false ────────────────────
    def test_toxic_herb_dosage_not_withheld_is_rejected(self):
        bad = json.loads(json.dumps(TOXIC_HERB_GOOD, ensure_ascii=False))
        bad["toxicity"]["dosage_withheld"] = False
        _write_json(self.root, "本草库", "fuzi.json", bad)
        errs = self._errors()
        self.assertTrue(errs, "dosage_withheld=false 必须被拦下（红线 R9）")

    # ── 反例 4：毒性药出现在方中但方剂未声明 dosage_withheld ──────────
    def test_formula_with_toxic_herb_must_withhold_dosage(self):
        bad = json.loads(json.dumps(VALID_FORMULA, ensure_ascii=False))
        bad["composition"] = [{"herb": "附子", "original_amount": "一枚", "role": "君"}]
        _write_json(self.root, "方剂库", "bad.json", bad)
        errs = self._errors()
        self.assertTrue(errs, "方含附子却未声明 dosage_withheld 必须被拦下")

    # ── 反例 5：缺必填字段 ────────────────────────────────────────────
    def test_missing_required_field_is_rejected(self):
        bad = json.loads(json.dumps(VALID_FORMULA, ensure_ascii=False))
        del bad["preparation"]
        _write_json(self.root, "方剂库", "bad.json", bad)
        errs = self._errors()
        self.assertTrue(any("preparation" in e.message for e in errs))

    # ── 反例 6：非法 JSON ─────────────────────────────────────────────
    def test_invalid_json_is_reported(self):
        d = os.path.join(self.root, "方剂库")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "broken.json"), "w", encoding="utf-8") as fh:
            fh.write("{ this is not json ")
        self.assertTrue(self._errors(), "非法 JSON 必须被报出")


if __name__ == "__main__":
    unittest.main(verbosity=2)
