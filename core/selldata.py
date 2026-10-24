# -*- coding: utf-8 -*-
"""卖出通知工具的数据层：东方财富数据中心报表拉取 + 本地 CSV 缓存

全部走 datacenter-web 的通用查询接口（与 core/data.py 的行情接口同源，免 key）：
  RPT_VALUEANALYSIS_DET   每日估值（PE-TTM / PB(MRQ) / PCF / 总市值）→ 信号 A 的分位序列
  RPT_LICO_FN_CPD         财报主要指标（营收/归母净利/ROE/EPS/是否分配/所属行业）→ 信号 B 第1组
  RPT_DMSK_FN_BALANCE     资产负债表（负债率/应收/存货/总资产）→ 信号 B 第2组
  RPT_DMSK_FN_CASHFLOW    现金流量表（经营现金流净额）→ 信号 B 第1组（OCF/净利润）
  RPT_SHAREBONUS_DET      分红方案（每10股派息/总股本）→ 信号 B 第3组
  RPT_PUBLIC_OP_NEWPREDICT 业绩预告（类型/公告日）→ 信号 C 的同行预警
  RPT_F10_EH_FREEHOLDERS  十大流通股东（名称/持股数）→ 信号 D 的社保与保险减持
缓存写在 data/sell/，同一类别当日内不重复拉取（.stamp 记录拉取日期）；
行业指数日线复用 core/data.py 的行情接口，代码规则见 resolve_index_secid。
"""
import datetime
import json
import os
import time
import urllib.parse
import urllib.request

import pandas as pd

from . import data as mkt_data

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SELL_DIR = os.path.join(BASE_DIR, 'data', 'sell')
os.makedirs(SELL_DIR, exist_ok=True)

DC_URL = 'https://datacenter-web.eastmoney.com/api/data/v1/get'
PAGE_SIZE = 500
DEFAULT_INDEX = '1.000300'      # 行业指数留空时的兜底：沪深300


def _today():
    return datetime.date.today().strftime('%Y-%m-%d')


def _dc_get(params, retries=3, timeout=20):
    url = DC_URL + '?' + urllib.parse.urlencode(params)
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={
                'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36',
                'Referer': 'https://data.eastmoney.com/',
            })
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode('utf-8'))
        except Exception as e:  # noqa
            last = e
            time.sleep(1.0 + i)
    raise RuntimeError('数据中心请求失败 %s: %s' % (params.get('reportName'), last))


def _dc_page(report_name, flt, sort_columns=None, page=1, page_size=PAGE_SIZE):
    p = {'reportName': report_name, 'columns': 'ALL', 'filter': flt,
         'pagesize': page_size, 'page': page, 'source': 'WEB', 'client': 'WEB'}
    if sort_columns:
        p['sortColumns'] = sort_columns
        p['sortTypes'] = '-1'
    return _dc_get(p)


def _dc_all(report_name, flt, sort_columns=None, max_pages=12):
    """按排序列翻页取全（数据中心单页上限不稳，取到空页/超页数为止）"""
    rows, page = [], 0
    while page < max_pages:
        page += 1
        d = _dc_page(report_name, flt, sort_columns, page)
        res = (d or {}).get('result') or {}
        chunk = res.get('data') or []
        rows.extend(chunk)
        if len(chunk) < PAGE_SIZE or page >= int(res.get('pages') or page):
            break
    return rows


def _d(v):
    """数据中心日期 '2026-06-30 00:00:00' -> '2026-06-30'（空给 ''）"""
    s = str(v or '')
    return s[:10] if s and s[:4].isdigit() else ''


def _df(rows, date_col):
    """rows -> DataFrame：日期截断、按日期升序、去重"""
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df[date_col] = df[date_col].map(_d)
    df = df[df[date_col] != '']
    df = df.drop_duplicates(subset=date_col, keep='last')
    return df.sort_values(date_col).reset_index(drop=True)


# ---------------- 缓存读写 ----------------

def _key(code):
    """缓存文件名：股票代码归一化；指数等自定义 key（如 '1_000300'）原样使用"""
    s = str(code).strip()
    try:
        return mkt_data.pure_code(s)
    except Exception:
        return s.replace('.', '_').replace('/', '_')


def _paths(code, kind):
    base = os.path.join(SELL_DIR, '%s_%s' % (_key(code), kind))
    return base + '.csv', base + '.stamp'


def _stamp(code, kind):
    try:
        with open(_paths(code, kind)[1]) as f:
            return f.read().strip()
    except Exception:
        return ''


def _fresh(code, kind, refresh):
    """当日已拉取过且没强制刷新 → 用缓存"""
    return (not refresh) and _stamp(code, kind) == _today()


