#!/usr/bin/env python3
"""打开有界面的浏览器窗口，供用户登录同花顺问财。

登录后会话写入持久化 profile（`runtime/wencai-profile`，已 gitignore），
之后可直接用 HTTP 调用 `web_stock_recommend_query` 接口，无需再开浏览器。

脚本会轮询登录状态并打印；用户关闭窗口或超时后退出。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

PROFILE = Path("/Users/youzix/WorkBuddy/Claw/runtime/wencai-profile")
HOME = "https://www.iwencai.com/"
COOKIE_DUMP = Path("/tmp/wencai_cookies.json")
MAX_MINUTES = 40

# 登录后会出现的会话 cookie 名（任意命中即视为已登录）
LOGIN_COOKIE_HINTS = ("userid", "user_id", "hx_user", "u_ukey", "escapename", "ticket")


def main() -> int:
    from playwright.sync_api import sync_playwright

    PROFILE.mkdir(parents=True, exist_ok=True)
    print(f"  profile: {PROFILE}")
    print("  正在打开浏览器窗口，请在其中登录问财…", flush=True)

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE),
            headless=False,
            viewport={"width": 1280, "height": 860},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto(HOME, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:
            print(f"  首页加载异常（可忽略，手动导航即可）: {exc}", flush=True)

        deadline = time.time() + MAX_MINUTES * 60
        reported = False
        while time.time() < deadline:
            time.sleep(5)
            try:
                cookies = ctx.cookies()
            except Exception:
                print("  浏览器已关闭", flush=True)
                return 0
            names = {c["name"] for c in cookies}
            hit = sorted(names & set(LOGIN_COOKIE_HINTS))
            if hit and not reported:
                reported = True
                COOKIE_DUMP.write_text(
                    json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                print(f"  ✅ 检测到登录会话 cookie: {hit}", flush=True)
                print(f"  已导出 {len(cookies)} 个 cookie -> {COOKIE_DUMP}", flush=True)
                print("  （保持窗口打开亦可；关掉窗口也不影响已保存的会话）", flush=True)
            elif not hit:
                print(f"  等待登录… 当前 cookie 数 {len(cookies)}", flush=True)

        print("  超时退出", flush=True)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
