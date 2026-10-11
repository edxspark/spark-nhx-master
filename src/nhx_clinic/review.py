"""倪师 AI · 医师审核状态机

实现「AI 预问诊 → 执业医师确认」闭环的状态流转与**强制不变量**。

设计文档：docs/00-产品设计/02-医师闭环设计.md

强制不变量（代码层保证，非约定）
--------------------------------
I1  AI 不能把自己生成的报告置为 reviewed / referred
I2  reviewed / referred 必须带 physician_id 与 license_no
I3  只有 reviewed / referred 可以进入 delivered
I4  每次状态变更都写入审计记录（谁、何时、从什么状态到什么状态、依据）
I5  needs_more 必须带具体问题列表（不能空转）
I6  已交付（delivered）/ 已取消（cancelled）为终态，不可再流转
I7  预问诊报告的 disclaimer 不可为空——交付物必须自带免责说明

零依赖：仅 Python 标准库。
"""

import json
import os
import re
import tempfile
import time
from typing import Any, Dict, List, Optional

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
CLINIC_ROOT = os.path.join(REPO_ROOT, "clinic")
CONSULT_DIR = os.path.join(CLINIC_ROOT, "consultations")

# ── 状态定义 ────────────────────────────────────────────────────────────────

STATES = ["pending", "assigned", "needs_more", "reviewed", "referred", "delivered", "cancelled"]

STATE_LABELS = {
    "pending": "等待接单",
    "assigned": "医师已接单",
    "needs_more": "需补充资料",
    "reviewed": "医师已出意见",
    "referred": "建议线下就诊",
    "delivered": "已交付用户",
    "cancelled": "已取消",
}

TERMINAL_STATES = ["delivered", "cancelled"]

# 流转表：当前状态 → 允许的下一状态
TRANSITIONS: Dict[str, List[str]] = {
    "pending":     ["assigned", "cancelled"],
    "assigned":    ["needs_more", "reviewed", "referred", "cancelled"],
    "needs_more":  ["assigned", "cancelled"],
    "reviewed":    ["delivered"],
    "referred":    ["delivered"],
    "delivered":   [],
    "cancelled":   [],
}

# 需要医师署名的状态
PHYSICIAN_SIGNED_STATES = ["reviewed", "referred"]

ACTOR_AI = "ai"
ACTOR_PHYSICIAN = "physician"
ACTOR_USER = "user"
ACTOR_SYSTEM = "system"

_ID_RE = re.compile(r"^c-\d{8}-\d{4}$")


class InvalidTransition(Exception):
    """非法状态流转。"""


class InvariantViolation(Exception):
    """违反强制不变量。"""


# ── 存储 ────────────────────────────────────────────────────────────────────

def _ensure_dirs() -> None:
    os.makedirs(CONSULT_DIR, exist_ok=True)


def _atomic_write_json(path: str, data: Any) -> None:
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


def _path(consultation_id: str) -> str:
    if not _ID_RE.match(consultation_id or ""):
        raise ValueError("consultation_id 格式非法（期望 c-YYYYMMDD-NNNN）：%r" % consultation_id)
    return os.path.join(CONSULT_DIR, "%s.json" % consultation_id)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def next_id(date_str: Optional[str] = None) -> str:
    """生成下一个预问诊编号。"""
    _ensure_dirs()
    day = date_str or time.strftime("%Y%m%d")
    nums = []
    prefix = "c-%s-" % day
    for fn in os.listdir(CONSULT_DIR):
        if fn.startswith(prefix) and fn.endswith(".json"):
            try:
                nums.append(int(fn[len(prefix):-5]))
            except ValueError:
                continue
    return "%s%04d" % (prefix, (max(nums) + 1) if nums else 1)


def load(consultation_id: str) -> Dict[str, Any]:
    path = _path(consultation_id)
    if not os.path.exists(path):
        raise FileNotFoundError("预问诊记录不存在：%s" % consultation_id)
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def save(doc: Dict[str, Any]) -> str:
    path = _path(doc["consultation_id"])
    _atomic_write_json(path, doc)
    return path


def list_consultations(state: Optional[str] = None,
                       physician_id: Optional[str] = None) -> List[Dict[str, Any]]:
    _ensure_dirs()
    out = []
    for fn in sorted(os.listdir(CONSULT_DIR)):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(CONSULT_DIR, fn), "r", encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        if state and doc.get("state") != state:
            continue
        phys = doc.get("physician") or {}
        if physician_id and phys.get("physician_id") != physician_id:
            continue
        out.append(doc)
    return out


