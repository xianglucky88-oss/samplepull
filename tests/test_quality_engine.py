"""Tests for the pure quality-conclusion engine.

These tests never touch I/O: every guarantee (state classification,
order independence, duplicate idempotence, revision handling, traceability)
is checked against plain notice dictionaries.
"""
import pytest

from samplepull.quality import (
    CONFLICT, INCOMPLETE, PROVISIONAL, PUBLISHED, STATE_LABELS,
    evaluate, evaluate_sample, explain, normalize_notice, RULES,
)


def _n(notice_id, sample, kind, result, source="lab_a", **kw):
    d = {"notice_id": notice_id, "sample_id": sample, "kind": kind,
         "result": result, "source": source,
         "notice_ts": kw.pop("notice_ts", "2030-02-01T00:00:00Z"),
         "received_ts": kw.pop("received_ts", "2030-02-01T00:00:00Z")}
    d.update(kw)
    return d


def _initial(nid, sample, result, **kw):
    return _n(nid, sample, "initial", result, **kw)


def _recheck(nid, sample, result, **kw):
    return _n(nid, sample, "recheck", result, **kw)


def _revoke(nid, sample, target, **kw):
    return _n(nid, sample, "revoke", None, target_notice_id=target, **kw)


# --------------------------------------------------------------------------- #
# The four distinguishable conclusion states.
# --------------------------------------------------------------------------- #
def test_initial_only_is_provisional():
    c = evaluate_sample([_initial("N1", "S1", "PASS")])
    assert c["state"] == PROVISIONAL
    assert c["state_label"] == STATE_LABELS[PROVISIONAL]
    assert c["result"] == "PASS"
    assert "R7" in c["rules_fired"]
    assert c["participating_notice_ids"] == ["N1"]


def test_agreeing_recheck_publishes():
    c = evaluate_sample([_initial("N1", "S2", "PASS"),
                         _recheck("N2", "S2", "PASS", source="lab_b")])
    assert c["state"] == PUBLISHED
    assert c["result"] == "PASS"
    assert "R8" in c["rules_fired"]


def test_disagreeing_recheck_is_conflict():
    c = evaluate_sample([_initial("N1", "S3", "PASS"),
                         _recheck("N2", "S3", "FAIL", source="lab_b")])
    assert c["state"] == CONFLICT
    assert c["result"] is None
    assert set(c["contested_results"]) == {"FAIL", "PASS"}
    assert "R9" in c["rules_fired"]
    assert c["anomalies"]


def test_recheck_without_initial_is_incomplete():
    c = evaluate_sample([_recheck("N1", "S4", "PASS")])
    assert c["state"] == INCOMPLETE
    assert "R10" in c["rules_fired"]
    assert c["result"] is None


def test_no_evidence_is_incomplete():
    c = evaluate_sample([])
    assert c["state"] == INCOMPLETE
    assert "R12" in c["rules_fired"]


def test_same_stage_disagreement_is_conflict():
    c = evaluate_sample([
        _initial("N1", "S5", "PASS", source="lab_a"),
        _initial("N2", "S5", "FAIL", source="lab_c"),
    ])
    assert c["state"] == CONFLICT
    assert "R11" in c["rules_fired"]


# --------------------------------------------------------------------------- #
# Revocations.
# --------------------------------------------------------------------------- #
def test_revoke_unpublishes_back_to_provisional():
    c = evaluate_sample([
        _initial("N1", "S6", "PASS"),
        _recheck("N2", "S6", "PASS", source="lab_b"),
        _revoke("N3", "S6", "N2", source="lab_b"),
    ])
    assert c["state"] == PROVISIONAL
    assert "R4" in c["rules_fired"]
    roles = {e["notice_id"]: e["role"] for e in c["evidence"]}
    assert roles["N2"] == "revoked"
    assert roles["N3"] == "revoke"


def test_revoking_everything_is_incomplete():
    c = evaluate_sample([
        _initial("N1", "S7", "PASS"),
        _revoke("N2", "S7", "N1", source="lab_a"),
    ])
    assert c["state"] == INCOMPLETE
    assert "R6" in c["rules_fired"]


def test_orphan_revoke_is_conflict():
    c = evaluate_sample([
        _initial("N1", "S8", "PASS"),
        _revoke("N2", "S8", "N-GHOST"),
    ])
    assert c["state"] == CONFLICT
    assert "R5" in c["rules_fired"]


