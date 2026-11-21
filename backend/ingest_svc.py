"""事件入账：窗内 / 拒收 / 迟到桶分流。

关窗后入账按《关窗禁写入册》走 **乙路径**：
    events(disposition='迟到桶', window_id=NULL) + late_bucket 同事务；
    任一腿失败则两侧回滚，整笔不落。

「窗外且窗仍开启」的拒收是另一条业务规则，与关窗口径互不混用。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

from fastapi import APIRouter, HTTPException

import seal_policy
from auth import AuthHeader, require_user
from db import connect, write_audit
from late_svc import write_closed_window_late
from locks import release, require_lock
from models import EventIn

router = APIRouter(tags=["ingest"])


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _deviation(conn, body: EventIn, user: str, registered: str) -> None:
    write_audit(
        conn,
        user,
        "write_policy_deviation",
        body.biz_key,
        {
            "note": seal_policy.DEVIATION_NOTE,
            "requested": body.write_policy,
            "registered": registered,
            "window": body.window_name,
        },
    )


@router.post("/api/events")
def ingest_event(body: EventIn, authorization: AuthHeader = None):
    user = require_user(authorization)
    lock_key = f"ingest:{body.window_name}"
    with connect() as conn:
        require_lock(conn, lock_key, user)
        try:
            w = conn.execute(
                "SELECT * FROM windows WHERE name=?", (body.window_name,)
            ).fetchone()
            if not w:
                raise HTTPException(404, "窗口不存在")
            ts = body.event_ts
            created = _now()

            if w["status"] == "已关闭":
                # 口径以该窗封存记录为准；缺册则回落当日写入册（绝不改口）
                policy_row = seal_policy.window_policy(conn, w)
                registered = policy_row["policy"]
                if body.write_policy is not None and body.write_policy != registered:
                    # 偏离关窗禁写入册：拒写并记「禁写偏了」
                    _deviation(conn, body, user, registered)
                    conn.commit()
                    raise HTTPException(
                        409,
                        {
                            "message": seal_policy.DEVIATION_NOTE,
                            "requested": body.write_policy,
                            "registered": registered,
                        },
                    )
                try:
                    event_id, late_id = write_closed_window_late(
                        conn, w, body.biz_key, body.event_ts, body.amount, created
                    )
                except sqlite3.Error as exc:
                    # 乙路径两腿必须同生共死：回滚后整笔不落
                    conn.rollback()
                    write_audit(
                        conn,
                        user,
                        "ingest_late_write_failed",
                        body.biz_key,
                        {
                            "window": body.window_name,
                            "error": str(exc),
                            "note": "迟到桶写腿失败：events/late_bucket 两侧回滚",
                        },
                    )
                    conn.commit()
                    raise HTTPException(500, "迟到桶写入失败，整笔回滚") from exc
                write_audit(
                    conn,
                    user,
                    "ingest_late_bucket",
                    body.biz_key,
                    {
                        "window_id": w["id"],
                        "late_id": late_id,
                        "policy": registered,
                        "effective_date": seal_policy.today(),
                    },
                )
                conn.commit()
                return {
                    "disposition": "迟到桶",
                    "window_id": None,
                    "event_id": event_id,
                    "late_id": late_id,
                    "policy": registered,
                }

            if not (w["open_at"] <= ts <= w["close_at"]):
                conn.execute(
                    """INSERT INTO events(window_id,biz_key,event_ts,amount,disposition,created_at)
                       VALUES (?,?,?,?,?,?)""",
                    (None, body.biz_key, ts, body.amount, "拒收", created),
                )
                conn.execute(
                    """INSERT INTO reject_slips(window_name,biz_key,event_ts,amount,reason,created_at)
                       VALUES (?,?,?,?,?,?)""",
                    (
                        body.window_name,
                        body.biz_key,
                        ts,
                        body.amount,
                        "窗外且窗仍开启",
                        created,
                    ),
                )
                write_audit(
                    conn,
                    user,
                    "ingest_reject",
                    body.biz_key,
                    {"window": body.window_name},
                )
                conn.commit()
                raise HTTPException(409, "事件时间落在窗外且窗仍开启：拒收")

            cur = conn.execute(
                """INSERT INTO events(window_id,biz_key,event_ts,amount,disposition,created_at)
                   VALUES (?,?,?,?,?,?)""",
                (w["id"], body.biz_key, ts, body.amount, "窗内", created),
            )
            write_audit(
                conn,
                user,
                "ingest_in_window",
                body.biz_key,
                {"window_id": w["id"]},
            )
            conn.commit()
            return {"disposition": "窗内", "window_id": w["id"], "event_id": cur.lastrowid}
        finally:
            release(conn, lock_key, user)
            conn.commit()


@router.get("/api/events")
def list_events(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM events ORDER BY id DESC LIMIT 200"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.get("/api/rejects")
def list_rejects(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM reject_slips ORDER BY id DESC LIMIT 100"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}
