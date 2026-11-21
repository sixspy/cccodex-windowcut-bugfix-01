"""关窗禁写入册：当日口径钉死、禁写偏了、封存台账。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import seal_policy
from app import app, init_db


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", tmp_path / "policy.db")
    init_db()
    yield


def _h(c: TestClient, username: str = "admin", password: str = "123456") -> dict:
    tok = c.post("/api/login", json={"username": username, "password": password}).json()["token"]
    return {"Authorization": "Bearer " + tok}


def _actions(c: TestClient, h: dict) -> list[str]:
    return [a["action"] for a in c.get("/api/audit?limit=300", headers=h).json()["items"]]


def test_register_pins_single_policy_per_day():
    c = TestClient(app)
    h = _h(c)
    body = c.get("/api/write-policy", headers=h).json()
    assert body["policy"] == seal_policy.POLICY_LATE_BUCKET
    assert body["selected_path"] == "乙"
    assert body["effective_date"] == seal_policy.today()

    # 同日确认同值：幂等通过，口径不变
    ok = c.post("/api/write-policy/change", headers=h, json={"policy": "late_bucket"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["policy"] == "late_bucket"

    # 同日改口：拒绝并记「禁写偏了」
    bad = c.post("/api/write-policy/change", headers=h, json={"policy": "reject"})
    assert bad.status_code == 409, bad.text
    detail = bad.json()["detail"]
    assert detail["registered"] == "late_bucket"
    assert detail["requested"] == "reject"
    assert "禁写偏了" in detail["message"]

    after = c.get("/api/write-policy", headers=h).json()
    assert after["policy"] == "late_bucket"
    assert after["effective_date"] == body["effective_date"]
    assert "write_policy_deviation" in _actions(c, h)


def test_register_keeps_history_across_days(monkeypatch):
    """换日不覆盖旧册：旧日口径保留，新日按当日重新入册。"""
    c = TestClient(app)
    h = _h(c)
    # 先落今日册，再模拟「昨日」建一条历史行
    import db

    with db.connect() as conn:
        seal_policy.ensure_registered(conn, day="2026-11-20")
        conn.commit()
    body = c.get("/api/write-policy", headers=h).json()
    assert body["effective_date"] == seal_policy.today()
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT effective_date FROM write_policy_ledger ORDER BY effective_date"
        ).fetchall()
    days = [r["effective_date"] for r in rows]
    assert "2026-11-20" in days
    assert seal_policy.today() in days


def test_close_seals_window_and_reports_no_ingest():
    c = TestClient(app)
    h = _h(c)
    first = c.post("/api/windows/2/close", headers=h).json()
    assert first["policy"] == "late_bucket"
    assert first["effective_date"] == seal_policy.today()

    again = c.post("/api/windows/2/close", headers=h).json()
    assert again["already"] is True
    assert again["accept_ingest"] is False
    assert again["policy"] == "late_bucket"

    ledger = c.get("/api/write-policy", headers=h).json()
    sealed = {(s["window_id"], s["generation"]) for s in ledger["sealed_windows"]}
    assert (1, 1) in sealed  # 历史已关闭窗：启动时入册
    assert (2, 1) in sealed
    assert "seal_policy_seal" in _actions(c, h)


def test_deviation_on_closed_window_ingest_rejects_without_writes():
    c = TestClient(app)
    h = _h(c)
    assert c.post("/api/windows/2/close", headers=h).status_code == 200
    before_events = c.get("/api/events", headers=h).json()["items"]
    before_late = c.get("/api/late-bucket", headers=h).json()["count"]
    before_rejects = c.get("/api/rejects", headers=h).json()["items"]

    r = c.post(
        "/api/events",
        headers=h,
        json={
            "window_name": "W-1102",
            "biz_key": "DEV-1",
            "event_ts": "2026-11-02T22:00:00",
            "amount": 1,
            "write_policy": "reject",
        },
    )
    assert r.status_code == 409, r.text
    assert "禁写偏了" in r.json()["detail"]["message"]

    # 五面均无新增
    assert c.get("/api/events", headers=h).json()["items"] == before_events
    assert c.get("/api/late-bucket", headers=h).json()["count"] == before_late
    assert c.get("/api/rejects", headers=h).json()["items"] == before_rejects
    assert "write_policy_deviation" in _actions(c, h)
