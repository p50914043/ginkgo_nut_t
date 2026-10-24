# -*- coding: utf-8 -*-
"""卖出通知工具：四路信号判定 + 每日汇总 + 历史回测

信号与需求编号对应：
  A 估值过高    非金融 PE-TTM 5年分位 / 银行 PB 10年分位 / 保险 PB（代理 PEV）5年分位 / 周期 PB 10年分位
                ≥70%（可配）黄色、≥80%（可配）红色
  B 基本面恶化  同源性分三组，跨组才计数：第1组 经营质量（营收/利润连续2季负增长、ROE 连降且低于行业均值、
                OCF/净利润<0.5 连续2季）；第2组 资产负债（负债率同比升超10pct、应收/存货增速显著超营收）；
                第3组 分红与治理（分红同比降超30%或取消；管理层变动/审计非标无公开数据源，v1 不自动化）
                任一命中 → 黄色；命中组数 ≥ b_red_groups → 红色
  C 行业与宏观  行业指数自近期高点回撤超 20% 且 同行多家发业绩预警 → 黄色（两条件同时成立）
  D 社保/保险   十大流通股东中社保、保险机构最新季度期末较上季减持或退出 → 黄色
只出风险预警，不计算收益、不给仓位金额、不接管下单；历史回测同样只是复盘提醒时点。
"""
import bisect
import datetime
import json
import os

import pandas as pd

from . import data as mkt_data, selldata, config

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_PATH = os.path.join(BASE_DIR, 'data', 'sell_log.jsonl')

LEVELS = {'none': 0, 'yellow': 1, 'red': 2}
LEVEL_MARK = {'none': '⚪', 'yellow': '🟡', 'red': '🔴'}
LEVEL_CN = {'none': '无预警', 'yellow': '黄色预警', 'red': '红色预警'}
SIG_CN = {'A': 'A 估值过高', 'B': 'B 基本面恶化', 'C': 'C 行业与宏观', 'D': 'D 社保/保险卖出'}

TYPE_CN = {'value': '非金融', 'bank': '银行', 'insurance': '保险', 'cycle': '周期'}
# type -> (估值列, 指标名, 窗口年数对应的配置键)：PEV 缺一致数据源，保险暂用 PB 代理 5 年窗口
VAL_SPEC = {
    'value': ('PE_TTM', 'PE-TTM', 'a_years_pe'),
    'bank': ('PB_MRQ', 'PB', 'a_years_pb'),
    'insurance': ('PB_MRQ', 'PB（代理PEV）', 'a_years_pe'),
    'cycle': ('PB_MRQ', 'PB', 'a_years_pb'),
}
NEG_WARN_WORDS = ('亏', '减')     # 业绩预警类型口径：含“亏/减”且非“扭亏”


def _num(x):
    try:
        v = float(x)
        return v if v == v else None       # NaN -> None
    except (TypeError, ValueError):
        return None


def _max_level(levels):
    best = 'none'
    for lv in levels:
        if LEVELS.get(lv, 0) > LEVELS[best]:
            best = lv
    return best


def _prev_year(date_str):
    """'2026-06-30' -> '2025-06-30'（财报固定报告期，无需担心 2/29）"""
    s = str(date_str or '')[:10]
    if len(s) < 10 or s[:4] < '1990':
        return ''
    return '%d%s' % (int(s[:4]) - 1, s[4:])


def _years_ago(date_str, years):
    d = datetime.date.fromisoformat(str(date_str)[:10])
    return (d - datetime.timedelta(days=int(years * 365.25))).isoformat()


def _conv(df, cols):
    if df is None or df.empty:
        return df
    for c in cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors='coerce')
    return df


def _rows_by_date(df, date_col):
    """DataFrame -> {date: row dict}（读缓存后数值列是字符串，统一转 float）"""
    out = {}
    if df is None or df.empty:
        return out
    for rec in df.to_dict('records'):
        d = str(rec.get(date_col) or '')[:10]
        if d:
            out[d] = rec
    return out


# ---------------- 信号 A：估值过高分位 ----------------

def percentile_of(vals, cur):
    """当前值在窗口序列里的分位（%）：小于等于它的占比"""
    vals = [v for v in vals if v is not None]
    if not vals or cur is None:
        return None
    return sum(1 for v in vals if v <= cur) * 100.0 / len(vals)


