"""Append-only persistence for the quality domain.

New, independent tables are created alongside the legacy ``records``/``meta``
tables, which are never modified:

* ``quality_notices``  – every raw notice exactly once (content-hash keyed)
* ``quality_conclusions`` – versioned, immutable conclusions per sample
* ``quality_evidence`` – many-to-many link between a conclusion version and
  the notices that participated, each tagged with its role
* ``quality_runs`` – run ledger used to prove failure recovery never leaves
  half-published state behind

A published conclusion row is never updated or deleted: a new evaluation that
changes the outcome gets a new version row and the previous ``is_latest`` flag
is flipped inside the same transaction.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from .framework import content_hash
from .quality import Notice, evaluate_sample, normalize_notice


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class QualityStore:
    def __init__(self, con: sqlite3.Connection):
        self.con = con
        self._init()

    def _init(self) -> None:
        self.con.executescript(
            """
            CREATE TABLE IF NOT EXISTS quality_notices(
                notice_hash   TEXT PRIMARY KEY,
                identity_hash TEXT NOT NULL,
                sample_id     TEXT NOT NULL,
                source        TEXT NOT NULL,
                kind          TEXT NOT NULL,
                notice_id     TEXT NOT NULL,
                notice_ts     TEXT,
                received_ts   TEXT,
                payload       TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS quality_conclusions(
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                sample_id     TEXT NOT NULL,
                version       INTEGER NOT NULL,
                state         TEXT NOT NULL,
                result        TEXT,
                basis_hash    TEXT NOT NULL,
                basis_json    TEXT NOT NULL,
                created_ts    TEXT NOT NULL,
                is_latest     INTEGER NOT NULL DEFAULT 1,
                UNIQUE(sample_id, version)
            );
            CREATE TABLE IF NOT EXISTS quality_evidence(
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                conclusion_id INTEGER NOT NULL,
                notice_hash   TEXT NOT NULL,
                notice_id     TEXT NOT NULL,
                role          TEXT NOT NULL,
                detail        TEXT,
                FOREIGN KEY(conclusion_id) REFERENCES quality_conclusions(id)
            );
            CREATE TABLE IF NOT EXISTS quality_runs(
                run_id        TEXT PRIMARY KEY,
                mode          TEXT NOT NULL,
                started_ts    TEXT NOT NULL,
                finished_ts   TEXT,
                status        TEXT NOT NULL,
                fetched       INTEGER DEFAULT 0,
                ingested      INTEGER DEFAULT 0,
                detail        TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_qn_sample ON quality_notices(sample_id);
            CREATE INDEX IF NOT EXISTS idx_qc_latest ON quality_conclusions(sample_id, is_latest);
            """)
        # Separate statement: on an older database the index creation can
        # collide with pre-existing rows, in which case the missing index only
        # degrades lookup speed and must not break startup.
        try:
            self.con.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_qn_identity "
                "ON quality_notices(identity_hash)")
        except sqlite3.IntegrityError:
            pass
        self.con.commit()

    # ------------------------------------------------------------------ #
    # Run ledger – failure recovery.
    # ------------------------------------------------------------------ #
    def begin_run(self, mode: str, run_id: str | None = None) -> str:
        run_id = run_id or ("run-" + content_hash({"mode": mode, "ts": _utcnow()}))
        self.con.execute(
            "INSERT OR IGNORE INTO quality_runs(run_id,mode,started_ts,status)"
            " VALUES(?,?,?,?)", (run_id, mode, _utcnow(), "running"))
        self.con.commit()
        return run_id

    def finish_run(self, run_id: str, status: str, fetched: int,
                   ingested: int, detail: dict) -> None:
        self.con.execute(
            "UPDATE quality_runs SET finished_ts=?, status=?, fetched=?,"
            " ingested=?, detail=? WHERE run_id=?",
            (_utcnow(), status, fetched, ingested,
             json.dumps(detail, ensure_ascii=False), run_id))
        self.con.commit()

    def runs(self) -> list[dict]:
        rows = self.con.execute(
            "SELECT run_id,mode,started_ts,finished_ts,status,fetched,ingested,detail"
            " FROM quality_runs ORDER BY started_ts").fetchall()
        return [dict(zip(("run_id", "mode", "started_ts", "finished_ts", "status",
                          "fetched", "ingested", "detail"),
                         (r[:7] + (json.loads(r[7]) if r[7] else None,)))) for r in rows]

    # ------------------------------------------------------------------ #
    # Notice ingestion (immutable, idempotent).
    # ------------------------------------------------------------------ #
    def ingest_notice(self, raw: dict) -> tuple[bool, Notice]:
        """Insert one notice. Returns (was_new, normalized Notice).

        Idempotence is based on the notice's business identity, so a
        re-delivery that only differs in transport metadata (e.g. a new
        receipt timestamp) is recognized as the same notice.
        """
        notice = normalize_notice(raw)
        cur = self.con.execute(
            "INSERT OR IGNORE INTO quality_notices(notice_hash,identity_hash,"
            "sample_id,source,kind,notice_id,notice_ts,received_ts,payload)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (notice.notice_hash, notice.identity_hash, notice.sample_id,
             notice.source, notice.kind, notice.notice_id, notice.notice_ts,
             notice.received_ts,
             json.dumps(notice.raw, ensure_ascii=False)))
        return cur.rowcount > 0, notice

    def load_notices(self, sample_id: str | None = None) -> list[Notice]:
        if sample_id is None:
            rows = self.con.execute(
                "SELECT payload FROM quality_notices").fetchall()
        else:
            rows = self.con.execute(
                "SELECT payload FROM quality_notices WHERE sample_id=?",
                (sample_id,)).fetchall()
        return [normalize_notice(json.loads(r[0])) for r in rows]

    def notice_count(self) -> int:
        return self.con.execute("SELECT COUNT(*) FROM quality_notices").fetchone()[0]

    # ------------------------------------------------------------------ #
    # Conclusions – versioned and immutable.
    # ------------------------------------------------------------------ #
    def latest_conclusion(self, sample_id: str) -> dict | None:
        row = self.con.execute(
            "SELECT id,sample_id,version,state,result,basis_hash,basis_json,created_ts"
            " FROM quality_conclusions WHERE sample_id=? AND is_latest=1",
            (sample_id,)).fetchone()
        if not row:
            return None
        d = dict(zip(("id", "sample_id", "version", "state", "result",
                      "basis_hash", "basis_json", "created_ts"), row))
        d["basis"] = json.loads(d.pop("basis_json"))
        return d

    def conclusion_history(self, sample_id: str) -> list[dict]:
        rows = self.con.execute(
            "SELECT version,state,result,basis_hash,basis_json,created_ts,is_latest"
            " FROM quality_conclusions WHERE sample_id=? ORDER BY version",
            (sample_id,)).fetchall()
        out = []
        for r in rows:
            out.append({"version": r[0], "state": r[1], "result": r[2],
                        "basis_hash": r[3], "basis": json.loads(r[4]),
                        "created_ts": r[5], "is_latest": bool(r[6])})
        return out

    def all_latest(self) -> list[dict]:
        rows = self.con.execute(
            "SELECT sample_id,version,state,result,basis_hash,created_ts"
            " FROM quality_conclusions WHERE is_latest=1 ORDER BY sample_id").fetchall()
        return [dict(zip(("sample_id", "version", "state", "result",
                          "basis_hash", "created_ts"), r)) for r in rows]

    def persist_conclusion(self, basis: dict) -> dict:
        """Store a new conclusion version only when the basis actually changed.

        Returns a dict describing what happened (``created`` / ``unchanged``).
        Everything happens in one transaction so a crash cannot leave the
        latest-pointer and the evidence table disagreeing.
        """
        sample_id = basis["sample_id"]
        current = self.latest_conclusion(sample_id)
        if current and current["basis_hash"] == basis["basis_hash"]:
            return {"sample_id": sample_id, "action": "unchanged",
                    "version": current["version"], "state": basis["state"]}

        next_version = (current["version"] + 1) if current else 1
        created_ts = _utcnow()
        try:
            if current:
                self.con.execute(
                    "UPDATE quality_conclusions SET is_latest=0 WHERE id=?",
                    (current["id"],))
            cur = self.con.execute(
                "INSERT INTO quality_conclusions(sample_id,version,state,result,"
                "basis_hash,basis_json,created_ts,is_latest) VALUES(?,?,?,?,?,?,?,1)",
                (sample_id, next_version, basis["state"],
                 None if basis["result"] is None else str(basis["result"]),
                 basis["basis_hash"],
                 json.dumps(basis, ensure_ascii=False), created_ts))
            conclusion_id = cur.lastrowid
            for ev in basis["evidence"]:
                self.con.execute(
                    "INSERT INTO quality_evidence(conclusion_id,notice_hash,"
                    "notice_id,role,detail) VALUES(?,?,?,?,?)",
                    (conclusion_id, ev["notice_hash"], ev["notice_id"], ev["role"],
                     json.dumps({k: v for k, v in ev.items()
                                 if k not in ("notice_hash", "notice_id", "role")},
                                ensure_ascii=False)))
            self.con.commit()
        except Exception:
            self.con.rollback()
            raise
        return {"sample_id": sample_id, "action": "created",
                "version": next_version, "state": basis["state"]}

    def reevaluate_sample(self, sample_id: str) -> dict:
        notices = self.load_notices(sample_id)
        basis = evaluate_sample(notices)
        return self.persist_conclusion(basis)

    def reevaluate_all(self) -> list[dict]:
        sample_ids = sorted({r[0] for r in self.con.execute(
            "SELECT DISTINCT sample_id FROM quality_notices")})
        return [self.reevaluate_sample(sid) for sid in sample_ids]

    def evidence_for(self, sample_id: str) -> list[dict]:
        """Traceability: notices + roles behind the latest conclusion."""
        latest = self.latest_conclusion(sample_id)
        if not latest:
            return []
        rows = self.con.execute(
            "SELECT notice_hash,notice_id,role,detail FROM quality_evidence"
            " WHERE conclusion_id=? ORDER BY role,notice_id", (latest["id"],)).fetchall()
        return [dict(zip(("notice_hash", "notice_id", "role", "detail"),
                         (r[0], r[1], r[2], json.loads(r[3]) if r[3] else None)))
                for r in rows]

    def reset_quality(self) -> None:
        """Remove only quality-domain tables; legacy data stays untouched."""
        self.con.executescript(
            "DELETE FROM quality_evidence; DELETE FROM quality_conclusions;"
            " DELETE FROM quality_notices; DELETE FROM quality_runs;")
        self.con.commit()
