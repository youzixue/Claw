import sqlite3

con = sqlite3.connect("claw.db")
cur = con.cursor()

# 08-24 预测名单 -> 08-25 盘中表现（stock_spot）
pred_rows = cur.execute(
    "SELECT code, name, target_board, predicted_probability, signal_status "
    "FROM promotion_prediction_record WHERE prediction_trade_date='2026-08-24'"
).fetchall()
codes = [r[0] for r in pred_rows]
pdf = {r[0]: r for r in pred_rows}

placeholders = ",".join("?" for _ in codes)
spot = cur.execute(
    f"SELECT code, change_pct FROM stock_spot WHERE code IN ({placeholders})",
    codes,
).fetchall()
spot_map = {r[0]: r[1] for r in spot}

covered = 0
dist = {"涨停": 0, ">5%": 0, "上涨": 0, "平": 0, "下跌": 0, "<-5%": 0, "无行情": 0}
for code, name, tb, prob, sig in pred_rows:
    chg = spot_map.get(code)
    if chg is None:
        dist["无行情"] += 1
        continue
    covered += 1
    if chg >= 9.9 * 0.99:
        dist["涨停"] += 1
    elif chg >= 5:
        dist[">5%"] += 1
    elif chg > 0:
        dist["上涨"] += 1
    elif chg == 0:
        dist["平"] += 1
    elif chg > -5:
        dist["下跌"] += 1
    else:
        dist["<-5%"] += 1

print(f"08-24 预测 {len(pred_rows)} 只，今日(08-25 10:44)有行情 {covered} 只")
print("涨跌分布:")
for k, v in dist.items():
    print(f"  {k}: {v}")

# 高概率组（prob>=0.3）表现
high = [r for r in pred_rows if r[3] and r[3] >= 0.3]
print(f"\n高概率组(prob>=0.3) {len(high)} 只:")
up = down = lu = 0
for code, name, tb, prob, sig in high:
    chg = spot_map.get(code)
    if chg is None:
        continue
    if chg >= 9.9 * 0.99:
        lu += 1
    elif chg > 0:
        up += 1
    else:
        down += 1
print(f"  涨停 {lu} / 上涨 {up} / 下跌&平 {down}")

# 列出高概率组的实际表现
print("\n高概率组明细(prob>=0.3):")
for code, name, tb, prob, sig in sorted(high, key=lambda x: -(x[3] or 0)):
    chg = spot_map.get(code)
    print(f"  {code} {name} TB{tb} prob={prob:.2f} 今涨幅={chg if chg is not None else 'N/A'}%")

con.close()