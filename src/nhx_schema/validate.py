"""倪师 AI · 结构化数据合规校验器

用途
----
在入库前对 `knowledge/06-结构化数据/` 下的 JSON 做 Schema 与合规校验，
把「原文原样」「剂量不落地」「毒性药必标注」三条红线**下沉到 CI**，
而不是指望模型每次自觉。

零依赖：仅使用 Python 标准库。

用法
----
    python3 src/nhx_schema/validate.py            # 校验全部
    python3 src/nhx_schema/validate.py --json     # 机器可读输出
"""

import json
import os
import re
import sys
from typing import Any, Dict, List, Tuple

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
_DATA_ROOT = os.path.join(_REPO_ROOT, "knowledge", "06-结构化数据")

# ── 毒性药材清单（窄治疗窗，必须带 toxicity 字段）────────────────────────
# 依据《医疗用毒性药品管理办法》与药典毒性药材分类
TOXIC_HERBS = {
    "附子", "川乌", "草乌", "乌头", "马钱子", "巴豆", "巴豆霜",
    "甘遂", "大戟", "京大戟", "红大戟", "芫花", "商陆", "牵牛子",
    "水蛭", "虻虫", "斑蝥", "青娘子", "红娘子", "蟾酥", "洋金花",
    "天仙子", "生半夏", "生南星", "生白附子", "雄黄", "朱砂", "轻粉",
    "白降丹", "红粉", "硫黄", "罂粟壳", "细辛", "麻黄",
}

# 现代剂量单位——出现即视为「剂量落地」，违反红线 R2
_MODERN_DOSE_UNITS = re.compile(r"\d+(\.\d+)?\s*(mg|毫克|g\b|克|kg|千克|ml|毫升)")

# 古籍剂量单位——允许
_CLASSIC_UNITS = ("两", "斤", "升", "合", "枚", "个", "分", "铢", "把", "片", "寸", "斗", "撮", "茎", "具", "束", "方寸匕", "钱匕", "字")

_FORMULA_REQUIRED = [
    "id", "name", "source", "composition", "preparation",
    "indications", "contraindications",
]
_HERB_REQUIRED = [
    "id", "name", "source", "nature_flavor", "meridians", "effects", "original_text",
]


class Finding:
    def __init__(self, level: str, path: str, message: str) -> None:
        self.level = level      # error | warning
        self.path = path
        self.message = message

    def __str__(self) -> str:
        tag = "❌ ERROR" if self.level == "error" else "⚠️  WARN "
        return "%s  %s\n           %s" % (tag, self.path, self.message)

    def to_dict(self) -> Dict[str, str]:
        return {"level": self.level, "path": self.path, "message": self.message}


def _iter_json(root: str):
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in sorted(filenames):
            if fn.endswith(".json"):
                yield os.path.join(dirpath, fn)


