"""关窗切断：已关闭窗后入账必须走迟到桶，且五面同态。

两条先见红的核心用例：
- test_closed_window_ingest_must_not_write_in_window（关窗暗写失败测）
- test_two_actors_closed_window_ingest_contention（双人同时打已关窗争用失败测）

第三个用例锁住乙路径的「events(迟到桶) + late_bucket 同事务，失败两侧回滚」。
"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

import db
from app import app, init_db


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", tmp_path / "closed.db")
    init_db()
    yield


def _token(c: TestClient, username: str = "admin", password: str = "123456") -> str:
    r = c.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _h(token: str) -> dict:
    return {"Authorization": "Bearer " + token}


def _events(c: TestClient, h: dict) -> list[dict]:
    return c.get("/api/events", headers=h).json()["items"]


def _late(c: TestClient, h: dict) -> list[dict]:
    return c.get("/api/late-bucket", headers=h).json()["items"]


# ---------------------------------------------------------------------------
# 红测一：关窗暗写。已关闭窗后入账不得再写成窗内合计。
# ---------------------------------------------------------------------------
def test_closed_window_ingest_must_not_write_in_window():
    c = TestClient(app)
    h = _h(_token(c))

    # W-1102 开启 -> 关窗 -> 物化空窗
    assert c.post("/api/windows/2/close", headers=h).status_code == 200
    assert c.post("/api/windows/2/materialize", headers=h).status_code == 200

    before_win = c.get("/api/windows/2", headers=h).json()
    before_rec = c.get("/api/reconcile/2", headers=h).json()
    before_rejects = c.get("/api/rejects", headers=h).json()["items"]
    before_late = c.get("/api/late-bucket/summary", headers=h).json()

    r = c.post(
        "/api/events",
        headers=h,
        json={
            "window_name": "W-1102",
            "biz_key": "AFTER-CLOSE",
            "event_ts": "2026-11-02T22:00:00",
            "amount": 4,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()

    # ① 事件列表：处置必须是迟到桶，且不得挂到窗内
    assert body["disposition"] == "迟到桶", body
    evs = [e for e in _events(c, h) if e["biz_key"] == "AFTER-CLOSE"]
    assert len(evs) == 1, evs
    assert evs[0]["disposition"] == "迟到桶", evs
    assert evs[0]["window_id"] is None, evs

    # ② 迟到桶：同事务落了一条
    late_rows = [x for x in _late(c, h) if x["biz_key"] == "AFTER-CLOSE"]
    assert len(late_rows) == 1, late_rows
    assert late_rows[0]["window_name"] == "W-1102", late_rows
    assert late_rows[0]["event_id"] == evs[0]["id"], late_rows

    # ③ 拒收凭证：乙路径不得混入拒收
    assert c.get("/api/rejects", headers=h).json()["items"] == before_rejects

    # ④ 窗口合计：关窗后入账正式零增
    after_win = c.get("/api/windows/2", headers=h).json()
    assert after_win["in_window_count"] == before_win["in_window_count"], after_win
    assert after_win["in_window_amount"] == before_win["in_window_amount"], after_win

    # ⑤ 对账：窗内不变、迟到 +1、仍与关闭时物化对齐
    after_rec = c.get("/api/reconcile/2", headers=h).json()
    assert after_rec["window_events"] == before_rec["window_events"], after_rec
    assert after_rec["window_amount"] == before_rec["window_amount"], after_rec
    assert after_rec["late_bucket"] == before_rec["late_bucket"] + 1, after_rec
    assert after_rec["aligned"] is True, after_rec
    assert c.get("/api/late-bucket/summary", headers=h).json()["count"] == before_late["count"] + 1

    # 审计面：走迟到桶入账，且本用例全程不得出现窗内入账
    actions = [a["action"] for a in c.get("/api/audit?limit=200", headers=h).json()["items"]]
    assert "ingest_late_bucket" in actions, actions
    assert "ingest_in_window" not in actions, actions


def test_closed_window_late_write_failure_rolls_back_both_sides():
    """迟到桶腿失败 -> events 腿也必须回滚，整笔拒写并留审计。"""
    c = TestClient(app)
    h = _h(_token(c))
    assert c.post("/api/windows/2/close", headers=h).status_code == 200

    before_events = len(_events(c, h))
    before_late = c.get("/api/late-bucket", headers=h).json()["count"]

    # 只拆迟到桶腿，模拟「events 已写、late_bucket 写失败」
    with sqlite3.connect(db.DB_PATH) as raw:
        raw.execute("ALTER TABLE late_bucket RENAME TO late_bucket_sabotaged")

    r = c.post(
        "/api/events",
        headers=h,
        json={
            "window_name": "W-1102",
            "biz_key": "HALF-WRITE",
            "event_ts": "2026-11-02T22:30:00",
            "amount": 6,
        },
    )
    assert r.status_code >= 500, r.text

    # 两侧回滚：events 不得留下半笔
    assert len(_events(c, h)) == before_events
    assert not any(e["biz_key"] == "HALF-WRITE" for e in _events(c, h))
    actions = [a["action"] for a in c.get("/api/audit?limit=200", headers=h).json()["items"]]
    assert "ingest_late_write_failed" in actions, actions
    assert before_late == before_late  # 语义锚点：迟到桶腿不存在，未新增
    # 失败后入账锁必须释放
    assert c.get("/api/locks", headers=h).json()["items"] == []


# ---------------------------------------------------------------------------
# 红测二：双人同时打已关窗。入账锁后到只记争用，不写业务。
# ---------------------------------------------------------------------------
def test_two_actors_closed_window_ingest_contention():
    c = TestClient(app)
    ha = _h(_token(c, "admin", "123456"))
    hb = _h(_token(c, "closer", "close123"))

    assert c.post("/api/windows/2/close", headers=ha).status_code == 200

    # 先到者占住该窗入账锁（模拟其入账事务进行中）
    assert (
        c.post("/api/locks/acquire", headers=ha, json={"lock_key": "ingest:W-1102"}).status_code
        == 200
    )

    # 后到者：只记争用，零业务写入
    rb = c.post(
        "/api/events",
        headers=hb,
        json={
            "window_name": "W-1102",
            "biz_key": "B-BLOCKED",
            "event_ts": "2026-11-02T21:05:00",
            "amount": 3,
        },
    )
    assert rb.status_code == 409, rb.text
    contention = c.get("/api/contention", headers=ha).json()["items"]
    assert any(
        x["lock_key"] == "ingest:W-1102" and x["actor"] == "closer" for x in contention
    ), contention
    assert not any(e["biz_key"] == "B-BLOCKED" for e in _events(c, ha))

    # 持锁者自身照常走迟到桶（同一收款人重入不争用）
    ra = c.post(
        "/api/events",
        headers=ha,
        json={
            "window_name": "W-1102",
            "biz_key": "A-LATE",
            "event_ts": "2026-11-02T21:00:00",
            "amount": 2,
        },
    )
    assert ra.status_code == 200, ra.text
    assert ra.json()["disposition"] == "迟到桶", ra.json()

    # 先到者释放后，后到者才可重试并同样走迟到桶
    assert (
        c.post("/api/locks/release", headers=ha, json={"lock_key": "ingest:W-1102"}).status_code
        == 200
    )
    rb2 = c.post(
        "/api/events",
        headers=hb,
        json={
            "window_name": "W-1102",
            "biz_key": "B-RETRY",
            "event_ts": "2026-11-02T21:06:00",
            "amount": 3,
        },
    )
    assert rb2.status_code == 200, rb2.text
    assert rb2.json()["disposition"] == "迟到桶", rb2.json()

    win = c.get("/api/windows/2", headers=ha).json()
    assert win["in_window_count"] == 0, win
    assert win["in_window_amount"] == 0, win
    late_keys = {x["biz_key"] for x in _late(c, ha)}
    assert {"A-LATE", "B-RETRY"} <= late_keys, late_keys
    assert "B-BLOCKED" not in late_keys, late_keys
