"""数据源基类 — 所有采集器的抽象基类"""

from abc import ABC, abstractmethod
from datetime import date, datetime
from typing import Any, Optional
import time as _time
import asyncio

import pandas as pd
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.data_quality import data_quality_guard
from app.config.settings import settings


class DataSourceBase(ABC):
    """数据源基类 — 提供限流、重试、质量监控等通用能力"""

    source_name: str = "base"      # 数据源名称
    rate_limit: float = 0.5        # 限流(秒/次)
    max_retries: int = 3           # 最大重试次数
    _akshare_call_lock = asyncio.Lock()

    def __init__(self):
        self._last_call_time = 0.0

    async def _rate_limit(self):
        """限流控制"""
        now = _time.monotonic()
        elapsed = now - self._last_call_time
        if elapsed < self.rate_limit:
            await asyncio.sleep(self.rate_limit - elapsed)
        self._last_call_time = _time.monotonic()

    @staticmethod
    def _is_akshare_func(func) -> bool:
        module = getattr(func, "__module__", "") or ""
        return module == "akshare" or module.startswith("akshare.")

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((ConnectionError, TimeoutError)),
    )
    async def _safe_call(self, api_name: str, func, *args, **kwargs) -> pd.DataFrame:
        """安全调用 — 带重试+限流+质量监控"""
        start = _time.monotonic()
        try:
            # AkShare 部分接口底层依赖 V8/libmini_racer, 同进程多线程并发会崩溃。
            # 统一串行化 AkShare 调用; 业务接口读取缓存, 外部源采集由后台慢慢跑。
            if self._is_akshare_func(func):
                async with self._akshare_call_lock:
                    await self._rate_limit()
                    loop = asyncio.get_event_loop()
                    result = await loop.run_in_executor(None, lambda: func(*args, **kwargs))
            else:
                await self._rate_limit()
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(None, lambda: func(*args, **kwargs))

            latency_ms = int((_time.monotonic() - start) * 1000)
            record_count = len(result) if isinstance(result, pd.DataFrame) else 0

            logger.debug(
                f"[{self.source_name}] {api_name}: {record_count}条, {latency_ms}ms"
            )
            return result

        except Exception as e:
            latency_ms = int((_time.monotonic() - start) * 1000)
            logger.error(f"[{self.source_name}] {api_name} 失败: {e}")
            raise

    @abstractmethod
    async def collect(self, session: AsyncSession, **kwargs) -> Any:
        """采集数据 — 子类必须实现"""
        ...

    @abstractmethod
    async def health_check(self, session: AsyncSession) -> bool:
        """健康检查 — 子类必须实现"""
        ...
