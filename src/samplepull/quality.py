"""Quality-conclusion engine for lab samples.

A sample can receive several kinds of quality notices:

* ``initial``  初检通知
* ``recheck``  复检通知
* ``revoke``   撤销通知

The engine turns the *set* of notices ever observed for a sample into a single
conclusion whose state is one of:

* ``PROVISIONAL`` 暂定  – usable evidence exists but the decision is not final
* ``PUBLISHED``   已发布 – the conclusion has been confirmed and is now stable
* ``CONFLICT``    存在冲突 – surviving notices disagree with one another
* ``INCOMPLETE``  缺少关键信息 – required information is missing or unusable

Design properties (all exercised by the regression suite):

* **Order independence** – notices may arrive in any order; the conclusion is a
  pure function of the notice set.
* **Idempotent duplicates** – the same notice repeated from the same source is
  stored/observed many times but only participates once.
* **Deterministic revisions** – a later revision (higher ``notice_version``,
  later ``notice_ts``) supersedes the earlier notice of the same
  source/kind/sample.  Repeated receipt or changed arrival order never flips a
  published conclusion by itself.
* **Explainable** – every conclusion names the participating notices and the
  rule ids (see :data:`RULES`) that produced it.

The module is deliberately free of I/O and clocks so it can be unit-tested in
isolation and replayed deterministically.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable

from .framework import content_hash

# --------------------------------------------------------------------------- #
# Conclusion states.
# --------------------------------------------------------------------------- #
PROVISIONAL = "PROVISIONAL"
PUBLISHED = "PUBLISHED"
CONFLICT = "CONFLICT"
INCOMPLETE = "INCOMPLETE"

STATE_LABELS = {
    PROVISIONAL: "暂定",
    PUBLISHED: "已发布",
    CONFLICT: "存在冲突",
    INCOMPLETE: "缺少关键信息",
}

# Canonical notice kinds plus the Chinese/legacy aliases accepted on input.
KIND_ALIASES = {
    "initial": "initial", "init": "initial", "first": "initial",
    "初检": "initial", "初检通知": "initial", "初次检验": "initial",
    "recheck": "recheck", "review": "recheck", "retest": "recheck",
    "复检": "recheck", "复检通知": "recheck", "复查": "recheck",
    "revoke": "revoke", "withdraw": "revoke", "withdrawal": "revoke",
    "cancel": "revoke", "rescind": "revoke",
    "撤销": "revoke", "撤销通知": "revoke", "撤回": "revoke",
}

REQUIRED_FIELDS = ("sample_id", "source", "kind", "result")

# --------------------------------------------------------------------------- #
# Rule catalog – the version is part of every conclusion's basis, so the rule
# basis behind any historical conclusion remains traceable forever.
# --------------------------------------------------------------------------- #
RULES = {
    "R1": {
        "version": "1.0",
        "name": "invalid_notice",
        "desc": "通知缺少关键字段或结果不可用，标记为无效并要求补齐信息",
    },
    "R2": {
        "version": "1.0",
        "name": "duplicate_notice",
        "desc": "同一通知重复送达（同来源、同通知标识），仅首次参与判断",
    },
    "R3": {
        "version": "1.0",
        "name": "revision_supersede",
        "desc": "同来源同类型通知的较新修订版本取代旧版本",
    },
    "R4": {
        "version": "1.0",
        "name": "revoke_target",
        "desc": "撤销通知作废其明确指向的目标通知",
    },
    "R5": {
        "version": "1.0",
        "name": "revoke_orphan",
        "desc": "撤销通知找不到仍有效的目标通知，记为异常冲突",
    },
    "R6": {
        "version": "1.0",
        "name": "revoke_all",
        "desc": "有效通知全部被撤销，样品缺少可判断信息",
    },
    "R7": {
        "version": "1.0",
        "name": "initial_only",
        "desc": "仅有初检结论，尚未经复检确认，结论暂定",
    },
    "R8": {
        "version": "1.0",
        "name": "recheck_confirm",
        "desc": "复检与初检结果一致，结论发布",
    },
    "R9": {
        "version": "1.0",
        "name": "recheck_disagree",
        "desc": "复检与初检结果不一致，结论存在冲突",
    },
    "R10": {
        "version": "1.0",
        "name": "recheck_without_initial",
        "desc": "只有复检而无有效初检，关键信息不足",
    },
    "R11": {
        "version": "1.0",
        "name": "same_stage_disagree",
        "desc": "同一判断阶段（多个初检或多个复检）来源结果不一致，结论存在冲突",
    },
    "R12": {
        "version": "1.0",
        "name": "no_evidence",
        "desc": "没有任何可用于判断的有效通知，关键信息不足",
    },
}


@dataclass
class Notice:
    """One normalized quality notice."""

    sample_id: str
    source: str
    kind: str
    result: Any
    notice_id: str
    notice_version: int
    notice_ts: str
    received_ts: str
    target_notice_id: str | None
    raw: dict[str, Any]
    invalid_reasons: list[str] = field(default_factory=list)

    @property
    def notice_hash(self) -> str:
        """Hash of the exact raw payload – the immutable storage key."""
        return content_hash(self.raw)

    @property
    def identity_hash(self) -> str:
        """Hash of the business content, stable across re-delivery.

        Transport bookkeeping (``received_ts``) is deliberately excluded: the
        same notice handed to us twice – even with a new receipt time – is one
        and the same notice for identity and decision purposes.
        """
        return content_hash({
            "sample_id": self.sample_id,
            "source": self.source,
            "kind": self.kind,
            "result": self.result,
            "notice_id": self.notice_id,
            "notice_version": self.notice_version,
            "notice_ts": self.notice_ts,
            "target_notice_id": self.target_notice_id,
            "invalid_reasons": sorted(self.invalid_reasons),
        })

    @property
    def is_valid(self) -> bool:
        return not self.invalid_reasons

    def as_dict(self) -> dict[str, Any]:
        return {
            "sample_id": self.sample_id,
            "source": self.source,
            "kind": self.kind,
            "result": self.result,
            "notice_id": self.notice_id,
            "notice_version": self.notice_version,
            "notice_ts": self.notice_ts,
            "received_ts": self.received_ts,
            "target_notice_id": self.target_notice_id,
            "notice_hash": self.notice_hash,
            "identity_hash": self.identity_hash,
            "invalid_reasons": list(self.invalid_reasons),
        }


def _norm_str(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def normalize_notice(raw: dict[str, Any]) -> Notice:
    """Convert a wire-format notice (with legacy aliases) into a Notice."""
    d = dict(raw or {})

    sample_id = _norm_str(d.get("sample_id") or d.get("sample"))
    source = _norm_str(d.get("source") or d.get("lab") or "unknown")
    kind_raw = _norm_str(d.get("kind") or d.get("type") or d.get("notice_type")).lower()
    kind = KIND_ALIASES.get(kind_raw, kind_raw)

    result = d.get("result")
    if result is None:
        result = d.get("value")
    if isinstance(result, str):
        result = result.strip()

    notice_id = _norm_str(d.get("notice_id") or d.get("id"))
    try:
        notice_version = int(d.get("notice_version") or d.get("version") or 1)
    except (TypeError, ValueError):
        notice_version = 1
    notice_ts = _norm_str(d.get("notice_ts") or d.get("ts") or "")
    received_ts = _norm_str(d.get("received_ts") or d.get("ingested_ts") or notice_ts)
    target = d.get("target_notice_id") or d.get("target") or d.get("revokes")
    target_notice_id = _norm_str(target) or None

    reasons: list[str] = []
    if not sample_id:
        reasons.append("sample_id")
    if not source:
        reasons.append("source")
    if kind not in ("initial", "recheck", "revoke"):
        reasons.append("kind")
    # A revoke acts on a target; its own result is not required.
    if kind != "revoke" and (result is None or result == ""):
        reasons.append("result")
    if kind == "revoke" and not target_notice_id:
        reasons.append("target_notice_id")

    if not notice_id:
        # Deterministic synthetic id so identical raw notices stay identical.
        notice_id = "auto-" + content_hash(d)[:16]

    return Notice(
        sample_id=sample_id or notice_id,
        source=source,
        kind=kind,
        result=result,
        notice_id=notice_id,
        notice_version=max(notice_version, 1),
        notice_ts=notice_ts,
        received_ts=received_ts,
        target_notice_id=target_notice_id,
        raw=d,
        invalid_reasons=reasons,
    )


def _rank_key(n: Notice) -> tuple[str, int, str, str]:
    """Total deterministic ordering for supersede/conflict tie breaks."""
    return (n.source, n.notice_version, n.notice_ts, n.notice_id)


def evaluate_sample(notices: Iterable[dict[str, Any] | Notice]) -> dict[str, Any]:
    """Evaluate every notice belonging to one sample.

    Returns a conclusion dict; see the module docstring for the state model.
    """
    normed = [n if isinstance(n, Notice) else normalize_notice(n) for n in notices]
    rules_fired: list[str] = []
    evidence: list[dict[str, Any]] = []
    anomalies: list[str] = []

    def fire(rule_id: str) -> None:
        if rule_id not in rules_fired:
            rules_fired.append(rule_id)

    # -- R1: invalid notices are quarantined but still explainable. ---------
    valid: list[Notice] = []
    for n in normed:
        if not n.is_valid:
            fire("R1")
            anomalies.append("notice %s invalid: missing %s"
                             % (n.notice_id, ",".join(n.invalid_reasons)))
            evidence.append({**n.as_dict(), "role": "invalid"})
        else:
            valid.append(n)

    # -- R2: exact re-reports (same source/kind/notice id/version) ----------
    # participate once.  A re-sent copy at the same version is never a
    # revision: repeated delivery must not, by itself, move a conclusion.
    identity_groups: dict[tuple, list[Notice]] = defaultdict(list)
    for n in valid:
        identity_groups[(n.source, n.kind, n.notice_id,
                         n.notice_version)].append(n)
    by_identity: dict[tuple, Notice] = {}
    for key, members in identity_groups.items():
        members.sort(key=lambda n: _rank_key(n) + (n.identity_hash,))
        winner = members[-1]
        for loser in members[:-1]:
            fire("R2")
            evidence.append({**loser.as_dict(), "role": "duplicate"})
        by_identity[key] = winner

    # -- R3: revisions supersede older versions of the same source/kind. ----
    # The surviving notice is the one with the greatest (version, ts, id);
    # the final notice_hash tie-break makes the choice total and therefore
    # independent of arrival order.
    groups: dict[tuple[str, str], list[Notice]] = defaultdict(list)
    for n in by_identity.values():
        groups[(n.source, n.kind)].append(n)
    voices: dict[str, Notice] = {}  # source|kind -> surviving non-revoke notice
    revokes: list[Notice] = []
    for (_src, kind), members in groups.items():
        members.sort(key=lambda n: _rank_key(n) + (n.identity_hash,))
        winner = members[-1]
        for loser in members[:-1]:
            fire("R3")
            evidence.append({**loser.as_dict(), "role": "superseded"})
        if kind == "revoke":
            revokes.append(winner)
        else:
            voices[winner.source + "|" + kind] = winner

    # Index every usable notice for revoke target resolution.  When several
    # revisions share a notice id, the newest surviving one is the target.
    index: dict[str, Notice] = {}
    for n in sorted(by_identity.values(),
                    key=lambda x: _rank_key(x) + (x.identity_hash,)):
        index[n.notice_id] = n
    revoke_targets: dict[str, list[str]] = defaultdict(list)

    # -- R4/R5: revokes resolve against their targets, deterministically. ---
    for rv in sorted(revokes, key=_rank_key):
        target = index.get(rv.target_notice_id or "")
        if target is None:
            fire("R5")
            anomalies.append("revoke %s targets unknown/absent notice %s"
                             % (rv.notice_id, rv.target_notice_id))
            evidence.append({**rv.as_dict(), "role": "orphan_revoke"})
            continue
        bucket = revoke_targets[target.notice_id]
        if bucket:  # another revoke already voided this target
            fire("R2")
            evidence.append({**rv.as_dict(), "role": "duplicate_revoke"})
            continue
        fire("R4")
        bucket.append(rv.notice_id)
        evidence.append({**rv.as_dict(), "role": "revoke",
                         "target_notice_id": target.notice_id})

    # Apply revocations to the surviving voices.
    alive: dict[str, Notice] = {}
    for key, n in voices.items():
        if n.notice_id in revoke_targets:
            evidence.append({**n.as_dict(), "role": "revoked",
                             "revoked_by": revoke_targets[n.notice_id]})
        else:
            alive[key] = n

    # Every non-revoke voice also appears as participating evidence.
    for n in sorted(alive.values(), key=_rank_key):
        evidence.append({**n.as_dict(), "role": "active"})

    initials = [n for n in alive.values() if n.kind == "initial"]
    rechecks = [n for n in alive.values() if n.kind == "recheck"]
    initials.sort(key=_rank_key)
    rechecks.sort(key=_rank_key)

    # -- R6/R12: nothing left to judge. -------------------------------------
    if not initials and not rechecks:
        if voices:  # voices existed but every one was revoked
            fire("R6")
        else:
            fire("R12")
        state = INCOMPLETE
        result = None
        contested: list[Any] = []
    else:
        result_set = {("" if n.result is None else str(n.result)) for n in initials + rechecks}
        contested = sorted(result_set)

        # -- R11: disagreement within the same judgment stage. --------------
        stage_conflict = (len({str(n.result) for n in initials}) > 1 or
                          len({str(n.result) for n in rechecks}) > 1)
        if stage_conflict:
            fire("R11")
            anomalies.append("multiple %s notices carry different results"
                             % ("initial" if len({str(n.result) for n in initials}) > 1
                                else "recheck"))

        # -- R9: recheck contradicts the initial result. --------------------
        cross_conflict = bool(initials and rechecks and
                              {str(n.result) for n in initials} !=
                              {str(n.result) for n in rechecks})
        if cross_conflict:
            fire("R9")
            anomalies.append("recheck result disagrees with initial result")

        # -- R10: recheck without a surviving initial. ----------------------
        if rechecks and not initials:
            fire("R10")

        # -- R7/R8: the ordinary progression. -------------------------------
        if initials and not rechecks:
            fire("R7")
        if initials and rechecks and not cross_conflict:
            fire("R8")

        if anomalies:
            state = CONFLICT
            result = None
        elif rechecks and not initials:
            state = INCOMPLETE
            result = None
        elif rechecks:
            state = PUBLISHED
            result = rechecks[0].result
        else:
            state = PROVISIONAL
            result = initials[0].result

    sample_id = normed[0].sample_id if normed else ""
    active_fingerprint = sorted(
        (e["identity_hash"], e["role"], "" if e.get("result") is None
         else str(e.get("result")))
        for e in evidence if e["role"] in ("active", "revoke", "orphan_revoke"))
    basis = {
        "sample_id": sample_id,
        "state": state,
        "state_label": STATE_LABELS[state],
        "result": result,
        "contested_results": contested,
        "anomalies": anomalies,
        "rules_fired": rules_fired,
        "rules_catalog": {rid: RULES[rid]["version"] for rid in rules_fired},
        "participating_notice_ids": sorted(
            {e["notice_id"] for e in evidence if e["role"] == "active"}),
        "evidence": sorted(evidence, key=lambda e: (e["source"], e["kind"],
                                                    e["notice_id"], e["role"])),
        "notice_count": len(normed),
    }
    # The decision hash covers only decision-relevant content: re-delivery of
    # an already-known notice (duplicate/supersede bookkeeping, receipt
    # timestamps) does not change it, while a genuine revoke, new evidence or
    # conflict does.  R2/R3 only describe how the notice set was reconciled.
    decision_rules = [r for r in rules_fired if r not in ("R2", "R3")]
    basis["basis_hash"] = content_hash({
        "sample_id": sample_id,
        "state": state,
        "result": result,
        "anomalies": anomalies,
        "rules_fired": decision_rules,
        "active_evidence": active_fingerprint,
    })
    return basis


def evaluate(notices: Iterable[dict[str, Any] | Notice]) -> dict[str, dict[str, Any]]:
    """Evaluate a mixed stream of notices, grouped by sample id."""
    per_sample: dict[str, list[Notice]] = defaultdict(list)
    for raw in notices:
        n = raw if isinstance(raw, Notice) else normalize_notice(raw)
        per_sample[n.sample_id].append(n)
    return {sid: evaluate_sample(batch) for sid, batch in sorted(per_sample.items())}


def explain(conclusion: dict[str, Any]) -> str:
    """One-line human explanation in Chinese, suitable for the quality UI."""
    state = conclusion["state"]
    rules = ",".join("%s(%s/v%s)" % (rid, RULES[rid]["name"], RULES[rid]["version"])
                     for rid in conclusion["rules_fired"]) or "-"
    notices = ",".join(conclusion["participating_notice_ids"]) or "-"
    head = "样品 %s：%s" % (conclusion["sample_id"], conclusion["state_label"])
    if state == PUBLISHED:
        head += "，结论=%s" % conclusion["result"]
    elif state == PROVISIONAL:
        head += "，暂定结果=%s（待复检确认）" % conclusion["result"]
    elif state == CONFLICT:
        head += "，候选结果=%s" % "/".join(conclusion["contested_results"])
    if conclusion["anomalies"]:
        head += "；异常：" + "；".join(conclusion["anomalies"])
    return head + "；依据通知[%s]；规则[%s]" % (notices, rules)
