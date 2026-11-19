"""Persistence tests: immutability, versioning, evidence and run ledger."""
import sqlite3

import pytest

from samplepull.qstore import QualityStore
from samplepull.store import Store
from samplepull.quality import CONFLICT, PROVISIONAL, PUBLISHED, INCOMPLETE


@pytest.fixture()
def qstore():
    con = sqlite3.connect(":memory:")
    Store(con)  # creates legacy tables too
    qs = QualityStore(con)
    yield qs, con
    con.close()


def _initial(nid, sample, result, **kw):
    d = {"notice_id": nid, "sample_id": sample, "kind": "initial",
         "result": result, "source": "lab_a",
         "notice_ts": "2030-02-01T00:00:00Z",
         "received_ts": "2030-02-01T00:00:00Z"}
    d.update(kw)
    return d


def test_notice_ingestion_is_idempotent(qstore):
    qs, _con = qstore
    raw = _initial("N1", "S1", "PASS")
    assert qs.ingest_notice(raw)[0] is True
    assert qs.ingest_notice(dict(raw))[0] is False
    assert qs.notice_count() == 1


def test_revision_with_same_notice_id_is_stored_separately(qstore):
    qs, _con = qstore
    qs.ingest_notice(_initial("N1", "S1", "PASS", notice_version=1))
    qs.ingest_notice(_initial("N1", "S1", "FAIL", notice_version=2,
                              notice_ts="2030-02-01T05:00:00Z",
                              received_ts="2030-02-01T06:00:00Z"))
    assert qs.notice_count() == 2
    outcome = qs.reevaluate_sample("S1")
    assert outcome["state"] == PROVISIONAL
    latest = qs.latest_conclusion("S1")
    assert latest["basis"]["result"] == "FAIL"


def test_conclusion_versions_are_append_only(qstore):
    qs, _con = qstore
    qs.ingest_notice(_initial("N1", "S2", "PASS"))
    first = qs.reevaluate_sample("S2")
    assert first["version"] == 1
    # Re-running without new evidence must not create another version.
    again = qs.reevaluate_sample("S2")
    assert again["action"] == "unchanged" and again["version"] == 1
    # A confirming recheck produces a new immutable version.
    qs.ingest_notice({"notice_id": "N2", "sample_id": "S2", "kind": "recheck",
                      "result": "PASS", "source": "lab_b",
                      "notice_ts": "2030-02-01T02:00:00Z",
                      "received_ts": "2030-02-01T02:00:00Z"})
    published = qs.reevaluate_sample("S2")
    assert published["action"] == "created" and published["version"] == 2
    history = qs.conclusion_history("S2")
    assert [h["version"] for h in history] == [1, 2]
    assert history[0]["is_latest"] is False and history[1]["is_latest"] is True
    assert history[0]["state"] == PROVISIONAL
    assert history[1]["state"] == PUBLISHED


def test_published_conclusion_unchanged_by_unrelated_ingest(qstore):
    qs, _con = qstore
    qs.ingest_notice(_initial("N1", "S3", "PASS"))
    qs.ingest_notice({"notice_id": "N2", "sample_id": "S3", "kind": "recheck",
                      "result": "PASS", "source": "lab_b",
                      "notice_ts": "2030-02-01T02:00:00Z",
                      "received_ts": "2030-02-01T02:00:00Z"})
    qs.reevaluate_sample("S3")
    before = qs.latest_conclusion("S3")
    # A duplicated initial notice changes nothing.
    qs.ingest_notice(_initial("N1", "S3", "PASS",
                              received_ts="2030-02-01T09:00:00Z"))
    qs.reevaluate_sample("S3")
    after = qs.latest_conclusion("S3")
    assert after["version"] == before["version"]
    assert after["basis_hash"] == before["basis_hash"]
    assert after["state"] == PUBLISHED


def test_evidence_links_are_traceable(qstore):
    qs, _con = qstore
    qs.ingest_notice(_initial("N1", "S4", "PASS"))
    qs.ingest_notice({"notice_id": "N2", "sample_id": "S4", "kind": "recheck",
                      "result": "FAIL", "source": "lab_b",
                      "notice_ts": "2030-02-01T02:00:00Z",
                      "received_ts": "2030-02-01T02:00:00Z"})
    qs.reevaluate_sample("S4")
    evidence = qs.evidence_for("S4")
    roles = {e["notice_id"]: e["role"] for e in evidence}
    assert roles == {"N1": "active", "N2": "active"}
    for e in evidence:
        assert len(e["notice_hash"]) == 24


def test_run_ledger_records_success_and_failure(qstore):
    qs, _con = qstore
    rid = qs.begin_run("full")
    qs.finish_run(rid, "ok", 3, 2, {"note": "fine"})
    rid2 = qs.begin_run("incremental")
    qs.finish_run(rid2, "failed", 0, 0, {"error": "boom"})
    runs = qs.runs()
    assert [r["status"] for r in runs] == ["ok", "failed"]
    assert runs[1]["detail"] == {"error": "boom"}


def test_quality_reset_leaves_legacy_records_intact(qstore):
    qs, con = qstore
    legacy = Store(con)
    legacy.upsert({"id": "legacy-1", "ts": "t1", "v": 1})
    qs.ingest_notice(_initial("N1", "S5", "PASS"))
    qs.reset_quality()
    assert qs.notice_count() == 0
    assert legacy.count() == 1
    assert legacy.watermark() is None
