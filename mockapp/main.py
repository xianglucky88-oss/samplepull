
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import hashlib
import json
from fastapi import FastAPI, Request, Response
from samplepull.config import SPEC
from samplepull.mockdata import build_rows, extra_row

app = FastAPI(title="Local source")
_ALL = build_rows()
_extra = []
_verified_sessions = set()
_accepted_tokens = set()

def _data():
    return _ALL + _extra

def _page_items(page):
    lo, hi = (page - 1) * SPEC.page_size, page * SPEC.page_size
    return _data()[lo:hi]

@app.get("/items")
def items(page: int = 1):
    data = _page_items(page)
    if SPEC.fmt == "json":
        return {"items": data, "page": page}
    if SPEC.fmt == "text":
        lines = ["\t".join(str(it.get(c, "")) for c in SPEC.columns) for it in data]
        return Response(content="\n".join(lines), media_type="text/plain")
    th = "".join("<th>%s</th>" % c for c in SPEC.columns)
    trs = "".join("<tr>" + "".join("<td>%s</td>" % it.get(c, "") for c in SPEC.columns)
                  + "</tr>" for it in data)
    return Response(content="<html><table><tr>%s</tr>%s</table></html>" % (th, trs),
                    media_type="text/html")

@app.get("/refresh")
def refresh():
    _extra.append(extra_row(len(_extra) + 1))
    return {"total": len(_data())}

@app.get("/probe")
def probe():
    app.__dict__["_flaky_n"] = app.__dict__.get("_flaky_n", 0) + 1
    if app.__dict__["_flaky_n"] <= 2:
        return Response(status_code=429)
    return {"ok": True, "attempts": app.__dict__["_flaky_n"]}

@app.get("/health")
def health():
    return {"ok": True, "rows": len(_data())}

@app.post("/login")
def login(response: Response):
    sid = "SID-local"
    csrf = "CSRF-local"
    challenge = hashlib.sha256((sid + csrf).encode()).hexdigest()[:4]
    response.set_cookie("sid", sid)
    response.set_cookie("csrf", csrf)
    response.set_cookie("challenge", challenge)
    return {"role": "operator"}

@app.get("/query")
def query(request: Request, page: int = 1):
    if not request.cookies.get("sid"):
        return Response(status_code=401)
    if request.headers.get("x-csrf") != request.cookies.get("csrf"):
        return Response(status_code=403)
    items_page = _page_items(page)
    nxt = _page_items(page + 1)
    sid = request.cookies.get("sid")
    gate = (SPEC.engine == "rpa" and page == 1 and sid not in _verified_sessions
            and len(_data()) > SPEC.page_size)
    return {"items": items_page, "page": page, "has_more": bool(nxt),
            "captcha_required": bool(gate)}

@app.post("/captcha")
async def captcha(request: Request):
    body = await request.json()
    ok = body.get("code") == request.cookies.get("challenge")
    if ok:
        _verified_sessions.add(request.cookies.get("sid"))
    return {"ok": bool(ok)}

@app.post("/submit")
async def submit(request: Request):
    body = await request.json()
    token = body.get("token")
    created = token not in _accepted_tokens
    if created:
        _accepted_tokens.add(token)
    return {"created": bool(created), "token": token}


# --------------------------------------------------------------------------- #
# Quality-notice source (初检 / 复检 / 撤销).
#
# Independent of the legacy endpoints above.  Control hooks let the regression
# suite simulate source outages, reordered delivery, duplicate re-reports and
# late revisions.
# --------------------------------------------------------------------------- #
import random as _random
from datetime import datetime as _dt, timedelta as _td, timezone as _tz

_Q_BASE = _dt(2030, 2, 1, tzinfo=_tz.utc)


def _qts(minutes: int) -> str:
    return (_Q_BASE + _td(minutes=minutes)).isoformat().replace("+00:00", "Z")


def _qnotice(nid, sample, kind, result, source, minutes, version=1,
             target=None, received_minutes=None):
    d = {
        "notice_id": nid,
        "sample_id": sample,
        "kind": kind,
        "result": result,
        "source": source,
        "notice_version": version,
        "notice_ts": _qts(minutes),
        "received_ts": _qts(received_minutes if received_minutes is not None
                            else minutes),
    }
    if target is not None:
        d["target_notice_id"] = target
    return d


