"""H05 回归：探头列与温度列在任何一层都不得对调。

四层各自独立断言：
1. 写入：INSERT 绑定参数 probe_id 列收探头编号、temp_c 列收温度数值；
2. 投影读出：GET 列表按列名原样投影，不做任何互换反算；
3. 判定：worker 按温度列判定（4.2 合格 / 12.5 超温）；
4. 列表渲染：前端表头“探头”绑 probe_id、“温度”绑 temp_c。

另含：非法/越权提交不落任何行；值班员令牌即使自带 role=writer 仍被拒写。
"""

import asyncio
import os
from datetime import datetime, timezone

import jwt
from aiohttp.test_utils import TestClient, TestServer

import api
import worker
from api import create_app

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND_APP = os.path.join(
    os.path.dirname(BACKEND_DIR), "frontend", "src", "App.jsx"
)

SAMPLES = [("探头A01", 4.2), ("探头B02", 12.5)]


class FakeRow:
    def __init__(self, mapping):
        self._m = mapping

    def __getitem__(self, key):
        return self._m[key]


def _seed_row(probe_id, temp_c, rid, verdict, reason, status="done"):
    return {
        "id": rid,
        "probe_id": probe_id,
        "temp_c": temp_c,
        "verdict": verdict,
        "reason": reason,
        "status": status,
        "created_by": "logger",
        "created_at": datetime(2026, 10, 2, tzinfo=timezone.utc),
        "processed_at": datetime(2026, 10, 2, tzinfo=timezone.utc),
    }


class FakePool:
    """记录 SQL 与绑定参数，绝不接触真实数据库。"""

    def __init__(self, rows):
        self.rows = rows
        self.fetch_calls = 0
        self.insert_sql = None
        self.insert_args = None

    async def fetch(self, sql):
        self.fetch_calls += 1
        return [FakeRow(dict(r)) for r in self.rows]

    async def fetchrow(self, sql, *args):
        self.insert_sql = sql
        self.insert_args = args
        row = _seed_row(
            args[0],
            args[1],
            rid=99,
            verdict=None,
            reason=None,
            status="pending",
        )
        row["created_by"] = args[2]
        row["processed_at"] = None
        self.rows.insert(0, row)
        return FakeRow(row)


def _make_client(pool):
    app = create_app()
    # 测试不连库：去掉建池/建表/种子启动钩子，直接注入假连接池。
    app.on_startup.clear()
    app.on_cleanup.clear()
    app["pool"] = pool
    return TestClient(TestServer(app))


def _run(coro_factory):
    async def driver():
        pool = FakePool(
            [
                _seed_row("探头A01", 4.2, 1, "合格", "探头温度未超过 8℃ 上限"),
                _seed_row("探头B02", 12.5, 2, "超温", "探头温度超过 8℃ 冷链上限"),
            ]
        )
        async with _make_client(pool) as client:
            return await coro_factory(client, pool)

    return asyncio.run(driver())


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# 1) 写入层：探头列只装探头，温度列只装温度
# ---------------------------------------------------------------------------

def test_write_binds_columns_without_swap():
    async def case(client, pool):
        tok = (await (await client.post(
            "/api/auth/login",
            json={"username": "logger", "password": "log123456"},
        )).json())["access_token"]
        for probe_id, temp_c in SAMPLES:
            res = await client.post(
                "/api/readings",
                headers=_auth(tok),
                json={"probe_id": probe_id, "temp_c": temp_c},
            )
            assert res.status == 201, await res.text()
            data = await res.json()
            # 返回体也必须按列对齐
            assert data["probe_id"] == probe_id
            assert data["temp_c"] == temp_c
            # INSERT 绑定：$1=探头编号（文本），$2=温度（数值），$3=提交人
            assert pool.insert_args[0] == probe_id
            assert pool.insert_args[1] == temp_c
            assert isinstance(pool.insert_args[1], float)
        return True

    assert _run(case)


def test_invalid_submit_leaves_no_row():
    async def case(client, pool):
        tok = (await (await client.post(
            "/api/auth/login",
            json={"username": "logger", "password": "log123456"},
        )).json())["access_token"]

        n_before = len(pool.rows)
        bad_bodies = [
            {"probe_id": "", "temp_c": 4.2},
            {"probe_id": "探头X09", "temp_c": "不是数字"},
            {"probe_id": "探头X09", "temp_c": None},
        ]
        for body in bad_bodies:
            res = await client.post(
                "/api/readings", headers=_auth(tok), json=body
            )
            assert res.status == 400, (body, await res.text())
        # 任何失败提交都不得残留行（更不得残留错位行）
        assert pool.insert_args is None
        assert len(pool.rows) == n_before
        return True

    assert _run(case)


# ---------------------------------------------------------------------------
# 2) 投影读出层：列名原样，不做互换反算
# ---------------------------------------------------------------------------

def test_read_projection_preserves_columns():
    async def case(client, pool):
        tok = (await (await client.post(
            "/api/auth/login",
            json={"username": "watcher", "password": "watch123456"},
        )).json())["access_token"]
        res = await client.get("/api/readings", headers=_auth(tok))
        assert res.status == 200
        data = await res.json()
        by_probe = {r["probe_id"]: r for r in data}
        assert set(by_probe) == {"探头A01", "探头B02"}
        # 甲探配四点二四面对齐：探头A01 的温度必须仍是 4.2
        assert by_probe["探头A01"]["temp_c"] == 4.2
        assert by_probe["探头A01"]["verdict"] == "合格"
        # 乙探配十二点五：探头B02 的温度必须仍是 12.5
        assert by_probe["探头B02"]["temp_c"] == 12.5
        assert by_probe["探头B02"]["verdict"] == "超温"
        # 温度列绝不能混入探头编号文本
        for r in data:
            assert isinstance(r["temp_c"], (int, float))
        return True

    assert _run(case)


