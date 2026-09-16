#!/usr/bin/env python3
"""Spike 3: 用已登录的 profile 验证会话 + 抓真实数据接口。

做三件事：
1. 调 `/gateway/aime/user-info` 判断是否真的登录（比看 cookie 名可靠）
2. 打开 screener 页并实际发起一次查询，抓**所有** /gateway/ 请求，
   找出返回 `datas` 非空的真正数据接口（含请求体）
3. 导出当前 cookie，供后续轻量 HTTP 调用
"""
from __future__ import annotations

import json
from pathlib import Path

PROFILE = Path("/Users/youzix/WorkBuddy/Claw/runtime/wencai-profile")
QUERY = "今天涨停"
URL = ("https://www.iwencai.com/screener/result?w=" +
       "%E4%BB%8A%E5%A4%A9%E6%B6%A8%E5%81%9C" + "&querytype=stock")
COOKIES_OUT = Path("/tmp/wencai_cookies_live.json")


def main() -> int:
    from playwright.sync_api import sync_playwright

    hits: list[dict] = []

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE), headless=True,
            viewport={"width": 1440, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            page = ctx.new_page()

            def on_response(resp):
                if "/gateway/" not in resp.url:
                    return
                rec = {"url": resp.url.split("?")[0].split("/gateway/")[-1],
                       "status": resp.status, "method": resp.request.method}
                try:
                    rec["req"] = (resp.request.post_data or "")[:400]
                except Exception:
                    rec["req"] = None
                try:
                    body = resp.text()
                    rec["len"] = len(body)
                    rec["head"] = body[:1500]
                    j = json.loads(body)
                    if isinstance(j, dict):
                        rec["total"] = (j.get("meta") or {}).get("total")
                        rec["datas"] = len(j.get("datas") or [])
                except Exception:
                    rec["len"] = -1
                hits.append(rec)

            page.on("response", on_response)

            print("  [1] 验证登录态…")
            page.goto("https://www.iwencai.com/gateway/aime/user-info?source=Ths_iwencai_Xuangu",
                      wait_until="domcontentloaded", timeout=40000)
            print("      user-info:", page.inner_text("body")[:300])

            print(f"  [2] 打开 screener: {QUERY}")
            page.goto(URL, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(15000)
            try:
                page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                pass

            txt = page.inner_text("body")
            print(f"      页面含『涨停』: {'涨停' in txt} | 长度 {len(txt)}")
            for sel in ("table", "[class*=result]", "[class*=table]"):
                print(f"      {sel}: {len(page.query_selector_all(sel))}")
            page.screenshot(path="/tmp/wencai_spike3.png", full_page=True)
        finally:
            cookies = ctx.cookies()
            COOKIES_OUT.write_text(json.dumps(cookies, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
            ctx.close()

    print(f"\n  [3] 捕获 &gateway& 请求 {len(hits)} 个；cookie {len(cookies)} 个 -> {COOKIES_OUT}")
    print("\n  === 返回了数据(datas>0)的接口 ===")
    got = [h for h in hits if (h.get("datas") or 0) > 0]
    for h in got:
        print(f"    {h['method']} {h['url']}  HTTP {h['status']}  datas={h['datas']} total={h.get('total')}")
        print(f"      请求体: {h.get('req')}")
        print(f"      响应头: {(h.get('head') or '')[:400]}")
    if not got:
        print("    （无）")
    print("\n  === 全部 gateway 接口一览 ===")
    seen = set()
    for h in hits:
        key = (h["url"], h["status"])
        if key in seen:
            continue
        seen.add(key)
        print(f"    {h['status']} {h['url']}  datas={h.get('datas')} total={h.get('total')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