def _quality_seed():
    """Deterministic baseline covering every conclusion state."""
    return [
        # S01: initial only -> 暂定
        _qnotice("N-001", "Q-S01", "initial", "PASS", "lab_a", 1),
        # S02: initial + agreeing recheck -> 已发布
        _qnotice("N-002", "Q-S02", "initial", "PASS", "lab_a", 2),
        _qnotice("N-003", "Q-S02", "recheck", "PASS", "lab_b", 4),
        # S03: initial vs recheck disagree -> 冲突
        _qnotice("N-004", "Q-S03", "initial", "PASS", "lab_a", 3),
        _qnotice("N-005", "Q-S03", "recheck", "FAIL", "lab_b", 5),
        # S04: recheck without initial -> 缺少关键信息
        _qnotice("N-006", "Q-S04", "recheck", "PASS", "lab_b", 6),
        # S05: full pair, then the recheck is revoked -> back to 暂定
        _qnotice("N-007", "Q-S05", "initial", "PASS", "lab_a", 7),
        _qnotice("N-008", "Q-S05", "recheck", "PASS", "lab_b", 8),
        _qnotice("N-009", "Q-S05", "revoke", None, "lab_b", 11,
                 target="N-008"),
        # S06: the same initial notice re-reported by the source -> dedup
        _qnotice("N-010", "Q-S06", "initial", "FAIL", "lab_a", 9),
        _qnotice("N-010", "Q-S06", "initial", "FAIL", "lab_a", 9,
                 received_minutes=10),
        # S07: a late revision of the initial arrives last in time
        _qnotice("N-011", "Q-S07", "initial", "PASS", "lab_a", 12),
        _qnotice("N-012", "Q-S07", "initial", "FAIL", "lab_a", 13,
                 version=2, received_minutes=14),
    ]


_quality_items: list = []
_quality_outage = {"remaining": 0}
_quality_shuffled = {"on": False}


def reset_quality():
    """Test helper: restore the deterministic baseline exactly."""
    _quality_items.clear()
    _quality_items.extend(_quality_seed())
    _quality_outage["remaining"] = 0
    _quality_shuffled["on"] = False


def add_quality_notice(notice):
    _quality_items.append(notice)
    if _quality_shuffled["on"]:
        _random.Random(42).shuffle(_quality_items)
    return len(_quality_items)


def set_quality_outage(count):
    _quality_outage["remaining"] = max(0, int(count))


def set_quality_shuffle(on):
    _quality_shuffled["on"] = bool(on)
    if on:
        _random.Random(42).shuffle(_quality_items)


reset_quality()


@app.get("/quality/items")
def quality_items(page: int = 1, since: str | None = None,
                  page_size: int = 3):
    if _quality_outage["remaining"] > 0:
        _quality_outage["remaining"] -= 1
        return Response(status_code=503)
    pool = [it for it in _quality_items
            if not since or it.get("received_ts", "") > since]
    lo, hi = (page - 1) * page_size, page * page_size
    batch = pool[lo:hi]
    return {"items": batch, "page": page, "page_size": page_size,
            "has_more": hi < len(pool), "total": len(pool)}


@app.get("/quality/health")
def quality_health():
    return {"ok": _quality_outage["remaining"] == 0,
            "outage_remaining": _quality_outage["remaining"],
            "rows": len(_quality_items), "shuffled": _quality_shuffled["on"]}


@app.post("/quality/control")
async def quality_control(request: Request):
    body = await request.json()
    action = body.get("action")
    if action == "reset":
        reset_quality()
    elif action == "outage":
        set_quality_outage(body.get("count", 1))
    elif action == "shuffle":
        set_quality_shuffle(bool(body.get("on", True)))
    elif action == "add":
        add_quality_notice(body["notice"])
    elif action == "add_many":
        for notice in body.get("notices", []):
            add_quality_notice(notice)
    else:
        return Response(status_code=400,
                        content='{"error":"unknown action"}',
                        media_type="application/json")
    return {"ok": True, "rows": len(_quality_items),
            "outage_remaining": _quality_outage["remaining"],
            "shuffled": _quality_shuffled["on"]}
