
import json, sys
from . import pipeline


def _show_sample(con, sample_id):
    from .qstore import QualityStore
    qs = QualityStore(con)
    latest = qs.latest_conclusion(sample_id)
    if latest is None:
        return {"error": "no conclusion for sample %s" % sample_id}
    from .quality import explain
    return {
        "sample_id": sample_id,
        "state": latest["state"],
        "state_label": latest["basis"]["state_label"],
        "result": latest["result"],
        "version": latest["version"],
        "basis_hash": latest["basis_hash"],
        "explanation": explain(latest["basis"]),
        "rules_fired": [
            {"rule": rid, "version": latest["basis"]["rules_catalog"].get(rid)}
            for rid in latest["basis"]["rules_fired"]
        ],
        "participating_notice_ids": latest["basis"]["participating_notice_ids"],
        "evidence": qs.evidence_for(sample_id),
        "history": qs.conclusion_history(sample_id),
        "anomalies": latest["basis"]["anomalies"],
    }


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    mode = "full"
    if "--incremental" in argv:
        mode = "incremental"
    if "--full" in argv:
        mode = "full"

    if "--quality" in argv:
        from .store import Store
        from .qstore import QualityStore
        con = Store().connect()
        try:
            if "--list" in argv:
                qs = QualityStore(con)
                print(json.dumps(qs.all_latest(), ensure_ascii=False, indent=2))
                return
            show_idx = argv.index("--show") + 1 if "--show" in argv else None
            if show_idx is not None and show_idx < len(argv):
                print(json.dumps(_show_sample(con, argv[show_idx]),
                                 ensure_ascii=False, indent=2))
                return
            print(json.dumps(pipeline.run_quality(mode=mode, con=con),
                             ensure_ascii=False))
        finally:
            con.close()
        return

    print(json.dumps(pipeline.run(mode=mode), ensure_ascii=False))


if __name__ == "__main__":
    main()
