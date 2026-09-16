"""腾讯实时行情 + 同花顺K线 并发性能基准测试

测试维度:
1. 腾讯实时行情: 不同并发数(1/5/10/20/50/100) 采集5000只的耗时
2. 同花顺K线: 不同并发数(1/5/10/20/50) 采集100只的耗时(全量init模式)
3. 输出: 每组3次取平均, 给出推荐值

用法: python scripts/benchmark_collectors.py
"""

import asyncio
import json
import sys
import time as _time
from pathlib import Path

# 项目根目录
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import httpx
from loguru import logger


# ============================================================================
# 1. 腾讯实时行情 基准测试
# ============================================================================

CODE_PREFIX = {
    "6": "sh", "0": "sz", "3": "sz", "68": "sh", "4": "bj", "8": "bj",
}

def _prefix(code):
    if code.startswith("68"):
        return "sh"
    return CODE_PREFIX.get(code[0], "sz")

def _tencent_code(code):
    return f"{_prefix(code)}{code}"

SPOT_URL = "https://qt.gtimg.cn/q="


async def fetch_tencent_batch(codes, client):
    """单批次获取100只"""
    tencent_codes = [_tencent_code(c) for c in codes]
    url = SPOT_URL + ",".join(tencent_codes)
    resp = await client.get(url)
    resp.raise_for_status()
    
    import re
    result = {}
    pattern = re.compile(r'v_[^=]+="([^"]*)"')
    for match in pattern.finditer(resp.text):
        fields = match.group(1).split("~")
        if len(fields) >= 80:
            code = fields[2] if len(fields[2]) == 6 else ""
            if code:
                result[code] = fields
    return result


async def benchmark_tencent(concurrency, total_codes=1000, rounds=2):
    """测试指定并发数的腾讯采集性能
    
    Args:
        concurrency: 并发数(同时发送的HTTP请求数)
        total_codes: 测试用的股票数量(默认1000只, 够统计意义)
        rounds: 重复次数取平均
    
    Returns:
        dict: {elapsed, success_rate, throughput}
    """
    # 生成测试代码(模拟真实分布)
    test_codes = [f"{i % 10}{(i % 900 + 100):04d}" for i in range(total_codes)]
    
    elapsed_list = []
    success_counts = []
    
    for r in range(rounds):
        start = _time.monotonic()
        all_results = {}
        success = 0
        
        async with httpx.AsyncClient(timeout=15) as client:
            # 分批: 每批100只
            batch_size = 100
            batches = [test_codes[i:i+batch_size] for i in range(0, len(test_codes), batch_size)]
            
            # 信号量控制并发
            sem = asyncio.Semaphore(concurrency)
            
            async def fetch_one(batch):
                async with sem:
                    try:
                        result = await fetch_tencent_batch(batch, client)
                        return result
                    except Exception:
                        return {}
            
            tasks = [fetch_one(batch) for batch in batches]
            results = await asyncio.gather(*tasks)
            
            for res in results:
                if res:
                    all_results.update(res)
        
        elapsed = _time.monotonic() - start
        elapsed_list.append(elapsed)
        success_counts.append(len(all_results))
    
    avg_elapsed = sum(elapsed_list) / len(elapsed_list)
    avg_success = sum(success_counts) / len(success_counts)
    
    return {
        "concurrency": concurrency,
        "total_codes": total_codes,
        "batches": (total_codes + 99) // 100,
        "avg_elapsed_s": round(avg_elapsed, 3),
        "avg_success": int(avg_success),
        "success_rate": round(avg_success / total_codes * 100, 1),
        "throughput_codes_per_sec": round(total_codes / avg_elapsed, 1),
        "rounds": rounds,
        "detail": [round(e, 3) for e in elapsed_list],
    }


# ============================================================================
# 2. 同花顺K线 基准测试
# ============================================================================

THS_KLINE_URL = "http://d.10jqka.com.cn/v6/line/hs_{code}/11/last.js"
STOCKPAGE_URL = "http://stockpage.10jqka.com.cn/{code}/"


async def get_ths_cookie():
    """获取Cookie"""
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
        resp = await client.get(STOCKPAGE_URL.format(code="000001"))
        cookies = dict(resp.cookies)
        if not cookies:
            for header_val in resp.headers.get_list("set-cookie"):
                parts = header_val.split(";")[0].split("=", 1)
                if len(parts) == 2:
                    cookies[parts[0].strip()] = parts[1].strip()
        return "; ".join(f"{k}={v}" for k, v in cookies.items())


async def fetch_ths_kline(code, cookie, client):
    """获取单只K线(last.js模式)"""
    url = THS_KLINE_URL.format(code=code)
    headers = {
        "Referer": f"http://stockpage.10jqka.com.cn/{code}/",
        "Cookie": cookie,
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
    }
    try:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        
        import re
        json_match = re.search(r'\((\{.*\})\)', resp.text, re.DOTALL)
        if not json_match:
            return None
        
        data = json.loads(json_match.group(1))
        raw_data = data.get("data", "")
        if not raw_data:
            return []
        
        count = raw_data.count(";") + 1
        return count
    except Exception:
        return -1


