"""探头列/温度列对齐回归测试。

覆盖四条链路：组装写入、投影读出、列表渲染、失败复位，
以及权限边界（值班员只读）与判定规则。
"""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
import pytest
from aiohttp import web

import api
import worker
from rules import judge_temp, verdict_for_display

BACKEND_DIR = Path(__file__).resolve().parents[1]
FRONTEND_APP = Path(__file__).resolve().parents[2] / "frontend" / "src" / "App.jsx"


def make_token(username: str, role: str) -> str:
    return jwt.encode(
        {
            "sub": username,
            "role": role,
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        },
        api.SECRET,
        algorithm="HS256",
    )


WRITER_TOKEN = make_token("logger", "writer")
READER_TOKEN = make_token("watcher", "reader")


class FakePool:
    """记录写入参数、回放读取行的内存池。"""

    def __init__(self, row=None, rows=None):
        self.insert_args = None
        self._row = row
        self._rows = rows or []

    async def fetchrow(self, query, *args):
        self.insert_args = args
        return self._row

    async def fetch(self, query):
        return self._rows


class FakeRequest:
    def __init__(self, token=None, body=None, pool=None):
        self.headers = {}
        if token:
            self.headers["Authorization"] = f"Bearer {token}"
        self._body = body
        self.app = {"pool": pool}

    async def json(self):
        return self._body


def make_row(**overrides):
    row = {
        "id": 1,
        "probe_id": "探头A01",
        "temp_c": 4.2,
        "verdict": None,
        "reason": None,
        "status": "pending",
        "created_by": "logger",
        "created_at": datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc),
        "processed_at": None,
    }
    row.update(overrides)
    return row


def run(coro):
    return asyncio.run(coro)


# ---------- 写入：探头列只装探头，温度列只装温度 ----------

@pytest.mark.parametrize(
    "probe_id, temp_c",
    [("探头A01", 4.2), ("探头B02", 12.5)],
)
def test_insert_keeps_columns_aligned(probe_id, temp_c):
    row = make_row(probe_id=probe_id, temp_c=temp_c)
    pool = FakePool(row=row)
    req = FakeRequest(
        token=WRITER_TOKEN,
        body={"probe_id": probe_id, "temp_c": temp_c},
        pool=pool,
    )
    resp = run(api.create_reading(req))
    assert resp.status == 201
    # INSERT 参数顺序：$1=probe_id, $2=temp_c, $3=created_by，不得对调
    assert pool.insert_args == (probe_id, temp_c, "logger")
    payload = json.loads(resp.text)
    assert payload["probe_id"] == probe_id
    assert payload["temp_c"] == temp_c


# ---------- 投影读出：列表接口原样返回，不再二次对调 ----------

def test_list_projection_not_swapped():
    rows = [
        make_row(id=2, probe_id="探头B02", temp_c=12.5, verdict="超温"),
        make_row(id=1, probe_id="探头A01", temp_c=4.2, verdict="合格"),
    ]
    pool = FakePool(rows=rows)
    req = FakeRequest(token=READER_TOKEN, pool=pool)
    resp = run(api.list_readings(req))
    assert resp.status == 200
    payload = json.loads(resp.text)
    assert payload[0]["probe_id"] == "探头B02"
    assert payload[0]["temp_c"] == 12.5
    assert payload[1]["probe_id"] == "探头A01"
    assert payload[1]["temp_c"] == 4.2


# ---------- 权限：值班员只读，不得借机拿到写权限 ----------

def test_watcher_cannot_submit():
    pool = FakePool(row=make_row())
    req = FakeRequest(
        token=READER_TOKEN,
        body={"probe_id": "探头C03", "temp_c": 4.2},
        pool=pool,
    )
    with pytest.raises(web.HTTPForbidden):
        run(api.create_reading(req))
    assert pool.insert_args is None


def test_anonymous_rejected():
    pool = FakePool(row=make_row())
    req = FakeRequest(body={"probe_id": "探头C03", "temp_c": 4.2}, pool=pool)
    with pytest.raises(web.HTTPUnauthorized):
        run(api.create_reading(req))
    with pytest.raises(web.HTTPUnauthorized):
        run(api.list_readings(FakeRequest(pool=pool)))


