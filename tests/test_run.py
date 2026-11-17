from samplepull.config import SPEC
from samplepull.pipeline import run
from samplepull.store import Store

def test_pipeline_returns_a_status():
    con = Store().connect()
    Store(con).reset()
    try:
        result = run(con=con)
    finally:
        con.close()
    assert isinstance(result, dict)
    assert result.get("engine") == SPEC.engine
    assert result