def signal_a(code, stype, sn, vdf=None):
    """亮灯仍按类型选定的口径与窗口判定；但无论什么类型，PE-TTM 与 PB-MRQ 的分位都一并算出供展示"""
    col, metric_cn, years_key = VAL_SPEC.get(stype) or VAL_SPEC['value']
    years = int(sn.get(years_key) or 5)
    y_pe = int(sn.get('a_years_pe') or 5)
    y_pb = int(sn.get('a_years_pb') or 10)
    try:
        df = vdf if vdf is not None else selldata.get_value(code, years=max(years, y_pe, y_pb))
    except Exception as e:
        return {'level': 'none', 'status': '数据失败', 'detail': '估值数据拉取失败：%s' % str(e)[:60]}
    if df is None or df.empty or 'TRADE_DATE' not in df.columns:
        return {'level': 'none', 'status': '缺数据', 'detail': '没有估值历史'}
    _conv(df, [c for c in ('PE_TTM', 'PB_MRQ') if c in df.columns])
    dates = df['TRADE_DATE'].astype(str).str[:10]

    def stat(cname, yrs):
        """某估值列：末值 + 近 yrs 年窗口内的分位（不足 60 根不给分位）"""
        if cname not in df.columns:
            return {'val': None, 'pct': None, 'n': 0, 'years': yrs, 'date': None}
        pairs = [(d, _num(v)) for d, v in zip(dates, df[cname]) if _num(v) is not None]
        if not pairs:
            return {'val': None, 'pct': None, 'n': 0, 'years': yrs, 'date': None}
        last_date, last_val = pairs[-1]
        cutoff = _years_ago(last_date, yrs)
        win = [v for d, v in pairs if d >= cutoff]
        pct = percentile_of(win, last_val) if len(win) >= 60 else None
        return {'val': round(last_val, 3),
                'pct': round(pct, 1) if pct is not None else None,
                'n': len(win), 'years': yrs, 'date': last_date}
    pe = stat('PE_TTM', y_pe)
    pb = stat('PB_MRQ', y_pb)
    prim = stat(col, years)   # 主判按类型口径与窗口（保险为 PB 代理 PEV 的 5 年），与展示用的 pb(10年) 分开
    res = {'status': 'ok', 'metric': metric_cn, 'years': prim['years'],
           'value': prim['val'], 'pct': prim['pct'], 'n_window': prim['n'],
           'date': prim.get('date'), 'pe': pe, 'pb': pb}
    if prim['val'] is None or prim['pct'] is None:
        res['level'] = 'none'
        res['status'] = '缺数据'
        res['detail'] = '估值历史仅 %d 根，不足分位计算' % prim['n']
        return res
    res['level'] = 'none'
    if prim['pct'] >= float(sn.get('a_red') or 80):
        res['level'] = 'red'
    elif prim['pct'] >= float(sn.get('a_yellow') or 70):
        res['level'] = 'yellow'
    res['detail'] = '%s %.3f，位于近 %d 年 %s 分位（窗口 %d 日）' % (
        metric_cn, prim['val'], prim['years'],
        ('%.1f%%' % prim['pct']), prim['n'])
    return res


def dividend_yield_ttm(div_df, price):
    """TTM 股息率%：近 365 天内除息的每股税前派息（每10股/10）累加 ÷ 现价。div_df 为分红表"""
    if div_df is None or getattr(div_df, 'empty', True) or not price:
        return None
    today = datetime.date.today()
    since = (today - datetime.timedelta(days=366)).isoformat()
    upto = today.isoformat()
    tot = 0.0
    for rec in div_df.to_dict('records'):
        exd = str(rec.get('EX_DIVIDEND_DATE') or '')[:10]
        per10 = _num(rec.get('PRETAX_BONUS_RMB'))
        if exd and since <= exd <= upto and per10:
            tot += per10 / 10.0
    return round(tot / float(price) * 100, 2)


# ---------------- 信号 B：基本面恶化（跨组计数） ----------------

def _b_group1(fina_map, cf_map, sn, roe_ref):
    """第1组 经营质量：营收/利润连续2季负增长、ROE 连降且低于行业均值、OCF/净利润<0.5 连续2季"""
    hits = []
    dates = sorted(fina_map)[-2:]
    if len(dates) == 2:
        revs, nps, note = [], [], []
        for d in dates:
            r = fina_map[d]
            p = fina_map.get(_prev_year(d)) or {}
            rv, pv = _num(r.get('TOTAL_OPERATE_INCOME')), _num(p.get('TOTAL_OPERATE_INCOME'))
            np_, pn = _num(r.get('PARENT_NETPROFIT')), _num(p.get('PARENT_NETPROFIT'))
            revs.append((rv - pv) / abs(pv) * 100 if rv is not None and pv else None)
            nps.append((np_ - pn) / abs(pn) * 100 if np_ is not None and pn else None)
        if all(v is not None and v < 0 for v in revs):
            hits.append('营收连续2期负增长（%s / %s）' % ('%+.1f%%' % revs[0], '%+.1f%%' % revs[1]))
        if all(v is not None and v < 0 for v in nps):
            hits.append('归母净利连续2期负增长（%s / %s）' % ('%+.1f%%' % nps[0], '%+.1f%%' % nps[1]))
    # ROE：最近 b_roe_years 份年报连降，且最新低于行业均值（清单同行平均）
    need = max(2, int(sn.get('b_roe_years') or 3))
    ann = [(d, _num(fina_map[d].get('WEIGHTAVG_ROE'))) for d in sorted(fina_map)
           if d[5:] == '12-31' and _num(fina_map[d].get('WEIGHTAVG_ROE')) is not None]
    if len(ann) >= need:
        recent = ann[-need:]
        falling = all(recent[i][1] < recent[i - 1][1] for i in range(1, len(recent)))
        latest_roe = recent[-1][1]
        if falling and roe_ref is not None and latest_roe < roe_ref:
            hits.append('ROE 近 %d 年连降且低于行业均值（%.1f%% < %.1f%%）' % (
                need, latest_roe, roe_ref))
    # OCF/净利润：最近两期都要净利润>0 且比值低于阈值
    cf_dates = sorted(cf_map)[-2:]
    if len(cf_dates) == 2 and all(d in fina_map for d in cf_dates):
        oks = []
        for d in cf_dates:
            np_ = _num(fina_map[d].get('PARENT_NETPROFIT'))
            ocf = _num(cf_map[d].get('NETCASH_OPERATE'))
            oks.append(ocf / np_ if (np_ and np_ > 0 and ocf is not None) else None)
        thr = float(sn.get('b_ocf_ratio') or 0.5)
        if all(v is not None and v < thr for v in oks):
            hits.append('经营现金流/净利润 连续2期 <%.1f（%.2f / %.2f）' % (thr, oks[0], oks[1]))
    return hits


