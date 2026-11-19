"""Collection pipeline for quality notices.

Reuses the local transport and bounded-retry HTTP helper, but keeps a fully
separate code path from the legacy crawl/RPA pipelines so existing commands and
stored records are untouched.

Guarantees:

* **Full vs incremental** – incremental requests carry the persisted
  ``quality_watermark`` (server *receipt* time), so a late revision is never
  skipped merely because its own event time is old.
* **Failure recovery** – the run ledger is opened before fetching and closed
  after committing.  A source outage or raised exception leaves the watermark
  and every published conclusion unchanged; the next run resumes and the
  idempotent notice store makes re-delivery harmless.
* **Stable published conclusions** – only samples whose notice set actually
  changed are re-evaluated; all other samples keep their existing version.
"""
from __future__ import annotations

import json

from .framework import Http, Sleeper, Transport
from .qstore import QualityStore
from .store import Store

QUALITY_WATERMARK_KEY = "quality_watermark"
QUALITY_PAGE_SIZE = 3


def fetch_quality_notices(http: Http, since: str | None = None,
                          page_size: int = QUALITY_PAGE_SIZE) -> list[dict]:
    """Page through ``/quality/items``; retries are handled by ``Http``."""
    items: list[dict] = []
    page = 1
    attempts = 0
    while True:
        status, _hdr, body, att = http.get(
            "/quality/items", params={"page": page, "since": since,
                                      "page_size": page_size})
        attempts += att + 1
        if status != 200:
            raise RuntimeError(
                "quality source unavailable status=%s page=%s" % (status, page))
        data = json.loads(body)
        batch = data.get("items", [])
        items.extend(batch)
        if not data.get("has_more"):
            break
        page += 1
        if page > 500:
            break
    http.last_attempts = attempts  # type: ignore[attr-defined]
    return items


def _get_watermark(store: Store) -> str | None:
    row = store.con.execute(
        "SELECT v FROM meta WHERE k=?", (QUALITY_WATERMARK_KEY,)).fetchone()
    return row[0] if row else None


def _set_watermark(store: Store, value: str) -> None:
    store.con.execute(
        "INSERT INTO meta(k,v) VALUES(?,?) "
        "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (QUALITY_WATERMARK_KEY, str(value)))
    store.con.commit()


def run_quality(mode: str = "full", con=None,
                transport: Transport | None = None) -> dict:
    own = con is None
    con = con or Store().connect()
    legacy = Store(con)
    qstore = QualityStore(con)
    sleeper = Sleeper()
    transport = transport or Transport()
    http = Http(transport, sleeper)
    run_id = qstore.begin_run(mode)
    try:
        since = None
        if mode == "incremental":
            since = _get_watermark(legacy)
        notices = fetch_quality_notices(http, since=since)

        ingested = 0
        affected: set[str] = set()
        max_received = since or ""
        for raw in notices:
            is_new, notice = qstore.ingest_notice(raw)
            if is_new:
                ingested += 1
            affected.add(notice.sample_id)
            if notice.received_ts and notice.received_ts > max_received:
                max_received = notice.received_ts

        # Re-evaluate only samples touched by this batch.  Every evaluation
        # reads the full stored notice set, so ordering inside this batch
        # cannot matter and revokes arriving late resolve correctly.
        changes = [qstore.reevaluate_sample(sid) for sid in sorted(affected)]
        created = sum(1 for c in changes if c["action"] == "created")

        # Watermark advances only after ingestion + evaluation committed.
        if max_received:
            _set_watermark(legacy, max_received)

        detail = {"fetched": len(notices), "ingested": ingested,
                  "affected_samples": sorted(affected),
                  "conclusions_created": created,
                  "watermark": _get_watermark(legacy),
                  "http_attempts": getattr(http, "last_attempts", 0),
                  "slept": round(sleeper.slept, 4)}
        qstore.finish_run(run_id, "ok", len(notices), ingested, detail)
        return {"engine": "quality", "mode": mode, "run_id": run_id, **detail,
                "notice_count": qstore.notice_count(),
                "samples": len(qstore.all_latest())}
    except Exception as exc:  # failure recovery: record and re-raise
        qstore.finish_run(run_id, "failed", 0, 0, {"error": str(exc)})
        raise
    finally:
        if own:
            con.close()
