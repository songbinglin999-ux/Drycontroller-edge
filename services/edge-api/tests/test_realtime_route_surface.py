import sys
from pathlib import Path

from fastapi import FastAPI

PATH = Path(__file__).resolve()
SERVICE_ROOT = PATH.parents[1]
ROOT = PATH.parents[3]
for entry in {str(ROOT), str(SERVICE_ROOT)}:
    if entry not in sys.path:
        sys.path.append(entry)

from app.api import realtime as realtime_router  # noqa: E402
from app.api.v1 import router as api_v1_router  # noqa: E402


def test_realtime_data_plane_has_single_public_surface():
    app = FastAPI()
    app.include_router(api_v1_router, prefix="/api/v1")
    app.include_router(realtime_router.router, prefix="/api")

    paths = {getattr(route, "path", "") for route in app.routes}

    assert "/api/ws/realtime" in paths
    assert "/api/v1/data/latest" in paths
    assert "/api/v1/data/history" in paths
    assert "/api/v1/data/tags" in paths

    assert "/api/realtime/latest" not in paths
    assert "/api/history" not in paths
    assert "/api/realtime/stream" not in paths
    assert "/api/v1/data/realtime" not in paths