def _b_group2(fina_map, bal_map, sn):
    """第2组 资产负债：负债率同比升超阈值；应收/存货增速显著超营收"""
    hits = []
    dates = sorted(bal_map)
    if dates:
        d = dates[-1]
        cur = _num(bal_map[d].get('DEBT_ASSET_RATIO'))
        prev = _num((bal_map.get(_prev_year(d)) or {}).get('DEBT_ASSET_RATIO'))
        up = float(sn.get('b_debt_up') or 10.0)
        if cur is not None and prev is not None and cur - prev > up:
            hits.append('资产负债率同比升 %.1fpct（%.1f%% → %.1f%%）' % (cur - prev, prev, cur))
        # 应收+存货平均增速 − 营收增速
        frow, prow = fina_map.get(d) or {}, fina_map.get(_prev_year(d)) or {}
        rev_c, rev_p = _num(frow.get('TOTAL_OPERATE_INCOME')), _num(prow.get('TOTAL_OPERATE_INCOME'))
        rev_g = (rev_c - rev_p) / abs(rev_p) * 100 if rev_c is not None and rev_p else None
        grows = []
        for col in ('ACCOUNTS_RECE', 'INVENTORY'):
            c, p = _num(bal_map[d].get(col)), _num((bal_map.get(_prev_year(d)) or {}).get(col))
            if c is not None and p is not None and p > 0:
                grows.append((c - p) / p * 100)
        gap = float(sn.get('b_growth_gap') or 30.0)
        if grows and rev_g is not None:
            avg_g = sum(grows) / len(grows)
            if avg_g - rev_g > gap:
                hits.append('应收/存货平均增速超营收 %.1fpct（%.1f%% vs %.1f%%）' % (
                    avg_g - rev_g, avg_g, rev_g))
    return hits


def _b_group3(fina_map, div_map, sn):
    """第3组 分红与治理：最近一个已披露年报的完整财年 vs 上一个有分红的年度，
    总额同比降超阈值或取消（管理层/审计无公开结构化数据，v1 不自动化）。
    只比完整财年，避免“当年仅披露中期分红”被错当成陡降。"""
    hits = []
    divs = {}
    for d, r in div_map.items():
        per10, shares = _num(r.get('PRETAX_BONUS_RMB')), _num(r.get('TOTAL_SHARES'))
        if not per10 or not shares:
            continue
        y = str(r.get('REPORT_DATE') or d)[:4]
        if y:
            divs[y] = divs.get(y, 0.0) + per10 / 10.0 * shares
    ann_years = {d[:4] for d in fina_map if d[5:] == '12-31'}
    latest_ann = max([d for d in fina_map if d[5:] == '12-31'] or [''])
    if latest_ann and '不分配' in str(fina_map[latest_ann].get('ASSIGNDSCRPT') or ''):
        divs[latest_ann[:4]] = 0.0
    cand = sorted(y for y in (set(divs) | ann_years) if y in ann_years)
    drop = float(sn.get('b_div_drop') or 30.0)
    if len(cand) >= 2:
        cur_y = cand[-1]
        prev_c = [y for y in cand[:-1] if divs.get(y, 0.0) > 0]
        if prev_c:
            prev_y = prev_c[-1]
            cur, prev = divs.get(cur_y, 0.0), divs[prev_y]
            if prev > 0 and (cur == 0 or (prev - cur) / prev * 100 > drop):
                hits.append('%s 年度分红 %s（较 %s 年度 %.1f 亿 %s%.1f%%）' % (
                    cur_y, '取消' if cur == 0 else '%.1f 亿' % (cur / 1e8), prev_y, prev / 1e8,
                    '降' if cur > 0 else '归零，', (prev - cur) / prev * 100))
    return hits


def signal_b_parts(fina_map, cf_map, bal_map, div_map, sn, roe_ref=None):
    groups = [('g1', '经营质量', _b_group1(fina_map, cf_map, sn, roe_ref)),
              ('g2', '资产负债', _b_group2(fina_map, bal_map, sn)),
              ('g3', '分红与治理', _b_group3(fina_map, div_map, sn))]
    n = sum(1 for _k, _cn, hits in groups if hits)
    level = 'none'
    if n >= 1:
        level = 'red' if n >= int(sn.get('b_red_groups') or 3) else 'yellow'
    return {'groups': [{'key': k, 'cn': cn, 'hits': h} for k, cn, h in groups],
            'n_groups': n, 'level': level,
            'detail': '命中 %d 组：%s' % (n, '；'.join(
                '%s：%s' % (cn, '、'.join(h)) for _k, cn, h in groups if h)) if n else '三组均未命中'}


