import os, re, json
for k in ['HTTP_PROXY','HTTPS_PROXY','http_proxy','https_proxy','ALL_PROXY','all_proxy']:
    os.environ.pop(k, None)
os.environ['NO_PROXY'] = '*'
import requests
s = requests.Session()
s.trust_env = False
headers = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)'}
s.get('http://stockpage.10jqka.com.cn/000001/', headers=headers, timeout=10)

code = '000001'

# ========== 1. 同花顺 - 分钟K线全部period ==========
print('='*100)
print('【同花顺 - 分钟K线完整扫描】')
print('='*100)
for period in ['01', '05', '15', '30', '60', '41', '51', '11', '21']:
    for suffix in ['last.js', '2026.js']:
        url = f'http://d.10jqka.com.cn/v6/line/hs_{code}/{period}/{suffix}'
        try:
            r = s.get(url, headers=headers, timeout=5)
            if r.status_code == 200 and len(r.text) > 50:
                match = re.search(r'\((\{.*\})\)', r.text, re.DOTALL)
                if not match:
                    # v8直接JSON
                    try:
                        data = json.loads(r.text)
                    except:
                        continue
                else:
                    data = json.loads(match.group(1))
                raw_data = data.get('data', '')
                rows = [r for r in raw_data.split(';') if r.strip()] if raw_data else []
                num = data.get('num', 0)
                total = data.get('total', 0)
                if rows:
                    fields = rows[0].split(',')
                    print(f'  period={period} {suffix}: total={total}, num={num}, {len(rows)}行, {len(fields)}字段')
                    print(f'    首行: {rows[0][:100]}')
                    if len(rows) > 1:
                        print(f'    末行: {rows[-1][:100]}')
                else:
                    print(f'  period={period} {suffix}: total={total}, num={num}, 无数据行')
        except Exception as e:
            pass

# ========== 2. 腾讯 - 全部K线周期 ==========
print()
print('='*100)
print('【腾讯K线(ifzq) - 全周期扫描】')
print('='*100)
periods = [
    ('日K', 'day'), ('周K', 'week'), ('月K', 'month'),
    ('60min', 'm60'), ('30min', 'm30'), ('15min', 'm15'),
    ('5min', 'm5'), ('1min', 'm1'),
]
for name, period in periods:
    url = f'https://ifzq.gtimg.cn/appstock/app/kline/mkline?param=sz000001,{period},,5'
    try:
        r = s.get(url, timeout=5)
        data = r.json()
        stock_data = data.get('data', {}).get('sz000001', {})
        for k, v in stock_data.items():
            if isinstance(v, list) and len(v) > 0:
                print(f'  {name}({period}) key={k}: {len(v)}行, {len(v[0])}字段, 示例={v[0]}')
    except Exception as e:
        print(f'  {name}({period}): 错误 {str(e)[:50]}')

# 前复权/后复权
print()
for name, fq in [('日K前复权', 'qfq'), ('日K后复权', 'hfq')]:
    url = f'https://ifzq.gtimg.cn/appstock/app/fqkline/get?_var=kline_day{name[-3:]}&param=sz000001,day,,,5,{fq}'
    try:
        r = s.get(url, timeout=5)
        text = r.text
        match = re.search(r'=(\{.*\})', text, re.DOTALL)
        if match:
            data = json.loads(match.group(1))
            stock_data = data.get('data', {}).get('sz000001', {})
            for k, v in stock_data.items():
                if isinstance(v, list) and len(v) > 0:
                    print(f'  {name} key={k}: {len(v)}行, {len(v[0])}字段, 示例={v[0]}')
    except Exception as e:
        print(f'  {name}: 错误 {str(e)[:50]}')

# ========== 3. 腾讯实时 - 批量6只股字段数验证 ==========
print()
print('='*100)
print('【腾讯实时 - 批量验证字段数一致性】')
print('='*100)
batch_codes = 'sz000001,sh600519,sz300750,sz000002,sh601398,sz002594'
r = s.get(f'http://qt.gtimg.cn/q={batch_codes}', timeout=10)
entries = r.text.strip().split(';')
for entry in entries:
    if '="' in entry and '~' in entry:
        parts = entry.split('~')
        code = parts[2] if len(parts) > 2 else '?'
        name = parts[1] if len(parts) > 1 else '?'
        print(f'  {code} {name}: {len(parts)}字段')

# ========== 4. 腾讯 - 是否有额外参数获取更多字段 ==========
print()
print('【腾讯实时 - 额外参数测试】')
# 测试r=1或_=时间戳等参数
import time
extra_params = [
    'sz000001',
    'sz000001,r_0.123',
    'sz000001?_var=v_sz000001',
]
for param in extra_params:
    try:
        url = f'http://qt.gtimg.cn/q={param}'
        r = s.get(url, timeout=5)
        text = r.text.strip()
        if '="' in text and '~' in text:
            content = text.split('="', 1)[1].rstrip('";')
            parts = content.split('~')
            print(f'  q={param}: {len(parts)}字段, 末尾字段=[85]{parts[85] if len(parts)>85 else "N/A"} [86]{parts[86] if len(parts)>86 else "N/A"} [87]{parts[87] if len(parts)>87 else "N/A"}')
        else:
            print(f'  q={param}: 格式不同, 前80字={text[:80]}')
    except Exception as e:
        print(f'  q={param}: 错误 {str(e)[:50]}')

# 腾讯secids格式(和东财一样的0.000001格式)
print()
print('【腾讯 - secids格式测试】')
secid_urls = [
    'http://qt.gtimg.cn/q=sz000001',
    'http://qt.gtimg.cn/q=0.000001',
    'http://qt.gtimg.cn/q=1.600519',
]
for url in secid_urls:
    try:
        r = s.get(url, timeout=5)
        text = r.text.strip()
        if '="' in text and '~' in text:
            content = text.split('="', 1)[1].rstrip('";')
            parts = content.split('~')
            print(f'  {url.split("q=")[-1]}: {len(parts)}字段, 名称={parts[1] if len(parts)>1 else "?"}')
        else:
            print(f'  {url.split("q=")[-1]}: 无数据')
    except Exception as e:
        print(f'  错误: {str(e)[:50]}')