# ── 创建 ────────────────────────────────────────────────────────────────────

def create_consultation(report: Dict[str, Any],
                        user_ref: str,
                        consultation_id: Optional[str] = None) -> Dict[str, Any]:
    """创建预问诊记录。报告由 AI 生成，初始状态 pending。"""
    _ensure_dirs()

    # I7：交付物必须自带免责说明
    if not (report or {}).get("disclaimer", "").strip():
        raise InvariantViolation(
            "I7：预问诊报告必须包含非空 `disclaimer`——"
            "交付物必须明确标注「AI 整理，未经医师确认」"
        )
    if (report or {}).get("review_status") not in (None, "unreviewed"):
        raise InvariantViolation("AI 生成的报告 `review_status` 只能是 unreviewed")

    cid = consultation_id or next_id()
    now = _now()

    report = dict(report)
    report["generated_by"] = ACTOR_AI
    report["review_status"] = "unreviewed"

    doc = {
        "consultation_id": cid,
        "state": "pending",
        "user_ref": user_ref,
        "created_at": now,
        "updated_at": now,
        "report": report,
        "physician": None,
        "opinion": None,
        "pending_questions": [],
        "referral": None,
        "delivered_at": None,
        "audit": [{
            "at": now, "actor": ACTOR_AI, "action": "create",
            "from": None, "to": "pending", "detail": "AI 生成预问诊报告",
        }],
    }
    save(doc)
    return doc


# ── 流转 ────────────────────────────────────────────────────────────────────

