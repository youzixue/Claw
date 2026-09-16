#!/usr/bin/env python3
"""Spike: 用 Playwright 打开问财 screener 页，验证能否拿到结果表。

目的：判断「Playwright 驱动」路线是否可行 —— 若可行，则无需逆向
`/gateway/aime/stream-query` 的流式协议与鉴权。

只做读取与截图，不写入任何业务数据、不调用任何交易接口。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

URL = ("https://www.iwencai.com/screener/result"
       "?w=%E6%B6%A8%E5%81%9C%E5%8E%9F%E5%9B%A0&querytype=stock&sign=1789573927514")
PROFILE = Path("/Users/youzix/WorkBuddy/Claw/runtime/wencai-profile")
SHOT = Path("/tmp/wencai_spike.png")


def main() -> int:
    from playwright.sync_api import sync_playwright

    PROFILE.mkdir(parents=True, exist_ok=True)
    print(f"  profile: {PROFILE}")

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE),
            headless=True,
            viewport={"width": 1440, "height": 900},
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"),
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            page = ctx.new_page()
            seen: list[str] = []
            page.on("response", lambda r: seen.append(f"{r.status} {r.url[:110]}"))

            print("  导航…")
            page.goto(URL, wait_until="domcontentloaded", timeout=45000)

            # 给前端 JS 留出渲染时间
            page.wait_for_timeout(8000)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass

            print(f"  title : {page.title()!r}")
            print(f"  url   : {page.url[:120]}")

            body = page.inner_text("body")[:600].replace("\n", " | ")
            print(f"  body  : {body[:400]}")

            tables = page.query_selector_all("table")
            print(f"  <table> 数: {len(tables)}")
            row_counts = []
            for t in tables[:3]:
                try:
                    row_counts.append(len(t.query_selector_all("tr")))
                except Exception:
                    row_counts.append(-1)
            if row_counts:
                print(f"  各表行数: {row_counts}")

            # 登录态判断
            login_hint = any(k in body for k in ("登录", "登陆", "注册", "验证"))
            print(f"  疑似登录/验证提示: {login_hint}")

            print("  关键响应:")
            for line in [s for s in seen if "gw" in s or "gateway" in s][:8]:
                print(f"    {line}")

            page.screenshot(path=str(SHOT), full_page=True)
            print(f"  截图: {SHOT}")
            print(f"  结果: {'✅ 有表格' if row_counts and row_counts[0] > 0 else '❌ 未拿到表格'}")
            return 0
        finally:
            ctx.close()


if __name__ == "__main__":
    raise SystemExit(main())