def _load_cached(code, kind, date_col=None, since=None):
    path = _paths(code, kind)[0]
    if not os.path.exists(path):
        return None
    try:
        df = pd.read_csv(path, dtype=str)
    except Exception:
        return None
    if df.empty:
        return df
    if since and date_col and date_col in df.columns:
        df = df[df[date_col].astype(str).str[:10] >= since].reset_index(drop=True)
    return df


def _store(code, kind, df, date_col):
    """落盘保留原列名（回读时按同一列名过滤）；当日 stamp"""
    path, sp = _paths(code, kind)
    df.to_csv(path, index=False)
    with open(sp, 'w') as f:
        f.write(_today())
    return df


def _cached_fetch(code, kind, date_col, fetcher, refresh=False, since=None):
    """通用：新鲜缓存 → 直接读；否则拉全量落盘再读（拉失败退回旧缓存）"""
    if _fresh(code, kind, refresh):
        df = _load_cached(code, kind, date_col, since)
        if df is not None and not df.empty:
            return df
    try:
        df = fetcher()
    except Exception:
        old = _load_cached(code, kind, date_col, since)
        if old is not None and not old.empty:
            return old
        raise
    return _store(code, kind, df, date_col)


# ---------------- 各类报表拉取 ----------------

def fetch_value(code, start_date):
    """每日估值：只留信号 A 用得到的列"""
    flt = '(SECURITY_CODE="%s")' % mkt_data.pure_code(code)
    rows = _dc_all('RPT_VALUEANALYSIS_DET', flt, 'TRADE_DATE')
    keep = []
    for r in rows:
        d = _d(r.get('TRADE_DATE'))
        if d and d >= start_date:
            keep.append({'TRADE_DATE': d, 'CLOSE_PRICE': r.get('CLOSE_PRICE'),
                         'PE_TTM': r.get('PE_TTM'), 'PB_MRQ': r.get('PB_MRQ'),
                         'PCF_OCF_TTM': r.get('PCF_OCF_TTM'),
                         'TOTAL_MARKET_CAP': r.get('TOTAL_MARKET_CAP'),
                         'BOARD_NAME': r.get('BOARD_NAME')})
    return _df(keep, 'TRADE_DATE')


def fetch_fina(code):
    """财报主要指标（累计值，YTD 口径）"""
    flt = '(SECURITY_CODE="%s")' % mkt_data.pure_code(code)
    rows = _dc_all('RPT_LICO_FN_CPD', flt, 'REPORTDATE', max_pages=3)
    keep = [{'REPORTDATE': r.get('REPORTDATE'), 'NOTICE_DATE': r.get('NOTICE_DATE'),
             'QDATE': r.get('QDATE'), 'DATEMMDD': r.get('DATEMMDD'),
             'TOTAL_OPERATE_INCOME': r.get('TOTAL_OPERATE_INCOME'),
             'PARENT_NETPROFIT': r.get('PARENT_NETPROFIT'),
             'WEIGHTAVG_ROE': r.get('WEIGHTAVG_ROE'), 'BASIC_EPS': r.get('BASIC_EPS'),
             'ASSIGNDSCRPT': r.get('ASSIGNDSCRPT'), 'BOARD_NAME': r.get('BOARD_NAME'),
             'BOARD_CODE': r.get('BOARD_CODE')} for r in rows]
    return _df(keep, 'REPORTDATE')


def fetch_balance(code):
    flt = '(SECURITY_CODE="%s")' % mkt_data.pure_code(code)
    rows = _dc_all('RPT_DMSK_FN_BALANCE', flt, 'REPORT_DATE', max_pages=3)
    keep = [{'REPORT_DATE': r.get('REPORT_DATE'), 'NOTICE_DATE': r.get('NOTICE_DATE'),
             'TOTAL_ASSETS': r.get('TOTAL_ASSETS'), 'TOTAL_LIABILITIES': r.get('TOTAL_LIABILITIES'),
             'DEBT_ASSET_RATIO': r.get('DEBT_ASSET_RATIO'), 'MONETARYFUNDS': r.get('MONETARYFUNDS'),
             'ACCOUNTS_RECE': r.get('ACCOUNTS_RECE'), 'INVENTORY': r.get('INVENTORY')} for r in rows]
    return _df(keep, 'REPORT_DATE')


def fetch_cashflow(code):
    flt = '(SECURITY_CODE="%s")' % mkt_data.pure_code(code)
    rows = _dc_all('RPT_DMSK_FN_CASHFLOW', flt, 'REPORT_DATE', max_pages=3)
    keep = [{'REPORT_DATE': r.get('REPORT_DATE'), 'NOTICE_DATE': r.get('NOTICE_DATE'),
             'NETCASH_OPERATE': r.get('NETCASH_OPERATE')} for r in rows]
    return _df(keep, 'REPORT_DATE')


