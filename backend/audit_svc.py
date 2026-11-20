"""审计与互证只读旁路。"""
from __future__ import annotations

from fastapi import APIRouter

from auth import AuthHeader, require_user
from db import connect

router = APIRouter(tags=["audit"])


def promotion_crosscheck():
    """暂存晋升五处同态巡检。

    同态口径（hold 列表 / 正式事件 / 窗口详情 / 对账互证 / 审计共一份事实）：
    - 状态「已晋升」的 hold 必须在正式 events 恰有 1 笔 disposition='窗内' 且
      hold_id 回指；否则为「双空」（暂存已清正式未增）；
    - 状态「暂存」的 hold 不得已有正式窗内事件；否则为「双挂」（正式已增暂存仍挂）；
    - 正式事件按 hold_id 不得重复落账。
    """
    violations: list[dict] = []
    with connect() as conn:
        holds = conn.execute("SELECT * FROM hold_staging ORDER BY id").fetchall()
        for h in holds:
            formal = conn.execute(
                """SELECT id, window_id FROM events
                   WHERE hold_id=? AND disposition='窗内'""",
                (h["id"],),
            ).fetchall()
            count = len(formal)
            in_active = h["status"] == "暂存"
            if h["status"] == "已晋升" and count != 1:
                violations.append(
                    {
                        "kind": "双空",
                        "hold_id": h["id"],
                        "biz_key": h["biz_key"],
                        "hold_status": h["status"],
                        "in_hold_active": in_active,
                        "formal_count": count,
                        "detail": "暂存标记已晋升但正式窗内事件零增（或重复）",
                    }
                )
            elif h["status"] == "暂存" and count > 0:
                violations.append(
                    {
                        "kind": "双挂",
                        "hold_id": h["id"],
                        "biz_key": h["biz_key"],
                        "hold_status": h["status"],
                        "in_hold_active": in_active,
                        "formal_count": count,
                        "detail": "正式窗内事件已增但暂存仍挂未清",
                    }
                )
            elif count > 1:
                violations.append(
                    {
                        "kind": "重复落账",
                        "hold_id": h["id"],
                        "biz_key": h["biz_key"],
                        "hold_status": h["status"],
                        "in_hold_active": in_active,
                        "formal_count": count,
                        "detail": "同一暂存笔晋升出多笔正式事件",
                    }
                )
        orphan = conn.execute(
            """SELECT e.id, e.hold_id, e.biz_key FROM events e
               LEFT JOIN hold_staging h ON h.id=e.hold_id
               WHERE e.disposition='窗内' AND e.hold_id IS NOT NULL AND h.id IS NULL"""
        ).fetchall()
        for e in orphan:
            violations.append(
                {
                    "kind": "孤儿正式事件",
                    "hold_id": e["hold_id"],
                    "biz_key": e["biz_key"],
                    "hold_status": None,
                    "in_hold_active": False,
                    "formal_count": 1,
                    "detail": "正式事件回指的暂存笔不存在",
                }
            )
    return {
        "consistent": not violations,
        "violations": violations,
        "checked_holds": len(holds),
    }


@router.get("/api/audit")
def list_audit(authorization: AuthHeader = None, limit: int = 100):
    require_user(authorization)
    limit = max(1, min(limit, 500))
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_trail ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.get("/api/audit/actions")
def audit_actions(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            """SELECT action, COUNT(*) c FROM audit_trail
               GROUP BY action ORDER BY c DESC"""
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.get("/api/dashboard")
def dashboard(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        open_n = conn.execute(
            "SELECT COUNT(*) c FROM windows WHERE status='开启'"
        ).fetchone()["c"]
        closed_n = conn.execute(
            "SELECT COUNT(*) c FROM windows WHERE status='已关闭'"
        ).fetchone()["c"]
        late_n = conn.execute("SELECT COUNT(*) c FROM late_bucket").fetchone()["c"]
        reject_n = conn.execute("SELECT COUNT(*) c FROM reject_slips").fetchone()["c"]
        hold_n = conn.execute(
            "SELECT COUNT(*) c FROM hold_staging WHERE status='暂存'"
        ).fetchone()["c"]
        mats = conn.execute("SELECT COUNT(*) c FROM materializations").fetchone()["c"]
        contention = conn.execute("SELECT COUNT(*) c FROM contention_log").fetchone()["c"]
        return {
            "open_windows": open_n,
            "closed_windows": closed_n,
            "late_bucket": late_n,
            "rejects": reject_n,
            "holds": hold_n,
            "materializations": mats,
            "contention": contention,
        }