def test_duplicate_revoke_same_target_is_idempotent():
    c = evaluate_sample([
        _initial("N1", "S9", "PASS"),
        _revoke("N2", "S9", "N1", source="lab_b", notice_ts="2030-02-01T01:00:00Z"),
        _revoke("N3", "S9", "N1", source="lab_c", notice_ts="2030-02-01T02:00:00Z"),
    ])
    # The initial is gone regardless of which revoke wins -> INCOMPLETE, and
    # the second revoke is recorded as a duplicate rather than a new anomaly.
    assert c["state"] == INCOMPLETE
    assert any(e["role"] == "duplicate_revoke" for e in c["evidence"])
    assert not any(e["role"] == "orphan_revoke" for e in c["evidence"])


# --------------------------------------------------------------------------- #
# Duplicate notices and late revisions.
# --------------------------------------------------------------------------- #
def test_duplicate_notice_from_same_source_participates_once():
    single = evaluate_sample([_initial("N1", "S10", "FAIL")])
    repeated = evaluate_sample([
        _initial("N1", "S10", "FAIL", received_ts="2030-02-01T00:00:00Z"),
        _initial("N1", "S10", "FAIL", received_ts="2030-02-01T03:00:00Z"),
    ])
    assert repeated["state"] == single["state"] == PROVISIONAL
    assert "R2" in repeated["rules_fired"]
    assert repeated["participating_notice_ids"] == ["N1"]
    assert any(e["role"] == "duplicate" for e in repeated["evidence"])


def test_late_revision_supersedes_old_notice():
    c = evaluate_sample([
        _initial("N1", "S11", "PASS", notice_version=1,
                 notice_ts="2030-02-01T00:00:00Z"),
        _initial("N1", "S11", "FAIL", notice_version=2,
                 notice_ts="2030-02-01T05:00:00Z",
                 received_ts="2030-02-01T06:00:00Z"),
    ])
    assert c["state"] == PROVISIONAL
    assert c["result"] == "FAIL"
    assert "R3" in c["rules_fired"]
    roles = {e["notice_id"] + ":" + e["role"] for e in c["evidence"]}
    assert "N1:superseded" in roles and "N1:active" in roles


def test_revision_same_version_breaks_ties_deterministically():
    # Two deliveries of a v1 and v2 revision, interleaved; winner must be v2.
    notices = [
        _initial("N1", "S12", "FAIL", notice_version=2,
                 notice_ts="2030-02-01T09:00:00Z"),
        _initial("N1", "S12", "PASS", notice_version=1,
                 notice_ts="2030-02-01T00:00:00Z"),
    ]
    assert evaluate_sample(notices)["result"] == "FAIL"
    assert evaluate_sample(list(reversed(notices)))["result"] == "FAIL"


# --------------------------------------------------------------------------- #
# Order independence – the headline property.
# --------------------------------------------------------------------------- #
def test_arrival_order_does_not_change_conclusion():
    notices = [
        _initial("N1", "S13", "PASS", notice_ts="2030-02-01T00:00:00Z"),
        _recheck("N2", "S13", "PASS", source="lab_b",
                 notice_ts="2030-02-01T02:00:00Z"),
        _revoke("N3", "S13", "N2", source="lab_b",
                notice_ts="2030-02-01T04:00:00Z"),
        _initial("N4", "S13", "PASS", source="lab_c",
                 notice_ts="2030-02-01T01:00:00Z"),
    ]
    import itertools
    hashes = {evaluate_sample(list(p))["basis_hash"]
              for p in itertools.permutations(notices)}
    assert len(hashes) == 1


def test_duplicate_delivery_anywhere_keeps_basis_hash_stable():
    base = [_initial("N1", "S14", "PASS"),
            _recheck("N2", "S14", "PASS", source="lab_b")]
    h = evaluate_sample(base)["basis_hash"]
    with_dup = base + [dict(base[0])]
    assert evaluate_sample(with_dup)["basis_hash"] == h


