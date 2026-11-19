"""End-to-end regression scenarios for the quality pipeline.

Covers, against the in-process ASGI fixture:
* source interruption (transient 503 retried; sustained outage recovery)
* out-of-order delivery (shuffled source)
* duplicate re-reports (same source re-sending a notice)
* late revisions (new version of an old notice)
* failure recovery (watermark and published conclusions survive a failed run)
* full vs incremental collection equivalence
* legacy commands, tables and records remaining fully usable
"""
import sqlite3

import pytest

from samplepull.framework import Transport
from samplepull.pipeline import run as legacy_run, run_quality
from samplepull.qpipeline import QUALITY_WATERMARK_KEY
from samplepull.qstore import QualityStore
from samplepull.store import Store
from samplepull.quality import (
    CONFLICT, INCOMPLETE, PROVISIONAL, PUBLISHED,
)


@pytest.fixture()
def con():
    c = sqlite3.connect(":memory:")
    Store(c)
    QualityStore(c)
    yield c
    c.close()


@pytest.fixture()
def transport():
    t = Transport()
    # Every test starts from the deterministic fixture baseline.
    t.request("POST", "/quality/control", json_body={"action": "reset"})
    yield t


def _watermark(con):
    row = con.execute("SELECT v FROM meta WHERE k=?",
                      (QUALITY_WATERMARK_KEY,)).fetchone()
    return row[0] if row else None


# --------------------------------------------------------------------------- #
# Baseline fixture: every conclusion state is reachable.
# --------------------------------------------------------------------------- #
def test_full_run_classifies_all_four_states(con, transport):
    summary = run_quality("full", con=con, transport=transport)
    assert summary["engine"] == "quality"
    qs = QualityStore(con)
    states = {row["sample_id"]: row["state"] for row in qs.all_latest()}
    assert states["Q-S01"] == PROVISIONAL
    assert states["Q-S02"] == PUBLISHED
    assert states["Q-S03"] == CONFLICT
    assert states["Q-S04"] == INCOMPLETE
    # S05's recheck was revoked -> back to provisional.
    assert states["Q-S05"] == PROVISIONAL
    # S06 re-report of N-010 is deduplicated at the store boundary (business
    # identity), so only one copy reaches the engine.  R2 itself is covered by
    # the pure engine tests; here we assert the observable result.
    assert qs.notice_count() == 12
    s06 = qs.latest_conclusion("Q-S06")["basis"]
    assert s06["participating_notice_ids"] == ["N-010"]
    assert s06["state"] == PROVISIONAL
    # S07 late revision v2 overrides v1.
    s07 = qs.latest_conclusion("Q-S07")
    assert s07["basis"]["result"] == "FAIL"
    assert "R3" in s07["basis"]["rules_fired"]


def test_full_run_is_repeatable_and_idempotent(con, transport):
    first = run_quality("full", con=con, transport=transport)
    hashes_before = {r["sample_id"]: r["basis_hash"]
                     for r in QualityStore(con).all_latest()}
    second = run_quality("full", con=con, transport=transport)
    assert second["ingested"] == 0
    hashes_after = {r["sample_id"]: r["basis_hash"]
                    for r in QualityStore(con).all_latest()}
    assert hashes_after == hashes_before
    # No sample gained a new version.
    qs = QualityStore(con)
    assert all(len(qs.conclusion_history(sid)) == 1
               for sid in hashes_before)


# --------------------------------------------------------------------------- #
# Source interruption: transient failures are retried transparently.
# --------------------------------------------------------------------------- #
def test_transient_outage_is_retried_without_data_loss(con, transport):
    transport.request("POST", "/quality/control",
                      json_body={"action": "outage", "count": 2})
    summary = run_quality("full", con=con, transport=transport)
    assert summary["fetched"] == 13  # all seed notices still collected
    assert QualityStore(con).notice_count() == 12  # one is a duplicate


def test_sustained_outage_fails_run_but_keeps_published_state(con, transport):
    # Publish once while the source is healthy.
    run_quality("full", con=con, transport=transport)
    qs = QualityStore(con)
    before = {r["sample_id"]: r["basis_hash"] for r in qs.all_latest()}
    wm_before = _watermark(con)

    # Http retries 5 times (attempts 0..4); force more failures than that.
    transport.request("POST", "/quality/control",
                      json_body={"action": "outage", "count": 50})
    with pytest.raises(RuntimeError, match="quality source unavailable"):
        run_quality("incremental", con=con, transport=transport)

    runs = qs.runs()
    assert runs[-1]["status"] == "failed"
    # Watermark untouched, published conclusions untouched.
    assert _watermark(con) == wm_before
    after = {r["sample_id"]: r["basis_hash"] for r in qs.all_latest()}
    assert after == before

    # Recovery: clear the outage and resume; state converges again.
    transport.request("POST", "/quality/control",
                      json_body={"action": "outage", "count": 0})
    recovered = run_quality("incremental", con=con, transport=transport)
    assert recovered["ingested"] == 0
    assert {r["sample_id"]: r["basis_hash"]
            for r in QualityStore(con).all_latest()} == before


