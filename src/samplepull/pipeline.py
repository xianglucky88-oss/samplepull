
"""Public orchestration entry point."""
from . import framework

def run(mode="full", con=None):
    from .config import SPEC
    if SPEC.engine == "rpa":
        return framework.run_rpa(con=con)
    return framework.run(mode=mode, con=con)

def run_quality(mode="full", con=None, transport=None):
    """Quality-conclusion pipeline (independent of the legacy engine)."""
    from .qpipeline import run_quality as _run
    return _run(mode=mode, con=con, transport=transport)
