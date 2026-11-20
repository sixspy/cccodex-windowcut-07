"""旁路暂存：关窗切版时新到事件进 hold，并按择定写路径晋升正式。

写路径口径（本仓择定，当天禁改口）：
- 甲路径【本仓采用】：插入正式窗内事件 + hold 置「已晋升」同库同事务提交；
  提交失败则整体回滚——正式零增、hold 保持暂存。
- 乙路径（影子晋升单 hold_promote_draft 原子切换）：与甲互斥；晋升册已择甲后，
  任何乙路径调用一律拒写并记审计「晋升偏了」，草稿作废、两侧不动。
两路晋升同一 hold_id 争用暂存锁 hold-promote:{id}，后到只记争用、不写业务。
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from auth import AuthHeader, require_user
from audit_svc import promotion_crosscheck
from db import connect, write_audit
from ingest_svc import insert_formal_event
from locks import release, require_lock
from models import HoldIn

router = APIRouter(tags=["hold"])

PATH_A = "甲"  # 同事务晋升清桶（本仓择定）
PATH_B = "乙"  # 影子晋升单原子切换（互斥，偏册即拒）


def _today() -> str:
    return datetime.now().date().isoformat()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class DraftIn(BaseModel):
    biz_key: str
    event_ts: str | None = None
    amount: float


def _registered_path(conn, day: str):
    return conn.execute(
        "SELECT * FROM hold_promotion_registry WHERE register_day=?", (day,)
    ).fetchone()


def _reject_deviation(actor: str, hold_id: int, requested: str, registered_row) -> None:
    """偏离暂存晋升册：独立事务拒写并记「晋升偏了」，不动两侧业务。"""
    with connect() as dev_conn:
        write_audit(
            dev_conn,
            actor,
            "hold_promote_deviate",
            str(hold_id),
            {
                "requested_path": requested,
                "registered_path": registered_row["path"],
                "register_day": registered_row["register_day"],
                "note": "晋升偏了：偏离暂存晋升册，拒写，两侧不动",
            },
        )
        dev_conn.commit()
    raise HTTPException(409, "晋升偏了：写路径与暂存晋升册不符，已拒写并审计")


@router.get("/api/holds")
def list_holds(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM hold_staging WHERE status='暂存' ORDER BY id DESC LIMIT 200"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.post("/api/holds")
def create_hold(body: HoldIn, authorization: AuthHeader = None):
    user = require_user(authorization)
    lock_key = f"hold:{body.window_name}"
    with connect() as conn:
        require_lock(conn, lock_key, user)
        try:
            w = conn.execute(
                "SELECT * FROM windows WHERE name=?", (body.window_name,)
            ).fetchone()
            if not w:
                raise HTTPException(404, "窗口不存在")
            now = _now()
            cur = conn.execute(
                """INSERT INTO hold_staging(window_id,biz_key,event_ts,amount,status,created_at)
                   VALUES (?,?,?,?,?,?)""",
                (w["id"], body.biz_key, body.event_ts, body.amount, "暂存", now),
            )
            write_audit(
                conn,
                user,
                "hold_create",
                body.biz_key,
                {"window_id": w["id"], "hold_id": cur.lastrowid},
            )
            conn.commit()
            return {"ok": True, "hold_id": cur.lastrowid, "status": "暂存"}
        finally:
            release(conn, lock_key, user)
            conn.commit()


@router.post("/api/holds/{hold_id}/promote")
def promote_hold(hold_id: int, authorization: AuthHeader = None):
    """甲路径：同事务晋升清桶。正式事件与 hold 状态同库提交，失败全回滚。"""
    user = require_user(authorization)
    day = _today()
    # 偏册先拦：册内已择路径且非甲，直接拒写记偏（不占晋升锁、不碰业务）
    with connect() as gate:
        registered = _registered_path(gate, day)
    if registered is not None and registered["path"] != PATH_A:
        _reject_deviation(user, hold_id, PATH_A, registered)

    lock_key = f"hold-promote:{hold_id}"
    with connect() as conn:
        require_lock(conn, lock_key, user)  # 后到只记争用，HTTP 409
        try:
            hold = conn.execute(
                "SELECT * FROM hold_staging WHERE id=?", (hold_id,)
            ).fetchone()
            if not hold:
                raise HTTPException(404, "暂存不存在")
            if hold["status"] != "暂存":
                raise HTTPException(409, "该暂存笔不在暂存状态，禁止重复晋升")

            # 同事务：① 正式窗内事件（与入账共用唯一写入口）② hold 清桶 ③ 晋升册择定
            event_id = insert_formal_event(
                conn,
                hold["window_id"],
                hold["biz_key"],
                hold["event_ts"],
                hold["amount"],
                hold_id=hold_id,
            )
            conn.execute(
                "UPDATE hold_staging SET status='已晋升', promoted_at=? WHERE id=?",
                (_now(), hold_id),
            )
            if registered is None:
                conn.execute(
                    """INSERT INTO hold_promotion_registry(register_day,hold_id,path,actor,created_at)
                       VALUES (?,?,?,?,?)""",
                    (day, hold_id, PATH_A, user, _now()),
                )
            write_audit(
                conn,
                user,
                "hold_promote",
                str(hold_id),
                {
                    "path": PATH_A,
                    "event_id": event_id,
                    "biz_key": hold["biz_key"],
                    "amount": hold["amount"],
                    "window_id": hold["window_id"],
                    "note": "同事务晋升清桶：正式事件与hold状态同库提交",
                },
            )
            try:
                conn.commit()  # 提交失败 -> 异常外抛，整笔回滚，两侧不动
            except Exception as exc:  # noqa: BLE001
                conn.rollback()
                write_audit(
                    conn,
                    user,
                    "hold_promote_fail",
                    str(hold_id),
                    {"path": PATH_A, "reason": str(exc)},
                )
                conn.commit()
                raise HTTPException(500, "晋升提交失败，已回滚：正式零增、hold 保持暂存") from exc
            return {
                "ok": True,
                "path": PATH_A,
                "hold_id": hold_id,
                "event_id": event_id,
                "status": "已晋升",
            }
        finally:
            release(conn, lock_key, user)
            conn.commit()


@router.post("/api/holds/{hold_id}/promote-draft")
def promote_hold_draft(hold_id: int, body: DraftIn, authorization: AuthHeader = None):
    """乙路径入口（影子晋升单原子切换）。

    与甲互斥：晋升册当天已择甲即偏册，拒写并记「晋升偏了」；
    不产生有效草稿、不改正式 events、不动 hold 状态。
    """
    user = require_user(authorization)
    day = _today()
    lock_key = f"hold-promote:{hold_id}"
    with connect() as conn:
        require_lock(conn, lock_key, user)
        try:
            hold = conn.execute(
                "SELECT * FROM hold_staging WHERE id=?", (hold_id,)
            ).fetchone()
            if not hold:
                raise HTTPException(404, "暂存不存在")
            registered = _registered_path(conn, day)
            if registered is not None and registered["path"] != PATH_B:
                # 金额键先校验，再判偏册：校验不过本身就是废票
                if body.biz_key != hold["biz_key"] or abs(body.amount - hold["amount"]) > 1e-9:
                    raise HTTPException(400, "草稿金额键与暂存笔不符，草稿作废")
                conn.execute(
                    """INSERT INTO hold_promote_draft(hold_id,biz_key,event_ts,amount,status,created_at)
                       VALUES (?,?,?,?,?,?)""",
                    (hold_id, hold["biz_key"], hold["event_ts"], hold["amount"], "作废", _now()),
                )
                write_audit(
                    conn,
                    user,
                    "hold_promote_deviate",
                    str(hold_id),
                    {
                        "requested_path": PATH_B,
                        "registered_path": registered["path"],
                        "register_day": registered["register_day"],
                        "note": "晋升偏了：影子晋升单与册子不符，草稿作废，两侧不动",
                    },
                )
                conn.commit()
                raise HTTPException(409, "晋升偏了：乙路径与暂存晋升册不符，草稿作废")
            # 理论不可达：本仓构建即择甲，乙路径永不开放
            raise HTTPException(409, "本仓写路径已择甲，乙路径不开放")
        finally:
            release(conn, lock_key, user)
            conn.commit()


@router.get("/api/holds/promotion-registry")
def promotion_registry(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM hold_promotion_registry ORDER BY id DESC LIMIT 100"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.get("/api/holds/promotion-crosscheck")
def hold_promotion_crosscheck(authorization: AuthHeader = None):
    require_user(authorization)
    return promotion_crosscheck()


@router.post("/api/holds/{hold_id}/release-stub")
def release_hold_stub(hold_id: int, authorization: AuthHeader = None):
    """兼容保留的旧空桩：仅登记意图，不落地写路径（晋升请走 /promote）。"""
    user = require_user(authorization)
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM hold_staging WHERE id=?", (hold_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "暂存不存在")
        write_audit(
            conn,
            user,
            "hold_release_stub",
            str(hold_id),
            {
                "paths": ["晋升正式同事务", "作废暂存零改正式"],
                "note": "空桩保留：正式写路径见 /api/holds/{id}/promote（甲）",
            },
        )
        conn.commit()
    return {"ok": True, "stub": True}
