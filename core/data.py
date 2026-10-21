# -*- coding: utf-8 -*-
"""数据层：东方财富日线K线获取 + 本地CSV缓存"""
import os
import time
import datetime
import urllib.request
import urllib.parse

import numpy as np
import pandas as pd

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, 'data')
os.makedirs(DATA_DIR, exist_ok=True)

K_URL = 'https://push2his.eastmoney.com/api/qt/stock/kline/get'
QUOTE_URL = 'https://push2.eastmoney.com/api/qt/stock/get'
FIELDS1 = 'f1,f2,f3,f4,f5,f6'
FIELDS2 = 'f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61'
COLS = ['date', 'open', 'close', 'high', 'low', 'volume', 'amount',
        'amplitude', 'pct_chg', 'change', 'turnover']
NUM_COLS = ['open', 'close', 'high', 'low', 'volume', 'amount',
            'amplitude', 'pct_chg', 'change', 'turnover']


def to_secid(code):
    """'600519' / 'sh.600519' / '600519.SH' / '1.600519' -> eastmoney secid"""
    code = str(code).strip().lower().replace('.ss', '.sh')
    if '.' in code and not code[0].isdigit():
        parts = code.split('.')
        if parts[0] in ('sh', 'sz', 'bj'):
            num = parts[1]
        else:
            num = parts[0]
    else:
        num = ''.join(c for c in code if c.isdigit())
    if len(num) != 6:
        raise ValueError('无法识别的股票代码: %s' % code)
    if num[0] in ('6', '9'):
        return '1.' + num   # 上海
    if num[0] in ('0', '2', '3'):
        return '0.' + num   # 深圳
    raise ValueError('暂不支持该市场代码(北交所等): %s' % code)


def pure_code(code):
    return to_secid(code).split('.')[1]


def _http_json(url, retries=3, timeout=20):
    last_err = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={
                'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36',
                'Referer': 'https://quote.eastmoney.com/',
            })
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return __import__('json').loads(resp.read().decode('utf-8'))
        except Exception as e:  # noqa
            last_err = e
            time.sleep(1.0 + i)
    raise RuntimeError('请求失败 %s: %s' % (url, last_err))


def fetch_kline_eastmoney(code, start_date='1990-01-01', end_date=None):
    """东方财富前复权日线（含换手率/成交额）"""
    if end_date is None:
        end_date = datetime.date.today().strftime('%Y-%m-%d')
    secid = to_secid(code)
    params = urllib.parse.urlencode({
        'secid': secid, 'fields1': FIELDS1, 'fields2': FIELDS2,
        'klt': '101', 'fqt': '1',
        'beg': start_date.replace('-', ''), 'end': end_date.replace('-', ''),
    })
    data = _http_json(K_URL + '?' + params)
    obj = (data or {}).get('data') or {}
    name = obj.get('name') or ''
    klines = obj.get('klines') or []
    rows = [k.split(',') for k in klines]
    df = pd.DataFrame(rows, columns=COLS)
    for c in NUM_COLS:
        df[c] = pd.to_numeric(df[c], errors='coerce')
    df = df.dropna(subset=['close'])
    df = df[df['volume'] > 0].reset_index(drop=True)  # 剔除停牌日
    return df, name


def to_tx_symbol(code):
    secid = to_secid(code)
    mk, num = secid.split('.')
    return ('sh' if mk == '1' else 'sz') + num


def _tx_quote_fields(code):
    """腾讯实时报价字段：名称、流通股本(股)"""
    symbol = to_tx_symbol(code)
    url = 'https://qt.gtimg.cn/q=' + symbol
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0',
                                               'Referer': 'https://gu.qq.com/'})
    raw = urllib.request.urlopen(req, timeout=15).read().decode('gbk', 'ignore')
    body = raw.split('=', 1)[-1].strip().strip(';"')
    f = body.split('~')
    name = f[1] if len(f) > 2 else ''
    price = _f(f, 3)
    float_mcap = _f(f, 44)   # 单位: 亿元
    shares = float_mcap * 1e8 / price if price and float_mcap else None
    return name, shares


def _sina_qfq_factors(symbol):
    """新浪累计前复权因子：返回(除权日升序列表, 因子列表)。除权日前价格 = 不复权价 / f"""
    import json as _json
    import re as _re
    url = 'https://finance.sina.com.cn/realstock/company/%s/qfq.js' % symbol
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0',
                                               'Referer': 'https://finance.sina.com.cn/'})
    raw = urllib.request.urlopen(req, timeout=15).read().decode('utf-8', 'ignore')
    m = _re.search(r'\{.*\}', raw)
    obj = _json.loads(m.group(0))
    items = sorted([(e['d'], float(e['f'])) for e in obj.get('data') or []])
    return [i[0] for i in items], [i[1] for i in items]