def fetch_dividend(code):
    flt = '(SECURITY_CODE="%s")' % mkt_data.pure_code(code)
    rows = _dc_all('RPT_SHAREBONUS_DET', flt, 'EX_DIVIDEND_DATE', max_pages=3)
    keep = [{'REPORT_DATE': r.get('REPORT_DATE'), 'NOTICE_DATE': r.get('NOTICE_DATE'),
             'EX_DIVIDEND_DATE': r.get('EX_DIVIDEND_DATE'),
             'PRETAX_BONUS_RMB': r.get('PRETAX_BONUS_RMB'), 'TOTAL_SHARES': r.get('TOTAL_SHARES'),
             'ASSIGN_PROGRESS': r.get('ASSIGN_PROGRESS')} for r in rows]
    return _df(keep, 'REPORT_DATE')


def fetch_forecast(code, since):
    """业绩预告/快报：只取 since（含）以后公告的"""
    flt = '(SECURITY_CODE="%s")(NOTICE_DATE>=\'%s\')' % (mkt_data.pure_code(code), since)
    rows = _dc_all('RPT_PUBLIC_OP_NEWPREDICT', flt, 'NOTICE_DATE', max_pages=2)
    keep = [{'REPORT_DATE': r.get('REPORT_DATE'), 'NOTICE_DATE': r.get('NOTICE_DATE'),
             'PREDICT_TYPE': r.get('PREDICT_TYPE'), 'FORECAST_STATE': r.get('FORECAST_STATE'),
             'PREDICT_CONTENT': (r.get('PREDICT_CONTENT') or '')[:60],
             'SECURITY_NAME_ABBR': r.get('SECURITY_NAME_ABBR')} for r in rows]
    return _df(keep, 'NOTICE_DATE')


def fetch_holders(code):
    """十大流通股东（按报告期倒序取最近一期页，500 行足够覆盖 8 个报告期）"""
    flt = '(SECURITY_CODE="%s")' % mkt_data.pure_code(code)
    rows = _dc_page('RPT_F10_EH_FREEHOLDERS', flt, 'END_DATE', 1, 500).get('result') or {}
    out = [(r.get('END_DATE'), r.get('NOTICE_DATE'), r.get('HOLDER_NAME'),
            r.get('HOLD_NUM'), r.get('HOLD_RATIO'), r.get('HOLD_NUM_CHANGE'))
           for r in (rows.get('data') or [])]
    df = pd.DataFrame(out, columns=['END_DATE', 'NOTICE_DATE', 'HOLDER_NAME',
                                    'HOLD_NUM', 'HOLD_RATIO', 'HOLD_NUM_CHANGE'])
    if df.empty:
        return df
    df['END_DATE'] = df['END_DATE'].map(_d)
    df['NOTICE_DATE'] = df['NOTICE_DATE'].map(_d)
    for c in ('HOLD_NUM', 'HOLD_RATIO'):
        df[c] = pd.to_numeric(df[c], errors='coerce')
    return df.sort_values(['END_DATE']).reset_index(drop=True)


# ---------------- 对外读取口（带当日缓存） ----------------

def get_value(code, years=10, refresh=False):
    start = (datetime.date.today() - datetime.timedelta(days=int(years * 365) + 5)
             ).strftime('%Y-%m-%d')
    df = _cached_fetch(code, 'value', 'TRADE_DATE',
                       lambda: fetch_value(code, start), refresh)
    for c in ('CLOSE_PRICE', 'PE_TTM', 'PB_MRQ', 'PCF_OCF_TTM', 'TOTAL_MARKET_CAP'):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors='coerce')
    return df


def get_fina(code, refresh=False):
    return _cached_fetch(code, 'fina', 'REPORTDATE', lambda: fetch_fina(code), refresh)


def get_balance(code, refresh=False):
    return _cached_fetch(code, 'balance', 'REPORT_DATE', lambda: fetch_balance(code), refresh)


def get_cashflow(code, refresh=False):
    return _cached_fetch(code, 'cashflow', 'REPORT_DATE', lambda: fetch_cashflow(code), refresh)


def get_dividend(code, refresh=False):
    return _cached_fetch(code, 'dividend', 'REPORT_DATE', lambda: fetch_dividend(code), refresh)


def get_forecast(code, days=60, refresh=False):
    since = (datetime.date.today() - datetime.timedelta(days=days)).strftime('%Y-%m-%d')
    return _cached_fetch(code, 'forecast', 'NOTICE_DATE',
                         lambda: fetch_forecast(code, since), refresh)


