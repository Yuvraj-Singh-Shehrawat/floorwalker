from fastapi.testclient import TestClient

from app import main


def test_api_end_to_end_without_the_realtime_loop():
    rt = main.rt
    with TestClient(main.app) as client:
        assert client.get("/api/state").json()["status"] == "warming_up"
        assert client.get("/").json()["endpoints"]["health"] == "/healthz"   # API root is not a bare 404
        assert client.post("/api/chaos", json={"type": "bad_floor_push"}).status_code == 503
        rt.boot()
        state = client.get("/api/state").json()
        assert state["status"] == "running" and len(state["series"]["revenue"]) == 180
        assert {g["geo"] for g in state["geos"]} == {"IN", "US", "BR", "ID", "DE"}
        assert len(client.get("/api/chaos/catalog").json()["types"]) == 5
        assert client.post("/api/chaos", json={"type": "nope"}).status_code == 422
        r = client.post("/api/chaos", json={"type": "bad_floor_push", "geo": "US"})
        assert r.status_code == 200 and "geo" not in r.json()
        assert client.post("/api/chaos", json={"type": "traffic_drop"}).status_code == 409   # one at a time
        assert client.get("/api/config").json()["audit_log"][0]["actor"] == "pricing-sync"
        assert client.get("/api/policy").json()["canary"]["fraction"] == 0.10
        assert client.post("/api/incidents/INC-0001/approval", json={"approved": True}).status_code in (404, 409)
