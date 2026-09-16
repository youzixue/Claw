"""Alembic 环境配置 — 异步模式"""

import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from app.config.settings import settings

# Alembic Config对象
config = context.config
# 与运行时共用同一条已规范化数据库 URL；同时允许 DATABASE_URL 在测试/部署时
# 覆盖 alembic.ini，避免从不同 cwd 执行迁移落到另一个 claw.db。
config.set_main_option("sqlalchemy.url", settings.DATABASE_URL.replace("%", "%%"))

# 日志
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 导入所有模型(Base.metadata)
from app.db.session import Base
from app.models import (  # noqa: F401 — 确保所有模型注册
    stock, sector, factor, signal, news, risk, paper, governance, promotion, regime, review,
)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """离线模式 — 生成SQL脚本，不连接数据库"""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """异步模式 — 实际连接数据库执行迁移"""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """在线模式"""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