# ---------- 失败路径：不得残留错位行 ----------

@pytest.mark.parametrize(
    "body",
    [
        {"probe_id": "探头C03", "temp_c": "abc"},   # 温度非数字
        {"probe_id": "探头C03"},                     # 缺温度
        {"probe_id": "   ", "temp_c": 4.2},          # 探头为空
    ],
)
def test_invalid_submission_leaves_no_row(body):
    pool = FakePool(row=make_row())
    req = FakeRequest(token=WRITER_TOKEN, body=body, pool=pool)
    with pytest.raises(web.HTTPBadRequest):
        run(api.create_reading(req))
    assert pool.insert_args is None, "校验失败不得落库"


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _FakeTransaction:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConn:
    def __init__(self, row, fail_finish=False):
        self.row = row
        self.fail_finish = fail_finish
        self.statements = []
        self.commits = 0
        self.rollbacks = 0

    def transaction(self):
        return _FakeTransaction()

    def execute(self, sql, params=None):
        self.statements.append((sql, params))
        if self.fail_finish and "status = 'done'" in sql:
            raise RuntimeError("db boom")
        if sql.lstrip().startswith("SELECT"):
            return _FakeResult(self.row)
        return _FakeResult(None)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def test_worker_failure_resets_pending():
    conn = FakeConn(
        {"id": 7, "probe_id": "探头A01", "temp_c": 4.2},
        fail_finish=True,
    )
    with pytest.raises(RuntimeError):
        worker.run_once(conn)
    resets = [
        params
        for sql, params in conn.statements
        if "SET status = 'pending'" in sql and params == (7,)
    ]
    assert resets, "判定失败必须把行复位为 pending，不得残留错位行"
    assert conn.rollbacks >= 1, "复位前必须先回滚已中止的事务"


@pytest.mark.parametrize(
    "temp_c, expected",
    [(4.2, "合格"), (12.5, "超温")],
)
def test_worker_judges_by_temp_column(temp_c, expected):
    conn = FakeConn({"id": 9, "probe_id": "探头X", "temp_c": temp_c})
    assert worker.run_once(conn) is True
    done = [
        params
        for sql, params in conn.statements
        if "status = 'done'" in sql
    ]
    assert done and done[0][0] == expected
    assert done[0][2] == 9


# ---------- 判定规则 ----------

def test_judge_temp_boundaries():
    assert judge_temp(4.2)[0] == "合格"
    assert judge_temp(8.0)[0] == "合格"
    assert judge_temp(8.1)[0] == "超温"
    assert judge_temp(12.5)[0] == "超温"


def test_verdict_for_display():
    assert verdict_for_display("合格", "done") == "合格"
    assert verdict_for_display(None, "pending") == "待处理"
    assert verdict_for_display(None, "processing") == "处理中"


# ---------- 列表渲染：首页表格探头列在前、温度列在后 ----------

def test_frontend_table_columns_aligned():
    text = FRONTEND_APP.read_text(encoding="utf-8")
    assert "h05" not in text, "渲染层不得残留对调标记"
    assert text.index("{r.probe_id}") < text.index("{r.temp_c}"), \
        "探头列必须在温度列之前渲染"
    assert text.index("探头") < text.index("温度℃")


# ---------- 拆除干净：对调模块与引用不得残留 ----------

def test_swap_modules_removed():
    removed = (
        "probe_temp_swap",
        "h05_extra_trap",
        "h05_pad_trap",
        "h05_render_trap",
    )
    for name in removed:
        assert not (BACKEND_DIR / f"{name}.py").exists(), f"{name}.py 应已拆除"
    for py in BACKEND_DIR.rglob("*.py"):
        if py.name == Path(__file__).name:
            continue
        text = py.read_text(encoding="utf-8")
        for marker in removed + ("swap_on_write", "swap_on_read", "prepare_insert", "prepare_row"):
            assert marker not in text, f"{py.name} 仍残留 {marker}"