def signal_b(code, sn, frames=None, roe_ref=None):
    """frames: {'fina','balance','cashflow','dividend'} 可选注入（回测/测试用）"""
    try:
        f = frames or {}
        fina = f.get('fina') if f.get('fina') is not None else selldata.get_fina(code)
        cf = f.get('cashflow') if f.get('cashflow') is not None else selldata.get_cashflow(code)
        bal = f.get('balance') if f.get('balance') is not None else selldata.get_balance(code)
        dv = f.get('dividend') if f.get('dividend') is not None else selldata.get_dividend(code)
    except Exception as e:
        return {'level': 'none', 'status': '数据失败', 'detail': '财务数据拉取失败：%s' % str(e)[:60]}
    fina_map = _rows_by_date(fina, 'REPORTDATE')
    bal_map = _rows_by_date(bal, 'REPORT_DATE')
    cf_map = _rows_by_date(cf, 'REPORT_DATE')
    div_map = _rows_by_date(dv, 'EX_DIVIDEND_DATE')
    if not fina_map:
        return {'level': 'none', 'status': '缺数据', 'detail': '没有财务摘要记录'}
    out = signal_b_parts(fina_map, cf_map, bal_map, div_map, sn, roe_ref)
    out['status'] = 'ok'
    out['latest_report'] = max(fina_map)
    return out


def fina_map_to_df(fm):
    """{date: row} -> DataFrame（只给 get_industry 兜底用）"""
    if not fm:
        return None
    return pd.DataFrame([fm[d] for d in sorted(fm)])


# ---------------- 信号 C：行业与宏观 ----------------

def is_neg_warn(predict_type):
    t = str(predict_type or '')
    if not t:
        return False
    return '扭亏' not in t and any(w in t for w in NEG_WARN_WORDS)


def signal_c(code, index_spec, industry, sn, warn_rows, idf=None):
    """warn_rows: [{'code','name','notice_date','predict_type'}] 同行（不含本票）预警行"""
    dd_thr = float(sn.get('c_dd_pct') or 20.0)
    days = int(sn.get('c_dd_days') or 250)
    peers_min = int(sn.get('c_warn_peers') or 2)
    try:
        start = (datetime.date.today() - datetime.timedelta(days=int(days * 1.7) + 40)
                 ).strftime('%Y-%m-%d')
        df = idf if idf is not None else selldata.get_index_kline(index_spec, start)
    except Exception as e:
        return {'level': 'none', 'status': '数据失败', 'detail': '行业指数拉取失败：%s' % str(e)[:60]}
    if df is None or df.empty or 'close' not in df.columns:
        return {'level': 'none', 'status': '缺数据', 'detail': '没有行业指数日线'}
    closes = [_num(x) for x in df['close']]
    closes = [c for c in closes if c is not None]
    if len(closes) < 30:
        return {'level': 'none', 'status': '缺数据', 'detail': '行业指数日线不足'}
    win = closes[-days:]
    hi, cur = max(win), win[-1]
    dd = (hi - cur) / hi * 100 if hi else 0.0
    within = int(sn.get('c_warn_days') or 30)
    since = (datetime.date.today() - datetime.timedelta(days=within)).isoformat()
    peers = sorted({(w.get('code'), w.get('name')) for w in (warn_rows or [])
                    if str(w.get('notice_date') or '') >= since and is_neg_warn(w.get('predict_type'))})
    dd_ok, peers_ok = dd >= dd_thr, len(peers) >= peers_min
    out = {'level': 'yellow' if (dd_ok and peers_ok) else 'none', 'status': 'ok',
           'dd': round(dd, 1), 'dd_ok': dd_ok, 'dd_thr': dd_thr,
           'n_peers': len(peers), 'peers': ['%s(%s)' % (n, c) for c, n in peers],
           'peers_ok': peers_ok, 'index': selldata.resolve_index_secid(index_spec),
           'industry': industry or '-'}
    out['detail'] = '指数回撤 %.1f%%（阈值 %.0f%%，%s）；同行预警 %d 家（阈值 %d 家，%d 日内）' % (
        dd, dd_thr, '成立' if dd_ok else '不成立', len(peers), peers_min, within)
    if peers:
        out['detail'] += '：%s' % '、'.join(out['peers'])
    return out


# ---------------- 信号 D：社保/保险卖出 ----------------

def holder_kind(name):
    n = str(name or '')
    if '社保' in n or '社会保障' in n:
        return '社保'
    if '保险' in n or '人寿' in n or '财险' in n or '养老' in n:
        return '保险'
    return ''


