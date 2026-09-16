#!/usr/bin/env python3
"""生成同花顺K线映射表: 行业90 + 概念375"""
import akshare as ak
import pywencai
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed

print("=" * 80)
print("生成同花顺K线映射表: 行业90 + 概念375")
print("=" * 80)

# ========== 1. 获取pywencai成分股数据 ==========
print("\n[1/4] 获取pywencai行业成分股...")
pyw = pywencai.get(query='同花顺行业级别3', loop=True)
pyw['行业二级'] = pyw['所属同花顺行业'].apply(lambda x: str(x).split('-')[1] if '-' in str(x) else str(x))
pyw_ind_counts = pyw.groupby('行业二级').size().reset_index(name='pyw_stock_count')
pyw_ind_pe = pyw.groupby('行业二级')['市盈率(pe)[20260410]'].mean().reset_index(name='pyw_pe_avg')
pyw_ind = pyw_ind_counts.merge(pyw_ind_pe, on='行业二级', how='left')
print(f"  pywencai行业: {len(pyw_ind)}个")

print("[1/4] 获取pywencai概念成分股...")
pyw_con = pywencai.get(query='概念板块', loop=True)
con_stock_count = {}
for _, row in pyw_con.iterrows():
    concepts = str(row.get('所属概念', '')).split(';')
    for c in concepts:
        c = c.strip()
        if c:
            con_stock_count[c] = con_stock_count.get(c, 0) + 1
pyw_con_df = pd.DataFrame([
    {'ths_name': k, 'pyw_stock_count': v} for k, v in con_stock_count.items()
])
print(f"  pywencai概念: {len(pyw_con_df)}个")

# ========== 2. 获取THS板块列表 ==========
print("\n[2/4] 获取THS板块列表...")
ths_ind = ak.stock_board_industry_name_ths()
ths_con = ak.stock_board_concept_name_ths()
print(f"  THS行业: {len(ths_ind)}个, THS概念: {len(ths_con)}个")

# ========== 3. 获取THS summary(涨跌家数) ==========
print("\n[3/4] 获取THS行业summary(涨跌家数)...")
ths_ind_sum = ak.stock_board_industry_summary_ths()
ths_ind_sum['合计'] = ths_ind_sum['上涨家数'] + ths_ind_sum['下跌家数']
print(f"  行业summary: {len(ths_ind_sum)}个")

print("[3/4] 获取THS概念summary...")
try:
    ths_con_sum = ak.stock_board_concept_summary_ths()
    print(f"  概念summary: {len(ths_con_sum)}个(不全)")
except:
    ths_con_sum = pd.DataFrame()
    print("  概念summary: 获取失败")

# ========== 4. 并发采集K线最新数据 ==========
print("\n[4/4] 并发采集K线数据...")

def fetch_kline(args):
    ktype, name = args
    try:
        if ktype == 'industry':
            df = ak.stock_board_industry_index_ths(symbol=name, start_date="20260407", end_date="20260410")
        else:
            df = ak.stock_board_concept_index_ths(symbol=name, start_date="20260407", end_date="20260410")
        if df.empty:
            return name, ktype, None
        latest = df.iloc[-1]
        prev = df.iloc[-2] if len(df) >= 2 else None
        result = {
            'date': str(latest['日期']),
            'open': round(float(latest['开盘价']), 3),
            'close': round(float(latest['收盘价']), 3),
            'high': round(float(latest['最高价']), 3),
            'low': round(float(latest['最低价']), 3),
            'volume': round(float(latest['成交量']), 0),
            'amount': round(float(latest['成交额']), 2),
        }
        if prev is not None:
            prev_close = float(prev['收盘价'])
            if prev_close > 0:
                result['pct_change'] = round((result['close'] - prev_close) / prev_close * 100, 2)
                result['amplitude'] = round((result['high'] - result['low']) / prev_close * 100, 2)
            else:
                result['pct_change'] = None
                result['amplitude'] = None
        return name, ktype, result
    except Exception as e:
        return name, ktype, f"ERROR: {e}"

tasks = []
for name in ths_ind['name']:
    tasks.append(('industry', name))
for name in ths_con['name']:
    tasks.append(('concept', name))

print(f"  总任务: {len(tasks)}个 (行业{len(ths_ind)}+概念{len(ths_con)})")

kline_results = {}
with ThreadPoolExecutor(max_workers=5) as pool:
    futures = {pool.submit(fetch_kline, t): t for t in tasks}
    for i, f in enumerate(as_completed(futures)):
        name, ktype, data = f.result()
        kline_results[(ktype, name)] = data
        if (i+1) % 100 == 0:
            print(f"  进度: {i+1}/{len(tasks)}")

kline_success = sum(1 for v in kline_results.values() if isinstance(v, dict))
kline_fail = sum(1 for v in kline_results.values() if not isinstance(v, dict))
print(f"  K线采集完成: {kline_success}成功, {kline_fail}失败")

# ========== 5. 组装映射表 ==========
print("\n[5/5] 组装映射表...")

rows = []

