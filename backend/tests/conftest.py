import os

import pytest

os.environ.setdefault("FLOORWALKER_NO_LOOP", "1")   # never start the real-time loop under pytest
os.environ.pop("ANTHROPIC_API_KEY", None)           # tests must not call the real API


@pytest.fixture(scope="session")
def baseline():
    """Fitted seasonal baseline. ~10 s the first time, then cached in backend/.cache."""
    from app.simulator import bootstrap_world
    return bootstrap_world(seed=0, cache_dir=".cache").baseline