def signal_d(code, sn, hdf=None):
    if not sn.get('d_on', True):
        return {'level': 'none', 'status': 'off', 'detail': '信号 D 已在因子配置中关闭'}
    try:
        df = hdf if hdf is not None else selldata.get_holders(code)
    except Exception as e:
        return {'level': 'none', 'status': '数据失败', 'detail': '股东数据拉取失败：%s' % str(e)[:60]}
    if df is None or df.empty:
        return {'level': 'none', 'status': '缺数据', 'detail': '没有十大流通股东记录'}
    ends = sorted({d for d in df['END_DATE'] if d})
    if len(ends) < 2:
        return {'level': 'none', 'status': '缺数据', 'detail': '股东报告期不足两期，无法比较'}
    cur_end, prev_end = ends[-1], ends[-2]

    def inst_map(end):
        agg = {}
        for rec in df[df['END_DATE'] == end].to_dict('records'):
            kind = holder_kind(rec.get('HOLDER_NAME'))
            if kind:
                name = str(rec.get('HOLDER_NAME'))
                a = agg.setdefault(name, {'kind': kind, 'num': 0.0, 'ratio': 0.0})
                a['num'] += _num(rec.get('HOLD_NUM')) or 0.0
                a['ratio'] += _num(rec.get('HOLD_RATIO')) or 0.0
        return agg

    prev, cur = inst_map(prev_end), inst_map(cur_end)
    hits = []
    for name, p in prev.items():
        c = cur.get(name)
        if c is None:
            hits.append({'name': name, 'kind': p['kind'], 'prev': p['num'], 'cur': None,
                         'chg_pct': None, 'note': '退出十大流通股东（可能被减至十大数据外）'})
        elif c['num'] < p['num']:
            chg = (c['num'] - p['num']) / p['num'] * 100 if p['num'] else None
            hits.append({'name': name, 'kind': p['kind'], 'prev': p['num'], 'cur': c['num'],
                         'chg_pct': round(chg, 1) if chg is not None else None,
                         'note': '减持 %.1f%%' % -chg if chg else '持股变化'})
    out = {'level': 'yellow' if hits else 'none', 'status': 'ok',
           'cur_end': cur_end, 'prev_end': prev_end, 'hits': hits,
           'n_inst': len(cur)}
    out['detail'] = ('%s 较 %s：%s' % (cur_end, prev_end,
                     '；'.join('%s（%s）%s' % (h['name'], h['kind'], h['note']) for h in hits)
                     )) if hits else ('两期共 %d 家社保/保险机构持股均未减少' % len(cur))
    return out


# ---------------- 每日扫描汇总 ----------------

def _latest_quote(code):
    """最近收盘价与当日涨跌（复用日线缓存，不强制刷新）"""
    try:
        df = mkt_data.get_kline(code, (datetime.date.today() - datetime.timedelta(days=40)
                                       ).strftime('%Y-%m-%d'), refresh=False)
        if len(df) == 0:
            return {}
        last, prev = df.iloc[-1], (df.iloc[-2] if len(df) > 1 else df.iloc[-1])
        return {'date': str(last['date']), 'close': _num(last['close']),
                'chg_pct': _num(last['pct_chg']) if 'pct_chg' in df.columns
                else ((None if not _num(prev['close']) else
                       (_num(last['close']) / _num(prev['close']) - 1) * 100))}
    except Exception:
        return {}


def roe_ref_map(stocks, fina_maps):
    """行业 ROE 参照：同一清单内同行业最新年报 ROE 平均（清单样本有限，页面注明口径）"""
    ind_roe = {}
    for st in stocks:
        fm = fina_maps.get(st['code']) or {}
        ind = str(((fm.get(max(fm)) if fm else {}) or {}).get('BOARD_NAME') or '')
        ann = [d for d in sorted(fm) if d[5:] == '12-31'
               and _num(fm[d].get('WEIGHTAVG_ROE')) is not None]
        if ind and ann:
            ind_roe.setdefault(ind, []).append(_num(fm[ann[-1]].get('WEIGHTAVG_ROE')))
    return {k: sum(v) / len(v) for k, v in ind_roe.items() if len(v) >= 2}


def _board_of(fm):
    """从财报映射里取最新一行的行业名（信号 C 的“同行”按它匹配）"""
    if not fm:
        return ''
    return str((fm.get(max(fm)) or {}).get('BOARD_NAME') or '')