# --------------------------------------------------------------------------- #
# Invalid input and aliases.
# --------------------------------------------------------------------------- #
def test_invalid_notices_are_quarantined_and_state_incomplete():
    c = evaluate_sample([
        {"notice_id": "N1", "sample_id": "S15", "kind": "initial",
         "source": "lab_a"},  # missing result
        {"notice_id": "N2", "sample_id": "S15", "kind": "telegram",
         "source": "lab_a", "result": "PASS"},  # unknown kind
    ])
    assert c["state"] == INCOMPLETE
    assert "R1" in c["rules_fired"]
    reasons = {e["notice_id"]: e["invalid_reasons"] for e in c["evidence"]
               if e["role"] == "invalid"}
    assert "result" in reasons["N1"]
    assert "kind" in reasons["N2"]


def test_invalid_revoke_missing_target_does_not_crash():
    c = evaluate_sample([_n("N1", "S16", "revoke", None)])
    assert c["state"] == INCOMPLETE
    assert any(e["role"] == "invalid" for e in c["evidence"])


def test_chinese_and_legacy_kind_aliases_are_accepted():
    c = evaluate_sample([
        {"notice_id": "N1", "sample_id": "S17", "类型" if False else "kind": "初检",
         "result": "PASS", "source": "lab_a"},
        {"notice_id": "N2", "sample_id": "S17", "kind": "复检",
         "result": "PASS", "source": "lab_b"},
    ])
    assert c["state"] == PUBLISHED


def test_legacy_field_aliases_normalize():
    n = normalize_notice({"id": "N9", "sample": "S18", "type": "init",
                          "value": " PASS ", "lab": "lab_x",
                          "ts": "2030-02-01T00:00:00Z"})
    assert n.kind == "initial"
    assert n.sample_id == "S18"
    assert n.source == "lab_x"
    assert n.result == "PASS"
    assert n.notice_id == "N9"


# --------------------------------------------------------------------------- #
# Traceability and explanations.
# --------------------------------------------------------------------------- #
def test_every_fired_rule_exists_in_catalog_with_version():
    c = evaluate_sample([_initial("N1", "S19", "PASS"),
                         _recheck("N2", "S19", "FAIL", source="lab_b")])
    for rid in c["rules_fired"]:
        assert rid in RULES
        assert c["rules_catalog"][rid] == RULES[rid]["version"]


def test_evidence_covers_every_input_notice():
    notices = [
        _initial("N1", "S20", "PASS"),
        _recheck("N2", "S20", "FAIL", source="lab_b"),
        _revoke("N3", "S20", "N1", source="lab_a"),
    ]
    c = evaluate_sample(notices)
    seen = {e["notice_id"] for e in c["evidence"]}
    assert seen == {"N1", "N2", "N3"}
    assert c["notice_count"] == 3


def test_explain_is_chinese_and_mentions_rules_and_notices():
    c = evaluate_sample([_initial("N1", "S21", "PASS")])
    text = explain(c)
    assert "S21" in text and "暂定" in text and "R7" in text and "N1" in text


def test_revoke_arriving_before_target_resolves_after_full_set_known():
    # Out-of-order delivery: revoke is seen first, target second.
    notices = [
        _revoke("N2", "S23", "N1", source="lab_a",
                notice_ts="2030-02-01T03:00:00Z"),
        _initial("N1", "S23", "PASS", notice_ts="2030-02-01T00:00:00Z"),
    ]
    c = evaluate_sample(notices)
    assert c["state"] == INCOMPLETE
    assert "R4" in c["rules_fired"] and "R5" not in c["rules_fired"]
    # Same conclusion regardless of which arrived first.
    assert evaluate_sample(list(reversed(notices)))["basis_hash"] == c["basis_hash"]


def test_evaluate_groups_mixed_stream_by_sample():
    out = evaluate([
        _initial("N1", "SA", "PASS"),
        _initial("N2", "SB", "FAIL"),
        _recheck("N3", "SB", "FAIL", source="lab_b"),
    ])
    assert set(out) == {"SA", "SB"}
    assert out["SA"]["state"] == PROVISIONAL
    assert out["SB"]["state"] == PUBLISHED


def test_basis_hash_stable_for_equivalent_notice_sets():
    a = evaluate_sample([_initial("N1", "S22", "PASS"),
                         _recheck("N2", "S22", "PASS", source="lab_b")])
    b = evaluate_sample([_recheck("N2", "S22", "PASS", source="lab_b"),
                         _initial("N1", "S22", "PASS")])
    assert a["basis_hash"] == b["basis_hash"]
