"""暂存晋升写路径红测（先红后绿，禁止跳过）。

覆盖题面五条硬约束：
1. 撕裂夹具（双空 / 双挂）必须被五处同态巡检当场抓出；
2. 正确晋升只留一处：正式 events 增、hold 出暂存、五处同态；
3. 同库提交失败 -> 正式零增、hold 保持暂存（甲路径原子性）；
4. 两路同时晋升同一 hold_id：占暂存锁，后到只记争用、不写业务；
5. 写路径择一入册、当天禁改口；偏离暂存晋升册拒写并记「晋升偏了」。
"""
from __future__ import annotations

import json
import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

import db
from app import app, init_db
from audit_svc import promotion_crosscheck

TEAR_FIXTURE = db.FIXTURES / "hold_tear_cases.json"


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "promote.db")
    init_db()
    yield


def _auth(c: TestClient, username: str = "admin", password: str = "123456") -> dict:
    tok = c.post(
        "/api/login", json={"username": username, "password": password}
    ).json()["token"]
    return {"Authorization": "Bearer " + tok}


def _create_hold(c: TestClient, h: dict, biz: str, amount: float) -> int:
    r = c.post(
        "/api/holds",
        headers=h,
        json={
            "window_name": "W-1102",
            "biz_key": biz,
            "event_ts": "2026-11-02T12:00:00",
            "amount": amount,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()["hold_id"]


def _seed_tear_cases() -> dict[str, dict]:
    """按 fixtures/hold_tear_cases.json 造晋升撕裂夹具，返回 case->行定位。"""
    cases = json.loads(TEAR_FIXTURE.read_text(encoding="utf-8"))
    located: dict[str, dict] = {}
    with db.connect() as conn:
        w = conn.execute("SELECT * FROM windows WHERE name='W-1102'").fetchone()
        for case in cases:
            spec = case["hold"]
            cur = conn.execute(
                """INSERT INTO hold_staging(window_id,biz_key,event_ts,amount,status,created_at,promoted_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    w["id"],
                    spec["biz_key"],
                    spec["event_ts"],
                    spec["amount"],
                    spec["status"],
                    "2026-11-02T09:00:01",
                    "2026-11-02T09:05:00" if spec["status"] == "已晋升" else None,
                ),
            )
            hold_id = cur.lastrowid
            event_id = None
            if case["event"]:
                ec = conn.execute(
                    """INSERT INTO events(window_id,biz_key,event_ts,amount,disposition,hold_id,created_at)
                       VALUES (?,?,?,?,?,?,?)""",
                    (
                        w["id"],
                        spec["biz_key"],
                        spec["event_ts"],
                        spec["amount"],
                        case["event"]["disposition"],
                        hold_id,
                        "2026-11-02T09:05:00",
                    ),
                )
                event_id = ec.lastrowid
            located[case["case"]] = {
                "hold_id": hold_id,
                "event_id": event_id,
                "biz_key": spec["biz_key"],
            }
        conn.commit()
    return located


def _hold_row(hold_id: int) -> sqlite3.Row:
    with db.connect() as conn:
        return conn.execute(
            "SELECT * FROM hold_staging WHERE id=?", (hold_id,)
        ).fetchone()


def _formal_event_count(hold_id: int) -> int:
    with db.connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) c FROM events WHERE hold_id=? AND disposition='窗内'",
            (hold_id,),
        ).fetchone()["c"]


# ---------------------------------------------------------------- 红测一：撕裂

def test_tear_fixtures_double_empty_and_double_hung_are_detected():
    """夹具复现：双空与双挂各造一笔，五处同态巡检必须双双见红。"""
    located = _seed_tear_cases()

    report = promotion_crosscheck()
    by_biz = {v["biz_key"]: v for v in report["violations"]}

    empty = by_biz["TEAR-EMPTY"]
    assert empty["kind"] == "双空"
    assert empty["hold_status"] == "已晋升"  # 桶已清（不在活跃暂存口径）
    assert empty["in_hold_active"] is False
    assert empty["formal_count"] == 0

    hung = by_biz["TEAR-DOUBLE"]
    assert hung["kind"] == "双挂"
    assert hung["hold_status"] == "暂存"  # 仍挂活跃暂存
    assert hung["in_hold_active"] is True
    assert hung["formal_count"] == 1

    # 同态巡检经 HTTP 只读面同样可查（审计页口径）
    c = TestClient(app)
    h = _auth(c)
    r = c.get("/api/holds/promotion-crosscheck", headers=h)
    assert r.status_code == 200
    assert r.json()["consistent"] is False
    assert {v["kind"] for v in r.json()["violations"]} == {"双空", "双挂"}


def test_healthy_promotion_leaves_exactly_one_record_and_five_faces_align():
    """正确晋升：正式与暂存只留一处，hold 列表/正式事件/窗口详情/对账/审计五处同态。"""
    c = TestClient(app)
    h = _auth(c)
    hold_id = _create_hold(c, h, "HOLD-OK", 12.34)

    before = c.get("/api/windows/2", headers=h).json()
    r = c.post(f"/api/holds/{hold_id}/promote", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["path"] == "甲"
    assert body["event_id"] > 0

    # 正式与暂存只留一处：正式 events 恰增一笔，hold 已出暂存
    assert _formal_event_count(hold_id) == 1
    row = _hold_row(hold_id)
    assert row["status"] == "已晋升"
    assert row["promoted_at"]
    assert row["window_id"] is not None  # 物理行留痕但不在活跃暂存口径内
    active = c.get("/api/holds", headers=h).json()["items"]
    assert all(x["id"] != hold_id for x in active)

    # 五处同态
    report = promotion_crosscheck()
    assert report["consistent"] is True
    assert report["violations"] == []

    detail = c.get("/api/windows/2", headers=h).json()
    assert detail["in_window_count"] == before["in_window_count"] + 1
    assert abs(detail["in_window_amount"] - (before["in_window_amount"] + 12.34)) < 1e-9

    recon = c.get("/api/reconcile/2", headers=h).json()
    assert recon["window_events"] == detail["in_window_count"]
    assert abs(recon["window_amount"] - detail["in_window_amount"]) < 1e-9

    events = c.get("/api/events", headers=h).json()["items"]
    ev = next(e for e in events if e["hold_id"] == hold_id)
    assert ev["disposition"] == "窗内"
    assert ev["biz_key"] == "HOLD-OK"

    audits = c.get("/api/audit?limit=50", headers=h).json()["items"]
    assert any(a["action"] == "hold_promote" and a["target"] == str(hold_id) for a in audits)


def test_promote_commit_failure_rolls_back_both_sides():
    """甲路径原子性：同库提交失败 -> 正式零增、hold 保持暂存，重试仍可成功。"""
    c = TestClient(app)
    h = _auth(c)
    hold_id = _create_hold(c, h, "HOLD-FAIL", 5.0)

    import hold_svc

    real_connect = db.connect
    state = {"fired": False}

    class FlakyConn:
        """连接包装器：门检（第 1 条连接）之后的晋升事务连接，首个 commit 注入故障。"""

        seq = 0

        def __init__(self, raw):
            self._raw = raw
            FlakyConn.seq += 1
            self.no = FlakyConn.seq

        def __getattr__(self, name):
            return getattr(self._raw, name)

        def commit(self):
            if self.no >= 2 and not state["fired"]:
                state["fired"] = True
                self._raw.rollback()
                raise sqlite3.OperationalError("模拟同库提交失败（夹具）")
            return self._raw.commit()

        def rollback(self):
            return self._raw.rollback()

        def __enter__(self):
            self._raw.__enter__()
            return self

        def __exit__(self, *exc):
            return self._raw.__exit__(*exc)

    hold_svc.connect = lambda: FlakyConn(real_connect())
    r = c.post(f"/api/holds/{hold_id}/promote", headers=h)
    assert r.status_code == 500
    hold_svc.connect = real_connect
    assert state["fired"] is True

    # 两侧不动：正式零增、hold 保持暂存
    assert _formal_event_count(hold_id) == 0
    assert _hold_row(hold_id)["status"] == "暂存"
    with db.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) c FROM hold_promotion_registry"
        ).fetchone()["c"] == 0
        assert conn.execute(
            "SELECT COUNT(*) c FROM audit_trail WHERE action='hold_promote'"
        ).fetchone()["c"] == 0

    # 故障消除后同笔仍可正常晋升，晋升后五处同态
    r2 = c.post(f"/api/holds/{hold_id}/promote", headers=h)
    assert r2.status_code == 200, r2.text
    assert _formal_event_count(hold_id) == 1
    assert _hold_row(hold_id)["status"] == "已晋升"
    assert promotion_crosscheck()["consistent"] is True


# ---------------------------------------------------------------- 红测二：争用

def test_two_actors_promoting_same_hold_only_one_wins_and_contention_logged():
    """两路同时晋升同一 hold_id：占暂存锁，后到只记争用，不写业务。"""
    c = TestClient(app)
    h_admin = _auth(c)
    h_closer = _auth(c, "closer", "close123")
    hold_id = _create_hold(c, h_admin, "HOLD-RACE", 9.9)

    # 确定性面：先到持锁，后到 409 且只记争用
    lr = c.post(
        "/api/locks/acquire",
        headers=h_closer,
        json={"lock_key": f"hold-promote:{hold_id}"},
    )
    assert lr.status_code == 200
    blocked = c.post(f"/api/holds/{hold_id}/promote", headers=h_admin)
    assert blocked.status_code == 409
    assert _formal_event_count(hold_id) == 0
    assert _hold_row(hold_id)["status"] == "暂存"
    contention = c.get("/api/contention", headers=h_admin).json()["items"]
    assert any(x["lock_key"] == f"hold-promote:{hold_id}" for x in contention)
    c.post(
        "/api/locks/release",
        headers=h_closer,
        json={"lock_key": f"hold-promote:{hold_id}"},
    )

    # 并发面：双线程同时撞同一笔，恰一笔成功
    outcomes: list[int] = []
    barrier = threading.Barrier(2)

    def worker(headers: dict) -> None:
        cc = TestClient(app)
        barrier.wait()
        rr = cc.post(f"/api/holds/{hold_id}/promote", headers=headers)
        outcomes.append(rr.status_code)

    t1 = threading.Thread(target=worker, args=(h_admin,))
    t2 = threading.Thread(target=worker, args=(h_closer,))
    t1.start()
    t2.start()
    t1.join(timeout=30)
    t2.join(timeout=30)

    assert sorted(outcomes) == [200, 409]
    assert _formal_event_count(hold_id) == 1  # 后到未写业务，正式只增一笔
    assert _hold_row(hold_id)["status"] == "已晋升"
    assert promotion_crosscheck()["consistent"] is True


# ----------------------------------------------------------- 晋升册与偏离拒写

def test_path_registry_pinned_for_day_and_deviation_rejected():
    """写路径择一入册当天禁改口；偏离暂存晋升册拒写并记「晋升偏了」，草稿作废两侧不动。"""
    c = TestClient(app)
    h = _auth(c)
    hold_id = _create_hold(c, h, "HOLD-PATH", 2.0)

    # 首次晋升择定甲路径并写入暂存晋升册
    r = c.post(f"/api/holds/{hold_id}/promote", headers=h)
    assert r.status_code == 200
    with db.connect() as conn:
        reg = conn.execute(
            "SELECT * FROM hold_promotion_registry ORDER BY id"
        ).fetchall()
        assert len(reg) == 1
        assert reg[0]["path"] == "甲"
        assert reg[0]["hold_id"] == hold_id

    # 当天改口走乙路径（影子晋升单）：拒写、记「晋升偏了」、两侧不动、草稿零行
    other = _create_hold(c, h, "HOLD-DEVIATE", 4.0)
    dev = c.post(
        f"/api/holds/{other}/promote-draft",
        headers=h,
        json={"amount": 4.0, "biz_key": "HOLD-DEVIATE"},
    )
    assert dev.status_code == 409
    with db.connect() as conn:
        audit = conn.execute(
            "SELECT * FROM audit_trail WHERE action='hold_promote_deviate'"
        ).fetchall()
        assert len(audit) == 1
        payload = json.loads(audit[0]["payload"])
        assert payload["requested_path"] == "乙"
        assert payload["registered_path"] == "甲"
        assert conn.execute(
            "SELECT COUNT(*) c FROM hold_promote_draft WHERE status='有效'"
        ).fetchone()["c"] == 0
        assert conn.execute(
            "SELECT COUNT(*) c FROM hold_promotion_registry"
        ).fetchone()["c"] == 1  # 册子未被改口
    assert _hold_row(other)["status"] == "暂存"
    assert _formal_event_count(other) == 0

    # 册面只读可查（前端审计页同口径）
    reg_view = c.get("/api/holds/promotion-registry", headers=h).json()["items"]
    assert [x["path"] for x in reg_view] == ["甲"]
    assert promotion_crosscheck()["consistent"] is True


def test_concurrent_lock_acquisition_race_never_double_holds():
    """两路同时抢 hold-promote 锁、读到同一空快照后并发写：不得 500，须退化为只记争用。

    确定性复现交错：WAL 下 b 先固定旧快照（读不到锁行、且读不阻塞 a 的写），
    a 随后插锁并提交，b 再 try_acquire 时 INSERT 必然撞 SQLITE_BUSY_SNAPSHOT/UNIQUE。
    正确行为是回滚过期快照、重读确认持有者、登记争用并返回 False，
    而不是把数据库异常抛成 500，也不得顶掉先到者的锁。
    """
    import locks

    with db.connect() as pragma_conn:
        pragma_conn.execute("PRAGMA journal_mode=WAL")

    key = "hold-promote:77"
    with db.connect() as a, db.connect() as b:
        b.execute("BEGIN")  # 固定 b 的旧读快照（WAL 下不阻塞 a 写）
        assert (
            b.execute(
                "SELECT holder FROM window_locks WHERE lock_key=?", (key,)
            ).fetchone()
            is None
        )
        assert locks.try_acquire(a, key, "admin") is True
        a.commit()  # 持锁行在 b 的快照之外落盘

        # 未加固实现：b 走 INSERT 抛 OperationalError/IntegrityError；加固后应为 False
        assert locks.try_acquire(b, key, "closer") is False
        b.commit()

    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM contention_log WHERE lock_key=?", (key,)
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["actor"] == "closer"
        holders = conn.execute(
            "SELECT holder FROM window_locks WHERE lock_key=?", (key,)
        ).fetchall()
        assert [r["holder"] for r in holders] == ["admin"]  # 锁未被第二路顶掉


def test_promoting_unknown_or_non_staged_hold_is_rejected_without_writes():
    c = TestClient(app)
    h = _auth(c)
    assert c.post("/api/holds/9999/promote", headers=h).status_code == 404

    hold_id = _create_hold(c, h, "HOLD-ONCE", 1.0)
    assert c.post(f"/api/holds/{hold_id}/promote", headers=h).status_code == 200
    again = c.post(f"/api/holds/{hold_id}/promote", headers=h)
    assert again.status_code == 409
    assert _formal_event_count(hold_id) == 1  # 无重复晋升