def scan_sell(cfg=None, refresh=False):
    """逐只跑四路信号，出一行汇总；清单小（十几只）时一次扫描约几十个请求，当日缓存后秒开"""
    cfg = cfg or config.load_config()
    sn = cfg.get('sell_notify') or {}
    stocks = [s for s in config.load_stocks() if s.get('enabled', True)]
    if not sn.get('enabled', True):
        return {'disabled': True, 'rows': [], 'cards': [], 'alerts': [], 'date': '',
                'n_yellow': 0, 'n_red': 0, 'n_monitor': len(stocks),
                'n_notrig': 0, 'n_pending': 0, 'cfg': cfg, 'stocks': stocks}
    err, fina_maps, frames_of = {}, {}, {}
    for st in stocks:
        code = st['code']
        try:
            fdf = selldata.get_fina(code, refresh)
            cdf = selldata.get_cashflow(code, refresh)
            bdf = selldata.get_balance(code, refresh)
            vdf = selldata.get_dividend(code, refresh)
        except Exception as e:
            err[code] = str(e)[:60]
            fdf = cdf = bdf = vdf = None
        fina_maps[code] = _rows_by_date(fdf, 'REPORTDATE')
        frames_of[code] = {'fina': fdf, 'cashflow': cdf, 'balance': bdf, 'dividend': vdf}
    ref = roe_ref_map(stocks, fina_maps)
    industry_of = {}
    for st in stocks:
        industry_of[st['code']] = _board_of(fina_maps.get(st['code'])) \
            or selldata.get_industry(st['code'], fina=fina_map_to_df(fina_maps.get(st['code'])))
    # 同行预警：清单内每只票近 c_warn_days+5 天的预告（多给 5 天余量防缓存错位）
    warn_days = int(sn.get('c_warn_days') or 30) + 5
    forecasts = {}
    for st in stocks:
        try:
            fdf = selldata.get_forecast(st['code'], days=warn_days, refresh=refresh)
            forecasts[st['code']] = [{'code': st['code'], 'name': st.get('name') or st['code'],
                                      'notice_date': str(r.get('NOTICE_DATE') or '')[:10],
                                      'predict_type': r.get('PREDICT_TYPE')}
                                     for r in (fdf.to_dict('records') if fdf is not None and len(fdf) else [])]
        except Exception:
            forecasts[st['code']] = []
    index_cache = {}
    rows = []
    for st in stocks:
        code = st['code']
        industry = industry_of.get(code) or '-'
        idx_spec = st.get('index') or ''
        if idx_spec not in index_cache:
            try:
                start = (datetime.date.today()
                         - datetime.timedelta(days=int(sn.get('c_dd_days') or 250) * 2)
                         ).strftime('%Y-%m-%d')
                index_cache[idx_spec] = selldata.get_index_kline(idx_spec, start, refresh)
            except Exception:
                index_cache[idx_spec] = None
        peers = [w for other in stocks
                 if other['code'] != code and industry != '-'
                 and industry_of.get(other['code']) == industry
                 for w in forecasts.get(other['code'], [])]
        row = {'code': code, 'name': st.get('name') or code, 'type': st.get('type', 'value'),
               'type_cn': TYPE_CN.get(st.get('type'), '非金融'), 'industry': industry,
               'quote': _latest_quote(code), 'error': err.get(code, '')}
        row['a'] = signal_a(code, st.get('type', 'value'), sn)
        # 股息率 TTM（与类型无关，都展示）：用已缓存的分红表 + 现价算，不额外触网
        row['a']['dy'] = dividend_yield_ttm((frames_of.get(code) or {}).get('dividend'),
                                            (row['quote'] or {}).get('close'))
        fm = fina_maps.get(code) or {}
        if fm:
            b = signal_b_parts(fm,
                               _rows_by_date(frames_of[code]['cashflow'], 'REPORT_DATE'),
                               _rows_by_date(frames_of[code]['balance'], 'REPORT_DATE'),
                               _rows_by_date(frames_of[code]['dividend'], 'EX_DIVIDEND_DATE'),
                               sn, ref.get(industry))
            b['status'] = 'ok'
            b['latest_report'] = max(fm)
            row['b'] = b
        else:
            row['b'] = {'level': 'none', 'status': err.get(code) and '数据失败' or '缺数据',
                        'detail': err.get(code) or '没有财务摘要记录', 'n_groups': 0, 'groups': []}
        row['c'] = signal_c(code, idx_spec, industry, sn, peers, idf=index_cache.get(idx_spec))
        row['d'] = signal_d(code, sn)
        row['level'] = _max_level([row['a']['level'], row['b']['level'],
                                   row['c']['level'], row['d']['level']])
        row['reasons'] = [row[k].get('detail') for k in 'abcd'
                          if row[k].get('level') != 'none' and row[k].get('detail')]
        sigs = [row['a'], row['b'], row['c'], row['d']]
        # 四路都拿到数据（或主动关闭）才算“已核验”，否则记“待核验”
        row['verified'] = all(s.get('status') in ('ok', 'off') for s in sigs)
        row['state'] = ('red' if row['level'] == 'red' else
                        'yellow' if row['level'] == 'yellow' else
                        'pending' if not row['verified'] else 'none')
        rows.append(row)
    alerts = [r for r in rows if r['level'] != 'none']
    alerts.sort(key=lambda r: (-LEVELS[r['level']], r['code']))
    # 卡片展示顺序：红 > 黄 > 待核验 > 未触发，同级按代码
    _state_rank = {'red': 0, 'yellow': 1, 'pending': 2, 'none': 3}
    cards = sorted(rows, key=lambda r: (_state_rank.get(r['state'], 9), r['code']))
    dates = [r['quote'].get('date') for r in rows if r['quote'].get('date')]
    return {'disabled': False, 'rows': rows, 'cards': cards, 'alerts': alerts,
            'n_yellow': sum(1 for r in alerts if r['level'] == 'yellow'),
            'n_red': sum(1 for r in alerts if r['level'] == 'red'),
            'n_monitor': len(rows),
            'n_notrig': sum(1 for r in rows if r['state'] == 'none'),
            'n_pending': sum(1 for r in rows if r['state'] == 'pending'),
            'date': max(dates) if dates else '', 'cfg': cfg, 'stocks': stocks}


# ---------------- 历史回测 ----------------