def _transition(doc: Dict[str, Any], to_state: str, actor: str,
                action: str, detail: str = "",
                physician: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """统一流转入口：校验状态合法性 + 角色权限 + 不变量，并写审计。"""
    cur = doc["state"]

    # I6：终态不可再流转
    if cur in TERMINAL_STATES:
        raise InvalidTransition("I6：%s 是终态，不可再流转（试图 → %s）" % (cur, to_state))

    allowed = TRANSITIONS.get(cur, [])
    if to_state not in allowed:
        raise InvalidTransition(
            "非法流转：%s → %s（允许：%s）" % (cur, to_state, allowed or "无")
        )

    # I1：AI 不能自己确认
    if to_state in PHYSICIAN_SIGNED_STATES and actor != ACTOR_PHYSICIAN:
        raise InvariantViolation(
            "I1：只有执业医师可以置为 `%s`，收到 actor=%r" % (to_state, actor)
        )

    # I2：医师署名状态必须有资质信息
    if to_state in PHYSICIAN_SIGNED_STATES:
        if not physician or not physician.get("physician_id") or not physician.get("license_no"):
            raise InvariantViolation(
                "I2：`%s` 必须带 physician_id 与 license_no" % to_state
            )

    # 只要提供了医师信息就落盘（assign 也要记，否则「谁在处理」会丢失）。
    # 署名状态下用新值覆盖；非署名状态下只在原值为空时填充，
    # 避免 needs_more → assigned 时把已登记的 license_no 抹掉。
    if physician:
        existing = doc.get("physician") or {}
        if to_state in PHYSICIAN_SIGNED_STATES:
            doc["physician"] = dict(physician)
        else:
            merged = dict(existing)
            merged.update({k: v for k, v in physician.items() if v is not None})
            doc["physician"] = merged

    now = _now()
    doc["state"] = to_state
    doc["updated_at"] = now
    doc["audit"].append({
        "at": now, "actor": actor, "action": action,
        "from": cur, "to": to_state, "detail": detail,
        "physician_id": (physician or {}).get("physician_id"),
    })
    return doc


def assign(consultation_id: str, physician_id: str) -> Dict[str, Any]:
    """医师接单。"""
    doc = load(consultation_id)
    doc = _transition(doc, "assigned", ACTOR_PHYSICIAN, "assign",
                      detail="医师接单", physician={"physician_id": physician_id})
    save(doc)
    return doc


def request_more(consultation_id: str, physician_id: str,
                 questions: List[str]) -> Dict[str, Any]:
    """医师要求补充资料。"""
    # I5：不能空转
    qs = [q.strip() for q in (questions or []) if q and q.strip()]
    if not qs:
        raise InvariantViolation("I5：`needs_more` 必须带具体问题列表，不能为空")

    doc = load(consultation_id)
    doc = _transition(doc, "needs_more", ACTOR_PHYSICIAN, "request_more",
                      detail="；".join(qs), physician={"physician_id": physician_id})
    doc["pending_questions"] = qs
    save(doc)
    return doc


def submit_opinion(consultation_id: str, physician_id: str, license_no: str,
                   opinion: str, diagnosis_level: str = "参考意见") -> Dict[str, Any]:
    """医师出具意见（诊疗主体是医师）。"""
    if not (opinion or "").strip():
        raise ValueError("医师意见不得为空")

    doc = load(consultation_id)
    doc = _transition(doc, "reviewed", ACTOR_PHYSICIAN, "submit_opinion",
                      detail=opinion[:120],
                      physician={"physician_id": physician_id, "license_no": license_no})
    doc["opinion"] = {
        "text": opinion.strip(),
        "diagnosis_level": diagnosis_level,
        "signed_at": _now(),
        "physician_id": physician_id,
        "license_no": license_no,
    }
    doc["pending_questions"] = []
    doc["report"]["review_status"] = "reviewed"
    save(doc)
    return doc


def refer(consultation_id: str, physician_id: str, license_no: str,
          reason: str, department: str = "") -> Dict[str, Any]:
    """建议线下就诊（不开方）。"""
    if not (reason or "").strip():
        raise ValueError("转诊建议必须说明理由")

    doc = load(consultation_id)
    doc = _transition(doc, "referred", ACTOR_PHYSICIAN, "refer",
                      detail=reason[:120],
                      physician={"physician_id": physician_id, "license_no": license_no})
    doc["referral"] = {
        "reason": reason.strip(),
        "department": department.strip(),
        "advised_at": _now(),
        "physician_id": physician_id,
        "license_no": license_no,
    }
    doc["report"]["review_status"] = "referred"
    save(doc)
    return doc


def deliver(consultation_id: str) -> Dict[str, Any]:
    """交付用户。I3：只有 reviewed / referred 可交付。"""
    doc = load(consultation_id)
    cur = doc["state"]
    if cur not in PHYSICIAN_SIGNED_STATES:
        raise InvariantViolation(
            "I3：只有医师已出意见（%s）后才可交付，当前状态为 %s"
            % ("/".join(PHYSICIAN_SIGNED_STATES), cur)
        )
    doc = _transition(doc, "delivered", ACTOR_SYSTEM, "deliver", detail="交付用户")
    doc["delivered_at"] = _now()
    save(doc)
    return doc


def cancel(consultation_id: str, actor: str = ACTOR_USER, reason: str = "") -> Dict[str, Any]:
    doc = load(consultation_id)
    doc = _transition(doc, "cancelled", actor, "cancel", detail=reason)
    save(doc)
    return doc


# ── 交付物组装 ──────────────────────────────────────────────────────────────

def build_deliverable(consultation_id: str) -> Dict[str, Any]:
    """组装给用户看的交付物。

    关键：AI 部分与医师部分**必须可区分**，且未确认时不得呈现为诊疗意见。
    """
    doc = load(consultation_id)
    reviewed = doc["state"] in PHYSICIAN_SIGNED_STATES + ["delivered"]

    return {
        "consultation_id": doc["consultation_id"],
        "state": doc["state"],
        "state_label": STATE_LABELS.get(doc["state"], doc["state"]),
        "sections": [
            {
                "part": "ai_report",
                "title": "预问诊报告（AI 整理）",
                "disclaimer": doc["report"].get("disclaimer"),
                "review_status": doc["report"].get("review_status"),
                "content": doc["report"],
                "attribution": "由 AI 基于倪海厦学术内容与中医经典整理",
                "is_clinical_advice": False,
            },
            {
                "part": "physician_opinion",
                "title": "医师意见",
                "available": reviewed and bool(doc.get("opinion")),
                "content": doc.get("opinion"),
                "attribution": (
                    "执业医师 %s（执业证号 %s）"
                    % (doc["physician"]["physician_id"], doc["physician"].get("license_no", ""))
                    if doc.get("physician") and doc.get("opinion") else None
                ),
                "is_clinical_advice": True,
                "notice": None if reviewed else "医师尚未出具意见，此部分暂不可用。",
            },
            {
                "part": "referral",
                "title": "转诊建议",
                "available": bool(doc.get("referral")),
                "content": doc.get("referral"),
                "attribution": (
                    "执业医师 %s（执业证号 %s）"
                    % (doc["physician"]["physician_id"], doc["physician"].get("license_no", ""))
                    if doc.get("referral") and doc.get("physician") else None
                ),
                "is_clinical_advice": True,
            },
        ],
        "physician_reviewed": reviewed,
        "separation_note": (
            "AI 报告与医师意见为不同主体产出，责任边界不同，"
            "请以医师意见为准。"
        ),
    }