# --------------------------------------------------------------------------- #
# Out-of-order delivery.
# --------------------------------------------------------------------------- #
def test_shuffled_delivery_produces_identical_conclusions(con, transport):
    ordered = run_quality("full", con=con, transport=transport)
    con2 = sqlite3.connect(":memory:")
    Store(con2)
    QualityStore(con2)
    try:
        t2 = Transport()
        t2.request("POST", "/quality/control", json_body={"action": "reset"})
        t2.request("POST", "/quality/control",
                   json_body={"action": "shuffle", "on": True})
        shuffled = run_quality("full", con=con2, transport=t2)
        assert shuffled["ingested"] == ordered["ingested"]
        a = {r["sample_id"]: (r["state"], r["basis_hash"])
             for r in QualityStore(con).all_latest()}
        b = {r["sample_id"]: (r["state"], r["basis_hash"])
             for r in QualityStore(con2).all_latest()}
        assert a == b
    finally:
        con2.close()


def test_late_arriving_revoke_changes_only_its_sample(con, transport):
    run_quality("full", con=con, transport=transport)
    qs = QualityStore(con)
    # Samples not touched by the new revoke must stay byte-for-byte stable.
    stable_before = {sid: qs.latest_conclusion(sid)["basis_hash"]
                     for sid in ("Q-S01", "Q-S03", "Q-S04")}
    # A revoke for Q-S02's recheck arrives long after publication.
    transport.request("POST", "/quality/control", json_body={
        "action": "add",
        "notice": {"notice_id": "N-LATE", "sample_id": "Q-S02",
                   "kind": "revoke", "source": "lab_b",
                   "target_notice_id": "N-003",
                   "notice_ts": "2030-03-01T00:00:00Z",
                   "received_ts": "2030-03-01T00:05:00Z"}})
    run_quality("incremental", con=con, transport=transport)
    # Q-S02 falls back to provisional; other samples' versions are unchanged.
    assert qs.latest_conclusion("Q-S02")["state"] == PROVISIONAL
    assert len(qs.conclusion_history("Q-S02")) == 2
    for sid, old_hash in stable_before.items():
        assert qs.latest_conclusion(sid)["basis_hash"] == old_hash
        assert len(qs.conclusion_history(sid)) == 1


# --------------------------------------------------------------------------- #
# Re-report and revision scenarios.
# --------------------------------------------------------------------------- #
def test_rereport_of_same_source_notice_does_not_flip_conclusion(con, transport):
    run_quality("full", con=con, transport=transport)
    qs = QualityStore(con)
    before = qs.latest_conclusion("Q-S02")
    # Source lab_a re-sends its original initial notice verbatim.
    transport.request("POST", "/quality/control", json_body={
        "action": "add",
        "notice": {"notice_id": "N-002", "sample_id": "Q-S02",
                   "kind": "initial", "result": "PASS", "source": "lab_a",
                   "notice_version": 1,
                   "notice_ts": "2030-02-01T00:02:00Z",
                   "received_ts": "2030-03-02T00:00:00Z"}})
    run_quality("incremental", con=con, transport=transport)
    after = qs.latest_conclusion("Q-S02")
    assert after["version"] == before["version"]
    assert after["state"] == PUBLISHED


def test_late_revision_publishes_new_version_without_touching_history(con, transport):
    # Q-S01 is provisional on a PASS initial.
    run_quality("full", con=con, transport=transport)
    qs = QualityStore(con)
    v1 = qs.latest_conclusion("Q-S01")
    assert v1["state"] == PROVISIONAL and v1["basis"]["result"] == "PASS"
    # A revised initial (v2, FAIL) arrives weeks later.
    transport.request("POST", "/quality/control", json_body={
        "action": "add",
        "notice": {"notice_id": "N-001", "sample_id": "Q-S01",
                   "kind": "initial", "result": "FAIL", "source": "lab_a",
                   "notice_version": 2,
                   "notice_ts": "2030-02-20T00:00:00Z",
                   "received_ts": "2030-02-20T00:01:00Z"}})
    run_quality("incremental", con=con, transport=transport)
    history = qs.conclusion_history("Q-S01")
    assert [h["version"] for h in history] == [1, 2]
    assert history[0]["basis"]["result"] == "PASS"   # immutable history
    assert history[1]["basis"]["result"] == "FAIL"
    assert qs.latest_conclusion("Q-S01")["state"] == PROVISIONAL