def _fwd_returns(kline, date_str, hs=(5, 10, 20)):
    """事件日之后第 h 个交易日收盘相对事件日（或其后首个交易日）收盘的涨跌 %"""
    if kline is None or not len(kline) or not date_str:
        return {}
    dates = list(kline['date'])
    close = [_num(x) for x in kline['close']]
    i = bisect.bisect_left(dates, date_str)
    if i >= len(dates):
        return {}
    base = close[i]
    out = {}
    for h in hs:
        j = i + h
        out['ret%d' % h] = (round((close[j] / base - 1) * 100, 2)
                            if j < len(dates) and base and close[j] is not None else None)
    return out


def _a_events(code, stype, sn, vdf, start):
    col, metric_cn, years_key = VAL_SPEC.get(stype) or VAL_SPEC['value']
    years = int(sn.get(years_key) or 5)
    if vdf is None or vdf.empty or col not in vdf.columns:
        return []
    _conv(vdf, [col])
    hist = [(d, _num(v)) for d, v in zip(vdf['TRADE_DATE'].astype(str).str[:10], vdf[col]) if _num(v)]
    dates = [h[0] for h in hist]
    events, prev_flag = [], False
    for i, (d, v) in enumerate(hist):
        if d < start or i < 60:
            continue
        cutoff = _years_ago(d, years)
        j = bisect.bisect_left(dates, cutoff)
        win = [x for _k, x in hist[j:i + 1] if x is not None]
        pct = percentile_of(win, v)
        if pct is None:
            continue
        flag = pct >= float(sn.get('a_yellow') or 70)
        if flag and not prev_flag:
            events.append({'date': d, 'code': code, 'sig': 'A',
                           'level': 'red' if pct >= float(sn.get('a_red') or 80) else 'yellow',
                           'detail': '%s %.3f 位于近 %d 年 %.1f%% 分位' % (metric_cn, v, years, pct)})
        prev_flag = flag
    return events


def _b_events(code, sn, fina_map, cf_map, bal_map, div_map, start, ref_roe):
    nds = sorted({str((fina_map[d] or {}).get('NOTICE_DATE') or '')[:10] or d
                  for d in fina_map if fina_map[d]})
    events, last_sig = [], None
    for nd in nds:
        if nd < start:
            continue
        fm = {d: r for d, r in fina_map.items() if d <= nd}
        if not fm:
            continue
        out = signal_b_parts(fm,
                             {d: r for d, r in cf_map.items() if d <= nd},
                             {d: r for d, r in bal_map.items() if d <= nd},
                             {d: r for d, r in div_map.items() if d <= nd},
                             sn, ref_roe)
        if out['n_groups'] >= 1:
            sig = tuple(sorted(h for g in out['groups'] for h in g['hits']))
            if sig != last_sig:                    # 与上一期命中内容完全相同则不重复计事件
                events.append({'date': nd, 'code': code, 'sig': 'B', 'level': out['level'],
                               'detail': out['detail']})
                last_sig = sig
    return events


def _c_events(code, sn, idf, warn_by_notice, start):
    """按指数日线逐日：回撤成立 × 回看窗口内同行预警家数达标 → 进入预警区间记事件"""
    if idf is None or idf.empty:
        return []
    closes = [_num(x) for x in idf['close']]
    dates = list(idf['date'])
    days = int(sn.get('c_dd_days') or 250)
    dd_thr = float(sn.get('c_dd_pct') or 20.0)
    peers_min = int(sn.get('c_warn_peers') or 2)
    within = int(sn.get('c_warn_days') or 30)
    events, prev_flag = [], False
    for i, d in enumerate(dates):
        if d < start or i < 30:
            continue
        win = [c for c in closes[max(0, i - days):i + 1] if c is not None]
        if not win:
            continue
        dd = (max(win) - win[-1]) / max(win) * 100 if max(win) else 0
        since = (_date_obj(d) - datetime.timedelta(days=within)).isoformat()
        n_peers = len({w[0] for w in warn_by_notice if w[2] >= since and w[1]})
        flag = dd >= dd_thr and n_peers >= peers_min
        if flag and not prev_flag:
            events.append({'date': d, 'code': code, 'sig': 'C', 'level': 'yellow',
                           'detail': '指数回撤 %.1f%% + 同行预警 %d 家' % (dd, n_peers)})
        prev_flag = flag
    return events


def _d_events(code, sn, hdf, start):
    if hdf is None or hdf.empty:
        return []
    ends = sorted({d for d in hdf['END_DATE'] if d})
    events = []
    for prev_end, cur_end in zip(ends, ends[1:]):
        if cur_end < start:
            continue
        pair = hdf[(hdf['END_DATE'] == prev_end) | (hdf['END_DATE'] == cur_end)]
        r = signal_d(code, sn, hdf=pair)
        if r.get('hits'):
            events.append({'date': cur_end, 'code': code, 'sig': 'D', 'level': 'yellow',
                           'detail': r['detail']})
    return events


def _date_obj(d):
    return datetime.date.fromisoformat(str(d)[:10])