async def benchmark_ths_kline(concurrency, sample_codes=None, rounds=2):
    """测试同花顺K线并发性能
    
    Returns:
        dict with performance metrics
    """
    if sample_codes is None:
        # 用100只热门股做样本
        sample_codes = [
            "000001","000002","000063","000065","000333","000338","000425",
            "000538","000568","000625","000651","000725","000768","000776",
            "000858","000895","000938","000977","001289","002007",
            "002027","002049","002120","002142","002230","002236","002241",
            "002304","002352","002371","002415","002456","002460",
            "002493","002555","002594","002601","002709","002714",
            "300003","300015","300033","300059","300122","300124",
            "300142","300223","300274","300308","300347","300394",
            "300408","300413","300418","300433","300496","300502",
            "300661","300699","300750","300760","300782","300832",
            "300896","300919","300999","301269","600009","600010",
            "600016","600019","600025","600028","600029","600030",
            "600031","600036","600048","600050","600061","600085",
            "600089","600104","600111","600115","600150","600176",
            "600196","600276","600309","600346","600406","600436",
            "600438","600519","600570","600585","600588","600600",
            "600690","600809","600837","600887","600893","600900",
            "601012","601066","601088","601111","601127","601138",
            "601166","601225","601228","601236","601318","601390",
            "601398","601618","601628","601633","601668","601669",
            "601688","601728","601766","601788","601799","601816",
            "601838","601857","601881","601899","601919","601985",
            "601989","603019","603160","603259","603288","603501",
            "603799","603833","603986","605117","688001","688005",
            "688012","688036","688099","688111","688187","688256",
            "688303","688396","688561","688599","688981","689188",
        ]
    
    elapsed_list = []
    kline_counts = []
    
    for r in range(rounds):
        start = _time.monotonic()
        cookie = await get_ths_cookie()
        total_klines = 0
        success = 0
        
        sem = asyncio.Semaphore(concurrency)
        
        async def fetch_one(code, c):
            nonlocal total_klines, success
            async with sem:
                try:
                    async with httpx.AsyncClient(timeout=15) as client:
                        result = await fetch_ths_kline(code, c, client)
                        if isinstance(result, int) and result > 0:
                            total_klines += result
                            success += 1
                        await asyncio.sleep(0.01)  # 最小限流
                except Exception:
                    pass
        
        tasks = [fetch_one(code, cookie) for code in sample_codes]
        await asyncio.gather(*tasks)
        
        elapsed = _time.monotonic() - start
        elapsed_list.append(elapsed)
        kline_counts.append(total_klines)
    
    avg_elapsed = sum(elapsed_list) / len(elapsed_list)
    avg_klines = sum(kline_counts) / len(kline_counts)
    
    return {
        "concurrency": concurrency,
        "total_stocks": len(sample_codes),
        "avg_elapsed_s": round(avg_elapsed, 3),
        "avg_success_rate": round(success / len(sample_codes) * 100, 1) if r >= 0 else 0,
        "avg_total_klines": int(avg_klines),
        "throughput_stocks_per_sec": round(len(sample_codes) / avg_elapsed, 1),
        "rounds": rounds,
        "detail": [round(e, 3) for e in elapsed_list],
    }


# ============================================================================
# 主流程
# ============================================================================