def fetch_kline_tencent(code, start_date='2015-01-01', end_date=None):
    """腾讯不复权日线(分页) x 新浪复权因子 -> 比例前复权"""
    if end_date is None:
        end_date = datetime.date.today().strftime('%Y-%m-%d')
    symbol = to_tx_symbol(code)
    name, float_shares = ('', None)
    try:
        name, float_shares = _tx_quote_fields(code)
    except Exception:
        pass
    rows, cursor = [], end_date
    while True:
        url = ('https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=%s,day,%s,%s,800,'
               % (symbol, start_date, cursor))
        data = _http_json(url)
        obj = ((data or {}).get('data') or {}).get(symbol) or {}
        days = obj.get('day') or obj.get('qfqday') or []
        if not days:
            break
        rows = days + rows
        first = str(days[0][0])
        if len(days) < 800 or first <= start_date:
            break
        cursor = (datetime.datetime.strptime(first, '%Y-%m-%d').date()
                  - datetime.timedelta(days=3)).strftime('%Y-%m-%d')
        time.sleep(0.3)
    df = pd.DataFrame([r[:6] for r in rows],
                      columns=['date', 'open', 'close', 'high', 'low', 'volume'])
    df = df.drop_duplicates(subset='date', keep='last').sort_values('date')
    for c in ('open', 'close', 'high', 'low', 'volume'):
        df[c] = pd.to_numeric(df[c], errors='coerce')
    df = df.dropna(subset=['close'])
    df = df[(df['volume'] > 0) & (df['close'] > 0)].reset_index(drop=True)
    if len(df) == 0:
        return df, name
    # 应用比例前复权因子
    try:
        fd, ff = _sina_qfq_factors(symbol)
        pos = np.searchsorted(fd, df['date'].values, side='right') - 1
        pos = np.clip(pos, 0, len(ff) - 1)
        factor = np.where(df['date'].values >= fd[0], np.array(ff)[pos], np.array(ff)[0])
        for c in ('open', 'close', 'high', 'low'):
            df[c] = df[c] / factor
    except Exception:
        pass  # 因子获取失败则用不复权数据（除权日会有假缺口，波段判定偏保守）
    avg_px = (df['high'] + df['low'] + df['close']) / 3.0
    df['amount'] = df['volume'] * 100.0 * avg_px
    if float_shares and float_shares > 0:
        df['turnover'] = df['volume'] * 100.0 / float_shares * 100.0
    else:
        df['turnover'] = np.nan
    prev_c = df['close'].shift(1)
    df['amplitude'] = (df['high'] - df['low']) / prev_c * 100
    df['pct_chg'] = (df['close'] - prev_c) / prev_c * 100
    df['change'] = df['close'] - prev_c
    return df, name


def _f(arr, i):
    try:
        v = float(arr[i])
        return v if v > 0 else None
    except (IndexError, ValueError):
        return None


def fetch_kline(code, start_date='1990-01-01', end_date=None):
    """多源自动切换：东财 -> 腾讯"""
    try:
        return fetch_kline_eastmoney(code, start_date, end_date)
    except Exception as e1:
        try:
            return fetch_kline_tencent(code, start_date, end_date)
        except Exception as e2:
            raise RuntimeError('数据拉取失败 eastmoney:%s tencent:%s' % (e1, e2))


def cache_path(code):
    return os.path.join(DATA_DIR, pure_code(code) + '.csv')


def meta_path(code):
    return os.path.join(DATA_DIR, pure_code(code) + '.meta')


def get_kline(code, start_date='1990-01-01', end_date=None, refresh=True):
    """带缓存的日线获取：缓存不足时增量拉取并合并"""
    if end_date is None:
        end_date = datetime.date.today().strftime('%Y-%m-%d')
    path, cached = cache_path(code), None
    if os.path.exists(path):
        try:
            cached = pd.read_csv(path, dtype={'date': str})
        except Exception:
            cached = None
    need_fetch = refresh or cached is None or cached.empty
    fetch_start = start_date
    if cached is not None and not cached.empty:
        have_end = cached['date'].max()
        if have_end >= end_date or not refresh:
            out = cached[(cached['date'] >= start_date) & (cached['date'] <= end_date)]
            return out.reset_index(drop=True)
        fetch_start = have_end  # 增量
    fresh, name = fetch_kline(code, fetch_start, end_date)
    if cached is not None and not cached.empty:
        df = pd.concat([cached, fresh], ignore_index=True)
        df = df.drop_duplicates(subset='date', keep='last')
    else:
        df = fresh
    df = df.sort_values('date').reset_index(drop=True)
    df.to_csv(path, index=False)
    if name:
        with open(meta_path(code), 'w') as f:
            f.write(name)
    out = df[(df['date'] >= start_date) & (df['date'] <= end_date)]
    return out.reset_index(drop=True)


def get_name(code):
    p = meta_path(code)
    if os.path.exists(p):
        try:
            with open(p) as f:
                n = f.read().strip()
                if n:
                    return n
        except Exception:
            pass
    for fn in (_name_eastmoney, _name_tencent):
        try:
            n = fn(code)
            if n:
                return n
        except Exception:
            continue
    return str(code)


def _name_eastmoney(code):
    params = urllib.parse.urlencode({'secid': to_secid(code), 'fields': 'f57,f58'})
    data = _http_json(QUOTE_URL + '?' + params, retries=1)
    obj = (data or {}).get('data') or {}
    return obj.get('f58') or obj.get('f57') or ''


def _name_tencent(code):
    name, _ = _tx_quote_fields(code)
    return name


def get_float_mcap(code):
    """流通市值(元)，失败返回 None"""
    try:
        params = urllib.parse.urlencode({'secid': to_secid(code), 'fields': 'f57,f58,f117'})
        data = _http_json(QUOTE_URL + '?' + params, retries=2)
        obj = (data or {}).get('data') or {}
        v = obj.get('f117')
        return float(v) if v not in (None, '-') else None
    except Exception:
        return None