def get_holders(code, refresh=False):
    return _cached_fetch(code, 'holders', 'END_DATE', lambda: fetch_holders(code), refresh)


def get_industry(code, fina=None):
    """行业名（信号 C 的“同行”按它匹配）：先财务摘要的板块名，退回估值表的 BOARD_NAME"""
    try:
        df = fina if fina is not None else get_fina(code)
        if df is not None and len(df) and 'BOARD_NAME' in df.columns:
            names = [str(x) for x in df['BOARD_NAME'] if x and str(x) != 'nan']
            if names:
                return names[-1]
    except Exception:
        pass
    try:
        v = get_value(code, years=1)
        if v is not None and len(v) and 'BOARD_NAME' in v.columns:
            x = v['BOARD_NAME'].iloc[-1]
            if x and str(x) != 'nan':
                return str(x)
    except Exception:
        pass
    return ''


# ---------------- 行业/宽基指数日线 ----------------

def resolve_index_secid(spec):
    """'1.000300'/'sh.000300'/'000300'/'399997' -> secid。
    纯 6 位的 000xxx 视为沪市指数系列（中证/沪深指数发布号），深市与国证以 399 开头。"""
    s = str(spec or '').strip().lower().replace('.ss', '.sh')
    if not s:
        return DEFAULT_INDEX
    if s.startswith('1.') or s.startswith('0.'):
        return s
    if s.startswith('sh.'):
        return '1.' + s[3:]
    if s.startswith('sz.'):
        return '0.' + s[3:]
    num = ''.join(c for c in s if c.isdigit())
    if len(num) != 6:
        raise ValueError('无法识别的指数代码: %s' % spec)
    if num.startswith('399'):
        return '0.' + num
    if num.startswith('000'):
        return '1.' + num        # 中证指数在沪市行情源
    if num[0] in ('6', '9'):
        return '1.' + num
    return '0.' + num


def _index_kline_em(secid, start_date, end_date):
    params = urllib.parse.urlencode({
        'secid': secid, 'fields1': mkt_data.FIELDS1,
        'fields2': 'f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61',
        'klt': '101', 'fqt': '1',
        'beg': start_date.replace('-', ''), 'end': end_date.replace('-', '')})
    raw = mkt_data._http_json(mkt_data.K_URL + '?' + params, retries=2)
    obj = (raw or {}).get('data') or {}
    rows = [k.split(',')[:7] for k in (obj.get('klines') or [])]
    return rows, obj.get('name') or ''


def _index_kline_tx(secid, start_date, end_date):
    """腾讯指数日线（分页回溯）：东财 push2his 对指数 secid 会断连时的兜底通道"""
    mk, num = secid.split('.')
    symbol = ('sh' if mk == '1' else 'sz') + num
    rows, cursor, name = [], end_date, ''
    while True:
        url = ('https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=%s,day,%s,%s,800,qfq'
               % (symbol, start_date, cursor))
        raw = mkt_data._http_json(url, retries=2)
        obj = ((raw or {}).get('data') or {}).get(symbol) or {}
        days = obj.get('day') or obj.get('qfqday') or []
        if not days:
            break
        rows = [d[:6] for d in days] + rows
        first = str(days[0][0])
        if len(days) < 800 or first <= start_date:
            break
        cursor = (datetime.datetime.strptime(first, '%Y-%m-%d').date()
                  - datetime.timedelta(days=3)).strftime('%Y-%m-%d')
        time.sleep(0.3)
    return rows, name


def get_index_kline(spec, start_date, refresh=False):
    """指数日线（date/open/close/high/low/volume）：当日缓存；东财→腾讯双源"""
    secid = resolve_index_secid(spec)
    key = secid.replace('.', '_')
    if not refresh and _stamp(key, 'index') == _today():
        df = _load_cached(key, 'index', 'date', start_date)
        if df is not None and not df.empty:
            return df
    end = _today()
    rows = []
    try:
        rows, name = _index_kline_em(secid, start_date, end)
    except Exception:
        rows = []
    if not rows:
        rows, name = _index_kline_tx(secid, start_date, end)
    cols = ['date', 'open', 'close', 'high', 'low', 'volume']
    df = pd.DataFrame([r[:6] for r in rows], columns=cols)
    if len(df):
        df = df.drop_duplicates(subset='date', keep='last').sort_values('date')
        for c in ('open', 'close', 'high', 'low'):
            df[c] = pd.to_numeric(df[c], errors='coerce')
        df = df[df['date'] >= start_date].reset_index(drop=True)
    path, sp = _paths(key, 'index')
    df.to_csv(path, index=False)
    with open(sp, 'w') as f:
        f.write(_today())
    return df