async def main():
    print("=" * 72)
    print("  Claw 数据采集器 并发性能基准测试")
    print(f"  时间: {_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 72)
    
    # ------------------------------------------------------------------
    # Part A: 腾讯实时行情 并发测试
    # ------------------------------------------------------------------
    print("\n" + "=" * 72)
    print("  Part A: 腾讯实时行情(qt.gtimg.cn) — 并发基准")
    print("  目标: 1000只 × 不同并发数 × 2轮")
    print("=" * 72)
    
    tencent_configs = [
        ("串行", 1),
        ("低并发", 3),
        ("中并发", 5),
        ("中高", 10),
        ("高并发", 20),
        ("超高", 50),
        ("极限", 100),  # 100个请求同时发出
    ]
    
    tencent_results = []
    for label, conc in tencent_configs:
        print(f"\n  ⏳ 测试: {label}(concurrency={conc}) ...")
        try:
            result = await benchmark_tencent(conc, total_codes=1000, rounds=2)
            result["label"] = label
            tencent_results.append(result)
            
            print(f"     ✅ {result['avg_elapsed_s']}s | 成功{result['avg_success']}/{result['total_codes']} "
                  f"({result['success_rate']}%) | 吞吐{result['throughput_codes_per_sec']}只/s "
                  f"| 详情:{result['detail']}")
        except Exception as e:
            print(f"     ❌ 失败: {e}")
    
    # ------------------------------------------------------------------
    # Part B: 同花顺K线 并发测试  
    # ------------------------------------------------------------------
    print("\n" + "=" * 72)
    print("  Part B: 同花顺日K线(d.10jqka.com.cn) — 并发基准")
    print("  目标: 100只(last.js全量) × 不同并发数 × 2轮")
    print("=" * 72)
    
    ths_configs = [
        ("串行", 1),
        ("低并发", 3),
        ("当前配置", 5),
        ("中并发", 10),
        ("高并发", 20),
        ("极限", 50),
    ]
    
    ths_results = []
    for label, conc in ths_configs:
        print(f"\n  ⏳ 测试: {label}(concurrency={conc}) ...")
        try:
            result = await benchmark_ths_kline(concurrency=conc, rounds=2)
            result["label"] = label
            ths_results.append(result)
            
            print(f"     ✅ {result['avg_elapsed_s']}s | K线总计≈{result['avg_total_klines']}条 "
                  f"| 吞吐{result['throughput_stocks_per_sec']}只/s "
                  f"| 详情:{result['detail']}")
        except Exception as e:
            print(f"     ❌ 失败: {e}")
    
    # ------------------------------------------------------------------
    # 结果汇总与推荐
    # ------------------------------------------------------------------
    print("\n" + "=" * 72)
    print("  📊 结果汇总 & 最佳配置推荐")
    print("=" * 72)
    
    # 腾讯汇总表
    print("\n  ── 腾讯实时行情 ──")
    print(f"  {'配置':<10} {'并发':>6} {'耗时(s)':>10} {'成功率%':>10} {'吞吐(只/s)':>14}")
    print("  " + "-" * 54)
    
    best_tenc = None
    best_tenc_tp = 0
    for r in tencent_results:
        mark = ""
        if r["throughput_codes_per_sec"] > best_tenc_tp:
            best_tenc_tp = r["throughput_codes_per_sec"]
            best_tenc = r
            mark = " ← 最佳"
        print(f"  {r['label']:<10} {r['concurrency']:>6} {r['avg_elapsed_s']:>10.3f} "
              f"{r['success_rate']:>10.1f} {r['throughput_codes_per_sec']:>14.1f}{mark}")
    
    # 推算5000只耗时
    if best_tenc:
        est_5000 = 5000 / best_tenc["throughput_codes_per_sec"]
        print(f"\n  🔹 推荐: concurrency={best_tenc['concurrency']}, 5000只预估耗时 ≈ {est_5000:.1f}s")
    
    # THS K线汇总表
    print("\n  ── 同花顺日K线 ──")
    print(f"  {'配置':<10} {'并发':>6} {'耗时(s)':>10} {'K线条':>10} {'吞吐(只/s)':>14}")
    print("  " + 54 * "-")
    
    best_ths = None
    best_ths_tp = 0
    for r in ths_results:
        mark = ""
        if r["throughput_stocks_per_sec"] > best_ths_tp:
            best_ths_tp = r["throughput_stocks_per_sec"]
            best_ths = r
            mark = " ← 最佳"
        print(f"  {r['label']:<10} {r['concurrency']:>6} {r['avg_elapsed_s']:>10.3f} "
              f"{r['avg_total_klines']:>10} {r['throughput_stocks_per_sec']:>14.1f}{mark}")
    
    # 推算5000只全量回填耗时
    if best_ths:
        est_5000_full = 5000 / best_ths["throughput_stocks_per_sec"] * (1758 / 140)  # init是last+year
        print(f"\n  🔹 推荐: concurrency={best_ths['concurrency']}, daily增量(100只last)≈{5000/best_ths['throughput_stocks_per_sec']:.0f}s")
        print(f"  🔹 全量回填(5000只×8年)≈{est_5000_full:.0f}s (≈{est_5000_full/60:.1f}分钟)")
    
    # 最终结论
    print("\n" + "=" * 72)
    print("  🎯 最终建议配置")
    print("=" * 72)
    if best_tenc and best_ths:
        print(f"""
  腾讯实时行情(tencent_source.py):
    • batch_concurrency: {best_tenc['concurrency']} (当前无并发控制, 逐batch串行)
    • 5000只耗时: ≈{5000/best_tenc['throughput_codes_per_sec']:.1f}s (当前约4-6s)
    • rate_limit: 可降至 {max(0.01, 0.08/best_tenc['concurrency']*3):.3f}s
  
  同花顺日K线(ths_kline_source.py):  
    • batch_concurrency: {best_ths['concurrency']} (当前=5, 串行rate_limit=0.2s)
    • 盘后daily增量(5000只): ≈{5000/best_ths['throughput_stocks_per_sec']:.0f}s (当前≈210s)
    • 全量init回填(5000只×8年): ≈{est_5000_full:.0f}s (当前≈40分钟)
""")
    
    # 保存结果
    output = {
        "timestamp": _time.strftime("%Y-%m-%d %H:%M:%S"),
        "tencent": tencent_results,
        "ths_kline": ths_results,
        "recommendation": {
            "tencent_best_concurrency": best_tenc["concurrency"] if best_tenc else None,
            "ths_best_concurrency": best_ths["concurrency"] if best_ths else None,
        }
    }
    
    out_path = PROJECT_ROOT / "test-results" / "collector_benchmark.json"
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  💾 详细数据已保存: {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
