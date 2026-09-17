"""Claw 鹰爪量化交易系统 — Pytest 配置"""

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

# 必须先于任何 app 导入固定隔离库；禁止测试因启动目录不同写入开发 claw.db。
_TEST_DATABASE_DIR = Path(tempfile.mkdtemp(prefix="claw-pytest-")).resolve()
_TEST_DATABASE_PATH = _TEST_DATABASE_DIR / "claw-pytest.db"
os.environ["DATABASE_URL"] = (
    f"sqlite+aiosqlite:///{_TEST_DATABASE_PATH.as_posix()}"
)
os.environ["SQL_ECHO"] = "false"
# 测试启动调度器时不得真的改变宿主电源断言；防休眠单测使用假子进程。
os.environ["SCHEDULER_PREVENT_IDLE_SLEEP"] = "false"

import pytest

# 确保后端目录在 sys.path 中
backend_dir = Path(__file__).resolve().parent.parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

# Tests cover CLIs in both repository/scripts and backend/scripts. Keep both
# explicit package search locations regardless of pytest cwd/import order;
# do not alter production package paths or skip the root replay-rule tests.
import scripts as _test_scripts
# A live _NamespacePath recalculates after a CLI changes sys.path and can drop
# manually appended locations. Freeze the two explicit test roots as a list.
_test_scripts.__path__ = list(dict.fromkeys([
    str(backend_dir / "scripts"), str(backend_dir.parent / "scripts"),
    *_test_scripts.__path__,
]))


@pytest.fixture(scope="session", autouse=True)
def isolated_test_database_guard():
    """会话级硬保护：全局 settings/engine 只能指向本次 pytest 临时目录。"""
    from sqlalchemy.engine import make_url

    from app.config.settings import settings
    from app.db.session import engine, init_db

    configured_path = Path(make_url(settings.DATABASE_URL).database or "").resolve()
    engine_path = Path(engine.url.database or "").resolve()
    assert configured_path.is_relative_to(_TEST_DATABASE_DIR)
    assert engine_path.is_relative_to(_TEST_DATABASE_DIR)
    assert configured_path == _TEST_DATABASE_PATH
    assert engine_path == _TEST_DATABASE_PATH
    # 少量单元测试会通过 trade_calendar 等全局服务访问 app.db.session；
    # 也必须让这条全局连接落在临时库中，并具备完整表结构。
    asyncio.run(init_db())
    asyncio.run(engine.dispose())
    yield
    asyncio.run(engine.dispose())
    shutil.rmtree(_TEST_DATABASE_DIR, ignore_errors=True)


# 2026-09-18：原先这里有一个 session 作用域的自定义 `event_loop` fixture。
# 该写法在 pytest-asyncio 0.24（requirements-dev.txt 的钉版）里是**被弃用但生效**的，
# 会让会话级循环与函数级用例互相踩状态，实测整套 8,200 项里
# 3,033 项失败 + 207 项报错（`RuntimeError: There is no current event loop`），
# 而单文件运行 157/157 通过 —— 是跨用例污染，不是被测代码的问题。
# pytest-asyncio >=1.0 已移除该 fixture（重定义不生效），所以本项目实际一直
# 依赖 1.x 的行为。此处删除：1.x 下是死代码，0.24 下是有害代码。
