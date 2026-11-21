"""关窗禁写入册：已关闭窗后入账的唯一正确性口径。

本交付选定 **乙路径**：
    关窗后入账 -> 同一事务写 events(disposition='迟到桶', window_id=NULL)
                + late_bucket；任一腿失败则两侧回滚，整笔不落。

甲路径（关窗后整笔拒收 + 拒收凭证）仅登记、不启用；两条路径不得混用。
「窗外且窗仍开启」的拒收是另一条业务规则，与本册的关窗口径无关。

写入册一旦在当日入册，当日禁止改口；任何偏离本册的写入请求一律拒写，
并在审计里记「禁写偏了」。
"""
from __future__ import annotations

from datetime import date, datetime

DOMAIN = "closed_window_ingest"

POLICY_LATE_BUCKET = "late_bucket"   # 乙：强制进迟到桶（本交付启用）
POLICY_REJECT = "reject"             # 甲：整笔拒收（仅登记，不启用）

IMPLEMENTED_POLICY = POLICY_LATE_BUCKET
SELECTED_PATH = "乙"
SELECTED_PATH_LABEL = (
    "乙路径：关窗后入账必须同事务写 late_bucket，events.disposition=迟到桶；"
    "任一腿失败两侧回滚"
)
DEVIATION_NOTE = "禁写偏了：请求的写入口径与《关窗禁写入册》不一致，已拒写"


def today() -> str:
    return date.today().isoformat()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def ensure_registered(conn, day: str | None = None) -> dict:
    """当日写入册不存在则落册；已存在则原样返回，绝不改口。"""
    day = day or today()
    row = conn.execute(
        "SELECT * FROM write_policy_ledger WHERE domain=? AND effective_date=?",
        (DOMAIN, day),
    ).fetchone()
    if row is not None:
        return dict(row)
    conn.execute(
        """INSERT INTO write_policy_ledger(domain,policy,effective_date,note,created_at)
           VALUES (?,?,?,?,?)""",
        (DOMAIN, IMPLEMENTED_POLICY, day, SELECTED_PATH_LABEL, _now()),
    )
    row = conn.execute(
        "SELECT * FROM write_policy_ledger WHERE domain=? AND effective_date=?",
        (DOMAIN, day),
    ).fetchone()
    return dict(row)


def registered_policy(conn, day: str | None = None) -> str:
    return ensure_registered(conn, day)["policy"]


def request_change(conn, policy: str, actor: str) -> tuple[bool, dict]:
    """当日禁止改口。同值确认幂等；任何异值都记「禁写偏了」并拒绝。"""
    from db import write_audit

    current = ensure_registered(conn)
    if policy not in (POLICY_LATE_BUCKET, POLICY_REJECT) or policy != current["policy"]:
        write_audit(
            conn,
            actor,
            "write_policy_deviation",
            DOMAIN,
            {
                "note": DEVIATION_NOTE,
                "requested": policy,
                "registered": current["policy"],
                "rule": "当天禁止改口",
            },
        )
        return False, current
    write_audit(
        conn,
        actor,
        "write_policy_confirm",
        DOMAIN,
        {"policy": policy, "effective_date": current["effective_date"]},
    )
    return True, current


def seal_window(conn, window, actor: str) -> dict:
    """关窗时把当日口径钉到「窗 + 世代」，形成关窗禁写入册。"""
    from db import write_audit

    base = ensure_registered(conn)
    row = conn.execute(
        "SELECT * FROM window_seal_ledger WHERE window_id=? AND generation=?",
        (window["id"], window["generation"]),
    ).fetchone()
    if row is not None:
        return dict(row)
    conn.execute(
        """INSERT INTO window_seal_ledger(
               window_id,generation,policy,effective_date,seal_token,created_at)
           VALUES (?,?,?,?,?,?)""",
        (
            window["id"],
            window["generation"],
            base["policy"],
            base["effective_date"],
            window["seal_token"] if "seal_token" in window.keys() else None,
            _now(),
        ),
    )
    row = conn.execute(
        "SELECT * FROM window_seal_ledger WHERE window_id=? AND generation=?",
        (window["id"], window["generation"]),
    ).fetchone()
    write_audit(
        conn,
        actor,
        "seal_policy_seal",
        str(window["id"]),
        {
            "window": window["name"],
            "generation": window["generation"],
            "policy": base["policy"],
            "effective_date": base["effective_date"],
            "note": "关窗禁写入册：当日口径已钉死",
        },
    )
    return dict(row)


def window_policy(conn, window) -> dict:
    """已关窗的生效口径：优先窗+世代入册记录，缺失则回落到当日写入册。"""
    row = conn.execute(
        """SELECT * FROM window_seal_ledger
           WHERE window_id=? AND generation=? ORDER BY id DESC LIMIT 1""",
        (window["id"], window["generation"]),
    ).fetchone()
    if row is not None:
        return dict(row)
    base = ensure_registered(conn)
    return {
        "window_id": window["id"],
        "generation": window["generation"],
        "policy": base["policy"],
        "effective_date": base["effective_date"],
        "seal_token": None,
        "sealed": False,
    }


def backfill_closed_windows(conn) -> None:
    """母仓既有已关闭窗也要入册，避免历史窗出现无册可依的暗写。"""
    rows = conn.execute("SELECT * FROM windows WHERE status='已关闭'").fetchall()
    for w in rows:
        seal_window(conn, w, "system")