def _load(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _rel(path: str) -> str:
    return os.path.relpath(path, _REPO_ROOT)


def _check_classic_amount(amount: str, path: str, where: str, findings: List[Finding]) -> None:
    """剂量字段必须用古籍单位，不得出现现代单位。"""
    if _MODERN_DOSE_UNITS.search(amount):
        findings.append(Finding(
            "error", path,
            "%s 出现现代剂量单位（%r）。红线 R2：只提供古籍原比例，现代剂量交执业医师。" % (where, amount),
        ))
    if not any(u in amount for u in _CLASSIC_UNITS):
        findings.append(Finding(
            "warning", path,
            "%s 未识别到古籍剂量单位（%r），请确认是否为原文。" % (where, amount),
        ))


def validate_formula(path: str, data: Dict[str, Any], findings: List[Finding]) -> None:
    for key in _FORMULA_REQUIRED:
        if key not in data:
            findings.append(Finding("error", path, "方剂缺少必填字段 `%s`" % key))

    # 出处与原文
    src = data.get("source") or {}
    for key in ("book", "license_status"):
        if not src.get(key):
            findings.append(Finding("error", path, "`source.%s` 缺失" % key))
    if not src.get("original_text"):
        findings.append(Finding("warning", path, "`source.original_text` 为空——原文是红线 R1 的保障"))

    # 组成：必须是古籍剂量
    comp = data.get("composition") or []
    if not comp:
        findings.append(Finding("error", path, "`composition` 为空"))
    for i, item in enumerate(comp):
        where = "composition[%d](%s)" % (i, item.get("herb", "?"))
        amt = item.get("original_amount")
        if not amt:
            findings.append(Finding("error", path, "%s 缺少 `original_amount`" % where))
            continue
        _check_classic_amount(amt, path, where, findings)

        # 毒性药出现在方中 → 方剂须声明 dosage_withheld
        if item.get("herb") in TOXIC_HERBS:
            if not data.get("dosage_withheld"):
                findings.append(Finding(
                    "error", path,
                    "%s 含毒性药「%s」，但方剂未声明 `dosage_withheld: true`（红线 R9）"
                    % (where, item.get("herb")),
                ))

    # 禁忌不得为空
    if not data.get("contraindications"):
        findings.append(Finding("warning", path, "`contraindications` 为空——方剂卡第 6 段需要它"))


def validate_herb(path: str, data: Dict[str, Any], findings: List[Finding]) -> None:
    for key in _HERB_REQUIRED:
        if key not in data:
            findings.append(Finding("error", path, "本草缺少必填字段 `%s`" % key))

    name = data.get("name", "")

    # 毒性药硬闸门
    if name in TOXIC_HERBS:
        tox = data.get("toxicity")
        if not tox:
            findings.append(Finding(
                "error", path,
                "药材「%s」属毒性/窄治疗窗药材，必须提供 `toxicity` 字段（红线 R9）" % name,
            ))
        else:
            for key in ("level", "risk", "requires_physician"):
                if not tox.get(key):
                    findings.append(Finding("error", path, "`toxicity.%s` 缺失（红线 R9）" % key))
            if tox.get("requires_physician") is not True:
                findings.append(Finding("error", path, "`toxicity.requires_physician` 必须为 true"))
            if tox.get("dosage_withheld") is not True:
                findings.append(Finding(
                    "error", path,
                    "毒性药 `toxicity.dosage_withheld` 必须为 true——数据层不提供克数（红线 R9）",
                ))
            risk = tox.get("risk", "")
            if len(risk) < 30:
                findings.append(Finding(
                    "warning", path,
                    "`toxicity.risk` 过短——须写明**具体后果**而非「有毒」这类空话",
                ))

    # 任何文本字段不得出现现代剂量
    for field in ("original_text", "contraindications", "effects"):
        val = data.get(field)
        texts = val if isinstance(val, list) else ([val] if val else [])
        for t in texts:
            if isinstance(t, str) and _MODERN_DOSE_UNITS.search(t):
                findings.append(Finding(
                    "error", path, "`%s` 中出现现代剂量：%r" % (field, t),
                ))


def validate_all(data_root: str = _DATA_ROOT) -> Tuple[List[Finding], int]:
    findings: List[Finding] = []
    count = 0

    for path in _iter_json(data_root):
        rel = _rel(path)
        try:
            data = _load(path)
        except json.JSONDecodeError as exc:
            findings.append(Finding("error", rel, "JSON 解析失败：%s" % exc))
            continue

        count += 1
        if "方剂库" in rel:
            validate_formula(rel, data, findings)
        elif "本草库" in rel:
            validate_herb(rel, data, findings)

    return findings, count


def main() -> int:
    as_json = "--json" in sys.argv
    findings, count = validate_all()

    errors = [f for f in findings if f.level == "error"]
    warnings = [f for f in findings if f.level == "warning"]

    if as_json:
        print(json.dumps({
            "files_checked": count,
            "errors": [f.to_dict() for f in errors],
            "warnings": [f.to_dict() for f in warnings],
            "passed": not errors,
        }, ensure_ascii=False, indent=2))
        return 1 if errors else 0

    print("倪师 AI · 结构化数据合规校验")
    print("=" * 60)
    print("校验文件数：%d" % count)
    print()

    if not findings:
        print("✅ 全部通过")
    else:
        for f in findings:
            print(f)
        print()
        print("=" * 60)
        print("❌ 错误 %d 项，⚠️  警告 %d 项" % (len(errors), len(warnings)))

    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