# ---------------------------------------------------------------------------
# 3) 判定层：worker 按温度列判定，种子/提交语义一致
# ---------------------------------------------------------------------------

class _FakeCursor:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class FakeTx:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConn:
    """模拟 psycopg 连接，记录 worker 的 SELECT/UPDATE 语句与参数。"""

    def __init__(self, claimed_row):
        self._claimed_row = claimed_row
        self.executed = []
        self.committed = False

    def transaction(self):
        return FakeTx()

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if sql.lstrip().upper().startswith("SELECT"):
            return _FakeCursor(self._claimed_row)
        return self

    def commit(self):
        self.committed = True


def test_worker_judges_by_temp_column():
    for probe_id, temp_c, verdict in [
        ("探头A01", 4.2, "合格"),
        ("探头B02", 12.5, "超温"),
    ]:
        conn = FakeConn({"id": 7, "probe_id": probe_id, "temp_c": temp_c})
        row = worker.claim_one(conn)
        assert row["probe_id"] == probe_id and row["temp_c"] == temp_c
        worker.finish(conn, row["id"], float(row["temp_c"]))
        updates = [
            (sql, p)
            for sql, p in conn.executed
            if sql.lstrip().upper().startswith("UPDATE")
        ]
        # 认领 UPDATE 置 processing，最后一条 UPDATE 才写结论
        update_sql, params = updates[-1]
        assert params[0] == verdict
        assert params[2] == 7
        assert conn.committed


def test_worker_no_pending_row_returns_none():
    conn = FakeConn(None)
    assert worker.claim_one(conn) is None


# ---------------------------------------------------------------------------
# 4) 权限：值班员只读，伪造 role 声明也不能提权
# ---------------------------------------------------------------------------

def test_writer_can_submit():
    async def case(client, pool):
        tok = (await (await client.post(
            "/api/auth/login",
            json={"username": "logger", "password": "log123456"},
        )).json())["access_token"]
        res = await client.post(
            "/api/readings",
            headers=_auth(tok),
            json={"probe_id": "探头C03", "temp_c": 5.5},
        )
        assert res.status == 201
        return True

    assert _run(case)


def test_watcher_forbidden_and_leaves_no_row():
    async def case(client, pool):
        tok = (await (await client.post(
            "/api/auth/login",
            json={"username": "watcher", "password": "watch123456"},
        )).json())["access_token"]
        n_before = len(pool.rows)

        res = await client.post(
            "/api/readings",
            headers=_auth(tok),
            json={"probe_id": "探头C03", "temp_c": 5.5},
        )
        assert res.status == 403
        assert pool.insert_args is None
        assert len(pool.rows) == n_before

        # 只读接口照常可用
        ok = await client.get("/api/readings", headers=_auth(tok))
        assert ok.status == 200
        return True

    assert _run(case)


def test_forged_writer_role_in_token_still_rejected():
    """用同一密钥给 watcher 签一个 role=writer 的令牌：服务端必须只认账号表。"""
    forged = jwt.encode(
        {"sub": "watcher", "role": "writer"},
        api.SECRET,
        algorithm="HS256",
    )

    async def case(client, pool):
        res = await client.post(
            "/api/readings",
            headers=_auth(forged),
            json={"probe_id": "探头C03", "temp_c": 5.5},
        )
        assert res.status == 403
        assert pool.insert_args is None
        return True

    assert _run(case)


def test_anonymous_cannot_read_or_write():
    async def case(client, pool):
        assert (await client.get("/api/readings")).status == 401
        assert (
            await client.post(
                "/api/readings", json={"probe_id": "探头C03", "temp_c": 5.5}
            )
        ).status == 401
        assert pool.insert_args is None
        return True

    assert _run(case)


# ---------------------------------------------------------------------------
# 5) 渲染层与 trap 清理：前端列绑定恒等，四个对调模块不复存在
# ---------------------------------------------------------------------------

def test_frontend_columns_render_in_order():
    src = open(FRONTEND_APP, encoding="utf-8").read()
    assert "h05-trap-cols" not in src
    tbody = src[src.index("<tbody>") : src.index("</tbody>")]
    i_probe = tbody.index("{r.probe_id}")
    i_temp = tbody.index("{r.temp_c}")
    # 表头顺序为 探头 → 温度℃，数据绑定必须同序，不得互换
    assert 0 < i_probe < i_temp
    # 每列只出现一次，杜绝一列装两个字段
    assert tbody.count("{r.probe_id}") == 1
    assert tbody.count("{r.temp_c}") == 1


def test_swap_trap_modules_removed():
    for name in (
        "probe_temp_swap.py",
        "h05_extra_trap.py",
        "h05_pad_trap.py",
        "h05_render_trap.py",
    ):
        assert not os.path.exists(os.path.join(BACKEND_DIR, name)), name
    api_src = open(os.path.join(BACKEND_DIR, "api.py"), encoding="utf-8").read()
    assert "swap" not in api_src.lower()
    assert "h05" not in api_src.lower()