# 行业板块
for _, ths_row in ths_ind.iterrows():
    name = ths_row['name']
    code = ths_row.get('code', '')
    
    kline = kline_results.get(('industry', name))
    
    pyw_match = pyw_ind[pyw_ind['行业二级'] == name]
    pyw_count = int(pyw_match['pyw_stock_count'].values[0]) if len(pyw_match) > 0 else ''
    pyw_pe = round(float(pyw_match['pyw_pe_avg'].values[0]), 1) if len(pyw_match) > 0 and pd.notna(pyw_match['pyw_pe_avg'].values[0]) else ''
    
    sum_match = ths_ind_sum[ths_ind_sum['板块'] == name]
    ths_up = int(sum_match['上涨家数'].values[0]) if len(sum_match) > 0 else ''
    ths_down = int(sum_match['下跌家数'].values[0]) if len(sum_match) > 0 else ''
    ths_total = int(sum_match['合计'].values[0]) if len(sum_match) > 0 else ''
    
    row = {
        'type': '行业',
        'ths_name': name,
        'ths_code': code,
        'pyw_match': '✅' if len(pyw_match) > 0 else '❌',
        'pyw_stock_count': pyw_count,
        'pyw_pe_avg': pyw_pe,
        'ths_up_count': ths_up,
        'ths_down_count': ths_down,
        'ths_total_count': ths_total,
    }
    
    if isinstance(kline, dict):
        row.update({
            'kline_date': kline['date'],
            'kline_open': kline['open'],
            'kline_close': kline['close'],
            'kline_high': kline['high'],
            'kline_low': kline['low'],
            'kline_volume': kline['volume'],
            'kline_amount': kline['amount'],
            'kline_pct_change': kline.get('pct_change', ''),
            'kline_amplitude': kline.get('amplitude', ''),
            'kline_status': '✅'
        })
    else:
        row.update({
            'kline_date': '', 'kline_open': '', 'kline_close': '', 'kline_high': '', 'kline_low': '',
            'kline_volume': '', 'kline_amount': '', 'kline_pct_change': '', 'kline_amplitude': '',
            'kline_status': f'❌ {kline}'
        })
    
    rows.append(row)

# 概念板块
for _, ths_row in ths_con.iterrows():
    name = ths_row['name']
    code = ths_row.get('code', '')
    
    kline = kline_results.get(('concept', name))
    
    pyw_match = pyw_con_df[pyw_con_df['ths_name'] == name]
    pyw_count = int(pyw_match['pyw_stock_count'].values[0]) if len(pyw_match) > 0 else ''
    
    if len(ths_con_sum) > 0 and '概念名称' in ths_con_sum.columns:
        sum_match = ths_con_sum[ths_con_sum['概念名称'] == name]
        ths_con_count = int(sum_match['成分股数量'].values[0]) if len(sum_match) > 0 else ''
    else:
        ths_con_count = ''
    
    row = {
        'type': '概念',
        'ths_name': name,
        'ths_code': code,
        'pyw_match': '✅' if len(pyw_match) > 0 else '❌',
        'pyw_stock_count': pyw_count,
        'pyw_pe_avg': '',
        'ths_up_count': '',
        'ths_down_count': '',
        'ths_total_count': ths_con_count,
    }
    
    if isinstance(kline, dict):
        row.update({
            'kline_date': kline['date'],
            'kline_open': kline['open'],
            'kline_close': kline['close'],
            'kline_high': kline['high'],
            'kline_low': kline['low'],
            'kline_volume': kline['volume'],
            'kline_amount': kline['amount'],
            'kline_pct_change': kline.get('pct_change', ''),
            'kline_amplitude': kline.get('amplitude', ''),
            'kline_status': '✅'
        })
    else:
        row.update({
            'kline_date': '', 'kline_open': '', 'kline_close': '', 'kline_high': '', 'kline_low': '',
            'kline_volume': '', 'kline_amount': '', 'kline_pct_change': '', 'kline_amplitude': '',
            'kline_status': f'❌ {kline}'
        })
    
    rows.append(row)

df = pd.DataFrame(rows)
df.to_csv('ths_kline_mapping.tsv', sep='\t', index=False)

ind_rows = df[df['type']=='行业']
con_rows = df[df['type']=='概念']
print(f"\n映射表已生成: ths_kline_mapping.tsv")
print(f"  行业: {len(ind_rows)}行, 概念: {len(con_rows)}行, 合计: {len(df)}行")
print(f"  行业K线成功: {(ind_rows['kline_status']=='✅').sum()}/{len(ind_rows)}")
print(f"  概念K线成功: {(con_rows['kline_status']=='✅').sum()}/{len(con_rows)}")
print(f"  行业pywencai匹配: {(ind_rows['pyw_match']=='✅').sum()}/{len(ind_rows)}")
print(f"  概念pywencai匹配: {(con_rows['pyw_match']=='✅').sum()}/{len(con_rows)}")

print(f"\n--- 行业样本(前5) ---")
for _, r in ind_rows.head(5).iterrows():
    print(f"  {r['ths_name']}: K线{r['kline_status']} pywencai{r['pyw_match']} 成分股{r['pyw_stock_count']}只 THS涨{r['ths_up_count']}/跌{r['ths_down_count']} 收{r['kline_close']} 涨跌{r['kline_pct_change']}%")

print(f"\n--- 概念样本(前5) ---")
for _, r in con_rows.head(5).iterrows():
    print(f"  {r['ths_name']}: K线{r['kline_status']} pywencai{r['pyw_match']} 成分股{r['pyw_stock_count']}只 收{r['kline_close']} 涨跌{r['kline_pct_change']}%")
