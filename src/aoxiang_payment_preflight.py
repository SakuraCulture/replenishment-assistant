"""Read-only local payment eligibility before browser work; never confirms facts."""
import sqlite3
from contextlib import closing
from pathlib import Path

import order_lifecycle as lifecycle
import purchases
from runtime_support import checkpoint


class PaymentPreparationError(ValueError):
    def __init__(self, code, message, details):
        super().__init__(message)
        self.code, self.details = code, details


def _token(doc):
    return {"version": doc["version"], "digest": doc["digest"]} if doc else None


def _version(token):
    return "未留存" if token is None else "V" + str(token["version"])


def _changes(expected, actual):
    return [{"stage": stage, "label": lifecycle.TITLES.get(stage, stage),
             "expected": expected.get(stage), "current": actual.get(stage)}
            for stage in dict.fromkeys([*expected, *actual])
            if expected.get(stage) != actual.get(stage)]


def _change_text(changes):
    return "；".join(c["label"] + " " + _version(c["expected"]) + " → " + _version(c["current"])
                     + ("（内容摘要变化）" if c["expected"] and c["current"] and c["expected"]["version"] == c["current"]["version"] else "")
                     for c in changes)


def require_payment(head, view):
    """Explain the existing lifecycle health predicate; do not broaden eligibility."""
    payment = view["documents"].get("payment")
    if view["health"].get("payment"):
        return payment
    identity = {k: head[k] for k in ("oid", "revision", "warehouse", "number")}
    details = {"head": identity, "payment": _token(payment), "flow_version": view["flow_version"]}
    number = head["number"]
    if not payment:
        raise PaymentPreparationError("PAYMENT_MISSING", number + "：付款单尚未确认，当前签发下尚无付款资料。请到“付款核对”导入或确认本单付款资料，再重建采购预览。", details)
    route = lifecycle.flow.CURRENT if view["flow_version"] == 2 else lifecycle.STAGES
    upstream = route[:route.index("payment")]
    actual = {s: _token(view["documents"].get(s)) for s in upstream}
    legacy = view.get("legacy_return")
    if actual.get("return") is None and legacy and legacy["complete"]:
        actual["return"] = {k: legacy[k] for k in ("version", "digest")}
    expected = payment["payload"].get("basis", {}).get("dependencies", {})
    details["dependency_changes"] = _changes(expected, actual)
    unready = [s for s in upstream if not view["health"].get(s)]
    details["unready_stages"] = unready
    if payment["payload"].get("pending"):
        next_step = "先核对" + "、".join(lifecycle.TITLES[s] for s in unready) + "，再" if unready else ""
        raise PaymentPreparationError("PAYMENT_PENDING", number + "：付款资料已留存V" + str(payment["version"]) + "，但上游未就绪时付款单尚未确认。请" + next_step + "在“付款核对”重新核对本单，再重建采购预览。", details)
    if unready:
        stages = "、".join(lifecycle.TITLES[s] for s in unready)
        changes = _change_text(details["dependency_changes"])
        raise PaymentPreparationError("PAYMENT_UPSTREAM_NOT_READY", number + "：" + stages + "尚未就绪或对照依据已变化" + ("（" + changes + "）" if changes else "") + "。请先核对上述环节，再核对付款单并重建采购预览。", details)
    changes = _change_text(details["dependency_changes"])
    raise PaymentPreparationError("PAYMENT_BASIS_CHANGED", number + "：付款单对照依据已变化" + ("：" + changes if changes else "") + "。请到“付款核对”重新核对本单，再重建采购预览。", details)


def _view(db, path, head, cancel):
    # Decode and verify only lifecycle snapshots through payment. A normal valid
    # payment does not load the much larger signed result or build per-SKU rows.
    route = lifecycle.flow.stages(db, head)
    docs = {s: None for s in route}
    for stage in route[:route.index("payment") + 1]:
        checkpoint(cancel)
        docs[stage] = lifecycle.current(db, head, stage)
    if docs["return"] is None and lifecycle.merchant_feedback.current(db, head["oid"], head["revision"]):
        # Legacy feedback completeness depends on the signed eligible SKU set;
        # use the canonical reader instead of guessing it from payment rows.
        return lifecycle.snapshot(path, head, cancel=cancel, db=db)
    _, _, _, health = lifecycle.progress_state(
        {s: lifecycle.document_summary(d) for s, d in docs.items()},
        operations_required=route == lifecycle.flow.CURRENT)
    return {"documents": docs, "health": health, "legacy_return": None,
            "flow_version": 2 if route == lifecycle.flow.CURRENT else 1}


def preflight_payment(path, head, *, cancel=None, db=None):
    """Read current local facts only; a success is not a durable upload permit."""
    checkpoint(cancel)
    if db is None:
        # mode=ro also prevents a wrong/missing path from creating a fresh DB.
        uri = Path(path).resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=10)) as connection:
            connection.execute("BEGIN")
            return preflight_payment(path, head, cancel=cancel, db=connection)
    if head["revision"] != head["current_revision"]:
        raise ValueError("只能以当前有效签发对应的已确认付款单生成采购文件")
    purchases.validate_head(db, head)
    view = _view(db, path, head, cancel)
    payment = require_payment(head, view)
    checkpoint(cancel)
    return {"head": {k: head[k] for k in ("oid", "revision", "warehouse", "number")},
            "payment": _token(payment), "flow_version": view["flow_version"]}