# --------------------------------------------------------------------------- #
# Full vs incremental equivalence.
# --------------------------------------------------------------------------- #
def test_incremental_runs_converge_to_full_run_result(con, transport):
    # Reference: one full run on a fresh database.
    ref = run_quality("full", con=con, transport=transport)
    ref_latest = {r["sample_id"]: (r["state"], r["basis_hash"])
                  for r in QualityStore(con).all_latest()}

    # Second database: feed the same notices in three incremental waves by
    # manipulating the fixture, using a second transport over the same app.
    con2 = sqlite3.connect(":memory:")
    Store(con2)
    QualityStore(con2)
    try:
        t2 = Transport()
        t2.request("POST", "/quality/control", json_body={"action": "reset"})
        # Wave 1: only the first third of the seed notices is visible.
        t2.request("POST", "/quality/control", json_body={
            "action": "add_many", "notices": []})  # no-op keeps fixture warm
        # Simulate incremental ingestion by truncating the source between runs.
        from mockapp.main import _quality_items
        full_items = [dict(x) for x in _quality_items]
        _quality_items[:] = full_items[:4]
        run_quality("incremental", con=con2, transport=t2)
        _quality_items[:] = full_items[:8]
        run_quality("incremental", con=con2, transport=t2)
        _quality_items[:] = [dict(x) for x in full_items]
        final = run_quality("incremental", con=con2, transport=t2)

        inc_latest = {r["sample_id"]: (r["state"], r["basis_hash"])
                      for r in QualityStore(con2).all_latest()}
        assert inc_latest == ref_latest
        assert final["notice_count"] == ref["notice_count"]
        # The watermark ends at the greatest receipt timestamp.
        assert _watermark(con2) == _watermark(con)
    finally:
        con2.close()


# --------------------------------------------------------------------------- #
# Failure recovery at the storage boundary.
# --------------------------------------------------------------------------- #
def test_ingest_half_batch_then_resume_is_consistent(con, transport):
    # First run only sees the first page's worth of notices.
    from mockapp.main import _quality_items
    snapshot = [dict(x) for x in _quality_items]
    try:
        _quality_items[:] = snapshot[:3]
        run_quality("full", con=con, transport=transport)
        qs = QualityStore(con)
        partial = qs.notice_count()
        assert partial == 3
        # Source comes back with the complete set.
        _quality_items[:] = [dict(x) for x in snapshot]
        summary = run_quality("full", con=con, transport=transport)
        assert qs.notice_count() == 12
        assert summary["ingested"] == 12 - partial
        states = {r["sample_id"]: r["state"] for r in qs.all_latest()}
        assert states["Q-S02"] == PUBLISHED
    finally:
        _quality_items[:] = [dict(x) for x in snapshot]


# --------------------------------------------------------------------------- #
# Backward compatibility: legacy pipeline, tables and commands still work.
# --------------------------------------------------------------------------- #
def test_legacy_pipeline_and_records_remain_usable(con):
    result = legacy_run(con=con)
    assert result["engine"] == "rpa"
    legacy = Store(con)
    assert legacy.count() == result["count"]
    # Legacy upsert/dedup/watermark semantics unchanged.
    assert legacy.upsert({"id": "legacy-x", "ts": "t9"}) is True
    assert legacy.upsert({"id": "legacy-x", "ts": "t9"}) is False
    legacy.set_watermark("wm-1")
    assert legacy.watermark() == "wm-1"
    # Quality tables coexist in the same database.
    qs = QualityStore(con)
    assert qs.notice_count() == 0
    qs.ingest_notice({"notice_id": "Q1", "sample_id": "QX", "kind": "initial",
                      "result": "PASS", "source": "lab_a",
                      "notice_ts": "2030-02-01T00:00:00Z",
                      "received_ts": "2030-02-01T00:00:00Z"})
    qs.reevaluate_sample("QX")
    assert legacy.count() == result["count"] + 1
    assert qs.latest_conclusion("QX")["state"] == PROVISIONAL


def test_legacy_and_quality_watermarks_are_independent(con, transport):
    legacy = Store(con)
    legacy.set_watermark("legacy-wm")
    run_quality("full", con=con, transport=transport)
    assert legacy.watermark() == "legacy-wm"
    assert _watermark(con) is not None and _watermark(con) != "legacy-wm"