def backtest(cfg=None, codes=None, years=None):
    """历史回测：只用本地缓存 + 一次拉取，事件按“进入预警”的时点计，附事件后 5/10/20 个交易日涨跌"""
    cfg = cfg or config.load_config()
    sn = cfg.get('sell_notify') or {}
    years = int(years or sn.get('bt_years') or 3)
    start = (datetime.date.today() - datetime.timedelta(days=years * 365 + 5)).strftime('%Y-%m-%d')
    stocks = [s for s in config.load_stocks()
              if s.get('enabled', True) and (not codes or s['code'] in codes)]
    klines = {}
    for st in stocks:
        try:
            klines[st['code']] = mkt_data.get_kline(st['code'], start, refresh=False)
        except Exception:
            klines[st['code']] = None
    fina_maps = {}
    ref = {}
    for st in stocks:
        try:
            fina_maps[st['code']] = _rows_by_date(selldata.get_fina(st['code']), 'REPORTDATE')
        except Exception:
            fina_maps[st['code']] = {}
    ref = roe_ref_map(stocks, fina_maps)
    # 同行预警历史（直接拉全区间，回测是显式重操作，可接受网络往返）
    forecasts = {}
    for st in stocks:
        try:
            fdf = selldata.fetch_forecast(st['code'], start)
            forecasts[st['code']] = fdf
        except Exception:
            forecasts[st['code']] = None
    industry_of = {}
    for st in stocks:
        industry_of[st['code']] = _board_of(fina_maps.get(st['code']) or {})
    index_cache = {}
    events = []
    for st in stocks:
        code, sn_st = st['code'], st
        try:
            vdf = selldata.get_value(code, years=int(selldata_val_years(sn, sn_st.get('type'))) + years + 1)
        except Exception:
            vdf = None
        events += _a_events(code, sn_st.get('type', 'value'), sn, vdf, start)
        cf_map = _rows_by_date(_safe(selldata.get_cashflow, code), 'REPORT_DATE')
        bal_map = _rows_by_date(_safe(selldata.get_balance, code), 'REPORT_DATE')
        div_map = _rows_by_date(_safe(selldata.get_dividend, code), 'EX_DIVIDEND_DATE')
        events += _b_events(code, sn, fina_maps.get(code) or {}, cf_map, bal_map, div_map,
                            start, ref.get(industry_of[code]))
        idx = sn_st.get('index') or ''
        if idx not in index_cache:
            try:
                index_cache[idx] = selldata.get_index_kline(
                    idx, (datetime.date.fromisoformat(start)
                          - datetime.timedelta(days=int(sn.get('c_dd_days') or 250) * 2))
                    .strftime('%Y-%m-%d'))
            except Exception:
                index_cache[idx] = None
        warn = []
        for other in stocks:
            oc = other['code']
            if oc == code or industry_of.get(oc) != industry_of.get(code):
                continue
            fdf = forecasts.get(oc)
            if fdf is None or not len(fdf):
                continue
            for rec in fdf.to_dict('records'):
                warn.append((oc, is_neg_warn(rec.get('PREDICT_TYPE')),
                             str(rec.get('NOTICE_DATE') or '')[:10]))
        events += _c_events(code, sn, index_cache.get(idx), warn, start)
        events += _d_events(code, sn, _safe(selldata.get_holders, code), start)
    for x in events:
        x.update(_fwd_returns(klines.get(x['code']), x['date']))
        x['name'] = next((s.get('name') or s['code'] for s in stocks if s['code'] == x['code']), x['code'])
        x['sig_cn'] = SIG_CN.get(x['sig'], x['sig'])
    events.sort(key=lambda e: (e['code'], e['date']))
    summary = []
    for sig in 'ABCD':
        xs = [e for e in events if e['sig'] == sig]
        r20 = [e['ret20'] for e in xs if e.get('ret20') is not None]
        summary.append({'sig': sig, 'sig_cn': SIG_CN[sig], 'n': len(xs),
                        'avg5': _avg(e.get('ret5') for e in xs),
                        'avg20': _avg(r20),
                        'drop_pct': round(sum(1 for v in r20 if v < 0) * 100.0 / len(r20), 1) if r20 else None})
    return {'events': events, 'summary': summary, 'start': start, 'years': years,
            'stocks': stocks, 'n_all': len(events),
            'avg20_all': _avg(e.get('ret20') for e in events if e.get('ret20') is not None)}


def selldata_val_years(sn, stype):
    _col, _m, years_key = VAL_SPEC.get(stype) or VAL_SPEC['value']
    return int(sn.get(years_key) or 5)


def _safe(fn, code):
    try:
        return fn(code)
    except Exception:
        return None


def _avg(vals):
    vs = [v for v in vals if v is not None]
    return round(sum(vs) / len(vs), 2) if vs else None


# ---------------- 发送日志（网页展示飞书通知用） ----------------

def append_log(entry):
    try:
        with open(LOG_PATH, 'a', encoding='utf-8') as f:
            f.write(json.dumps(entry, ensure_ascii=False) + '\n')
    except Exception:
        pass
    return entry


def read_log(n=5):
    if not os.path.exists(LOG_PATH):
        return []
    try:
        with open(LOG_PATH, encoding='utf-8') as f:
            lines = [x for x in f.read().splitlines() if x.strip()]
    except Exception:
        return []
    out = []
    for x in lines[-n:][::-1]:
        try:
            out.append(json.loads(x))
        except Exception:
            continue
    return out
