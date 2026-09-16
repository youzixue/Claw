#!/usr/bin/env python3
"""Spike 2: 抓取问财选股接口的**请求体与响应体**。

Spike 1 已证明：`/gateway/iwc-web-business-center/executor/execute/data_query_mongo/`
与 `web_stock_recommend_query/` 在**未登录**状态下返回 200；
而 AI 对话的 `/gateway/aime/stream-query` 返回 401。

若能拿到这两个接口的请求体结构，就可能**不需要 Playwright 常驻** ——
直接用 HTTP 调用即可（更轻、更快、无浏览器依赖）。
"""
from __future__ import annotations

import json
from pathlib import Path

URL = ("https://www.iwencai.com/screener/result"
       "?w=%E6%B6%A8%E5%81%9C%E5%8E%9F%E5%9B%A0&querytype=stock&sign=1789573927514")
PROFILE = Path("/Users/youzix/WorkBuddy/Claw/runtime/wencai-profile")
OUT = Path("/tmp/wencai_api_capture.json")
TARGET = "iwc-web-business-center/executor/execute"


def main() -> int:
    from playwright.sync_api import sync_playwright

    captured: list[dict] = []

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE), headless=True,
            viewport={"width": 1440, "height": 900},
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"),
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            page = ctx.new_page()

            def on_response(resp):
                if TARGET not in resp.url:
                    return
                item = {"url": resp.url, "status": resp.status, "method": resp.request.method}
                try:
                    item["request_post_data"] = resp.request.post_data
                except Exception:
                    item["request_post_data"] = None
                try:
                    body = resp.text()
                    item["response_len"] = len(body)
                    item["response_head"] = body[:4000]
                except Exception as exc:
                    item["response_error"] = str(exc)
                captured.append(item)

            page.on("response", on_response)
            page.goto(URL, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(12000)
            try:
                page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                pass

            # 抓表格/结果区
            print(f"  <table> 数: {len(page.query_selector_all('table'))}")
            for sel in ("[class*=result]", "[class*=table]", "[class*=stock]"):
                els = page.query_selector_all(sel)
                if els:
                    print(f"  {sel}: {len(els)} 个")
            page.screenshot(path="/tmp/wencai_spike2.png", full_page=True)
        finally:
            ctx.close()

    OUT.write_text(json.dumps(captured, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  捕获 {len(captured)} 个目标接口，写入 {OUT}")
    for item in captured:
        print(f"\n  --- {item['method']} {item['url'].split('?')[0].split('/gateway/')[-1]}  HTTP {item['status']} ---")
        pd = item.get("request_post_data")
        print(f"    请求体: {(pd or '(空)')[:400]}")
        print(f"    响应长度: {item.get('response_len')}  头部: {(item.get('response_head') or '')[:300]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
