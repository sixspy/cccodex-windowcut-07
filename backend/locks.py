"""窗口锁与争用登记（公共并发面，题面争用句应对得上）。"""
from __future__ import annotations

import sqlite3
from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from auth import AuthHeader, require_user
from db import connect, write_audit

router = APIRouter(tags=["locks"])


class LockIn(BaseModel):
    lock_key: str
    action: str = "acquire"


def _log_contention(conn, lock_key: str, holder: str, detail: str) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        """INSERT INTO contention_log(lock_key,actor,action,detail,created_at)
           VALUES (?,?,?,?,?)""",
        (lock_key, holder, "争用", detail, now),
    )
    # 争用登记必须独立落盘：随后业务异常/回滚不得抹掉这条「后到只记争用」
    conn.commit()


def try_acquire(conn, lock_key: str, holder: str) -> bool:
    now = datetime.now().isoformat(timespec="seconds")
    row = conn.execute(
        "SELECT holder FROM window_locks WHERE lock_key=?", (lock_key,)
    ).fetchone()
    if row is not None and row["holder"] == holder:
        return True
    if row is None:
        try:
            conn.execute(
                "INSERT INTO window_locks(lock_key,holder,acquired_at) VALUES (?,?,?)",
                (lock_key, holder, now),
            )
            return True
        except (sqlite3.IntegrityError, sqlite3.OperationalError) as exc:
            # 并发交错：本连接读到空快照，但先到者已插锁（UNIQUE 冲突）
            # 或本连接持有过期快照（SQLITE_BUSY_SNAPSHOT/database is locked）。
            # 回滚过期事务、重读确认持有者，按「后到只记争用」处理，绝不抛 500。
            conn.rollback()
            current = conn.execute(
                "SELECT holder FROM window_locks WHERE lock_key=?", (lock_key,)
            ).fetchone()
            if current is None:
                # 极端情况下持锁行已被释放且无主：重试一次插入；仍失败则抛给调用方
                try:
                    conn.execute(
                        "INSERT INTO window_locks(lock_key,holder,acquired_at) VALUES (?,?,?)",
                        (lock_key, holder, now),
                    )
                    return True
                except (sqlite3.IntegrityError, sqlite3.OperationalError):
                    conn.rollback()
                    current = conn.execute(
                        "SELECT holder FROM window_locks WHERE lock_key=?", (lock_key,)
                    ).fetchone()
            _log_contention(
                conn,
                lock_key,
                holder,
                f"持有者={current['holder'] if current else '未知'}（并发抢锁冲突已退化记争用：{type(exc).__name__}）",
            )
            return False
    _log_contention(conn, lock_key, holder, f"持有者={row['holder']}")
    return False


def release(conn, lock_key: str, holder: str) -> None:
    conn.execute(
        "DELETE FROM window_locks WHERE lock_key=? AND holder=?",
        (lock_key, holder),
    )


def require_lock(conn, lock_key: str, holder: str) -> None:
    if not try_acquire(conn, lock_key, holder):
        raise HTTPException(409, "争用：后到只记争用，不写业务")


@router.get("/api/locks")
def list_locks(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM window_locks ORDER BY acquired_at DESC"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.get("/api/contention")
def list_contention(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM contention_log ORDER BY id DESC LIMIT 100"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.post("/api/locks/acquire")
def acquire_lock(body: LockIn, authorization: AuthHeader = None):
    user = require_user(authorization)
    with connect() as conn:
        ok = try_acquire(conn, body.lock_key, user)
        write_audit(
            conn,
            user,
            "lock_acquire" if ok else "lock_contention",
            body.lock_key,
            {"ok": ok},
        )
        conn.commit()
        if not ok:
            raise HTTPException(409, "争用：后到只记争用")
        return {"ok": True, "lock_key": body.lock_key, "holder": user}


@router.post("/api/locks/release")
def release_lock(body: LockIn, authorization: AuthHeader = None):
    user = require_user(authorization)
    with connect() as conn:
        release(conn, body.lock_key, user)
        write_audit(conn, user, "lock_release", body.lock_key, {})
        conn.commit()
    return {"ok": True}
