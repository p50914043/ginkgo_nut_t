# -*- coding: utf-8 -*-
"""盘后扫描：更新数据 -> 算指标 -> 定三态（多头/空头/震荡）-> 汇总播报素材

判定层（core/regime.py，均线 + ADX）是唯一的状态来源，仪表盘、飞书播报、
K 线背景色块、历史波段统计都读同一份结果；提示层（core/tips.py）只在已定的
三态上附加“震荡段做T纪律”与“多头回踩介入”提示，不回改判定。
"""
from . import data, config, regime, tips
from .indicators import compute_factors, snapshot as ind_snapshot

OK = '正常'
FLIP_BARS = 501        # 切换分级统计的回测窗口（交易日）
FLIP_HORIZON = 10      # 切换次日收盘买入后看多少个交易日
# 今天要提醒的提示动作（其余 hold/wait/bull_wait/off 只是状态描述）
TIP_ALERT = ('buy', 'sell', 'stop', 'expire', 'avoid', 'watch')


def scan(cfg=None, stocks=None, refresh=True):
    """逐只标的出一行：现价/涨跌 + 三态 + 本波段天数 + 指标快照"""
    cfg = cfg or config.load_config()
    stocks = stocks if stocks is not None else config.load_stocks()
    results = []
    dfs = {}
    for st in stocks:
        item = {'code': st['code'], 'name': st.get('name') or data.get_name(st['code']),
                'status': '-', 'reason': '', 'date': None, 'close': None,
                'chg_pct': None, 'ind': None, 'adx': None, 'regime': None, 'tip': None}
        try:
            df = data.get_kline(st['code'], cfg['data_start'], refresh=refresh)
        except Exception as e:
            item.update(status='数据失败', reason=str(e)[:80])
            results.append(item)
            continue
        if len(df) < 60:
            item.update(status='数据不足', reason='日线不足 60 根，均线尚未就绪')
            results.append(item)
            continue
        fdf = compute_factors(df)
        dfs[st['code']] = fdf
        row = fdf.iloc[-1]
        item['date'] = row['date']
        item['ind'] = ind_snapshot(row)
        item['close'] = item['ind']['close']
        item['chg_pct'] = item['ind']['chg_pct']
        item['adx'] = item['ind']['adx']
        # 三态：所有标的都要播报（这也是图上波段色块的来历）
        item['regime'] = regime.snapshot(fdf, cfg)
        # 提示：震荡段给做T纪律，多头段给回踩介入（只读三态，不改三态）
        item['tip'] = tips.for_stock(fdf, cfg, item['regime'])
        snap = item['regime'] or {}
        if snap.get('kind') in (None, regime.UNKNOWN):
            item.update(status='均线未就绪', reason='慢线还没算出来，今日不做三态判定')
        else:
            item.update(status=OK, reason=snap.get('desc', ''))
        results.append(item)
    return {'cfg': cfg, 'stocks': stocks, 'results': results, 'dfs': dfs,
            'date': max([r['date'] for r in results if r['date']] or [''])}


def build_trend_report(scan_out):
    """趋势播报：每只标的当前三态（强/弱）、本波段第几天、是否刚切换、是否在酝酿。

    changed=今日已确认切换（按分级推提醒）：strong=震荡→趋势（强信号），
    momentum=趋势→震荡（动能变化）；pending=原始判定已转新状态但还没凑满确认天数；
    tips=今日有动作的区间做T/回踩介入提示（来自 core/tips.py，不影响三态口径）。
    """
    rows, changed, strong_changed, momentum_changed, pending = [], [], [], [], []
    tips_alert, holds = [], []
    for r in scan_out['results']:
        snap = r.get('regime')
        if not snap or snap['kind'] == regime.UNKNOWN:
            continue
        row = {'code': r['code'], 'name': r['name'], 'close': r.get('close'),
               'chg_pct': r.get('chg_pct'), 'ind': r.get('ind'), 'date': r.get('date'),
               'kind': snap['kind'], 'label_cn': snap['label_cn'],
               'desc': snap['desc'], 'days': snap['days'],
               'strong': bool(snap.get('strong')), 'level_cn': snap.get('level_cn', snap['label_cn']),
               'adx': snap.get('adx'), 'adx_min': snap.get('adx_min'),
               'segment': snap.get('segment') or {}, 'ma': snap.get('ma') or {},
               'changed': bool(snap['changed']), 'prev_cn': snap['prev_label_cn'],
               'grade': snap.get('grade'),
               'pending': snap['pending'], 'pending_cn': snap['pending_label_cn'],
               'pending_days': snap['pending_days'], 'confirm_days': snap['confirm_days'],
               'n_segments': snap.get('n_segments', 0),
               'tip': r.get('tip') or {}}
        rows.append(row)
        tip = row['tip']
        if tip.get('action') in TIP_ALERT:
            tips_alert.append(row)
        elif tip.get('action') == 'hold':
            holds.append(row)
        if row['changed']:
            changed.append(row)
            if (row['grade'] or {}).get('grade') == 'strong':
                strong_changed.append(row)
            elif (row['grade'] or {}).get('grade') == 'momentum':
                momentum_changed.append(row)
        elif snap['pending'] != regime.UNKNOWN:
            pending.append(row)
    cnt = {k: sum(1 for x in rows if x['kind'] == k)
           for k in (regime.BULL, regime.BEAR, regime.RANGE)}
    act = lambda a: sum(1 for x in tips_alert if x['tip'].get('action') == a)
    return {'rows': rows, 'changed': changed, 'strong_changed': strong_changed,
            'momentum_changed': momentum_changed, 'pending': pending,
            'tips': tips_alert, 'holds': holds,
            'n_tip_buy': act('buy'), 'n_tip_sell': act('sell'),
            'n_tip_stop': act('stop') + act('expire') + act('avoid'),
            'n_tip_watch': act('watch'), 'n_tip': len(tips_alert),
            'n_bull': cnt[regime.BULL], 'n_bear': cnt[regime.BEAR],
            'n_range': cnt[regime.RANGE], 'n_rows': len(rows),
            'n_strong_rows': sum(1 for x in rows if x['strong']),
            'date': scan_out.get('date') or '',
            'quiet': [r for r in scan_out['results'] if r['status'] != OK]}


def wave_stats(segments):
    """把波段列表压成统计：各态段数/天数/平均涨跌，页面用来回答“这轮行情是什么性格”"""
    agg = {}
    for s in segments or []:
        a = agg.setdefault(s['kind'], {'kind': s['kind'], 'label_cn': s['label_cn'],
                                       'n': 0, 'days': 0, 'strong_days': 0,
                                       'chg_sum': 0.0,
                                       'up': 0, 'worst': 0.0, 'best': 0.0})
        a['n'] += 1
        a['days'] += s['days']
        a['strong_days'] += int(s.get('n_strong') or 0)
        a['chg_sum'] += s['chg_pct']
        a['up'] += int(s['chg_pct'] > 0)
        a['worst'] = min(a['worst'], s['chg_pct'])
        a['best'] = max(a['best'], s['chg_pct'])
    out = []
    for a in agg.values():
        a['avg_chg'] = round(a['chg_sum'] / a['n'], 2) if a['n'] else 0.0
        a['avg_days'] = round(a['days'] / a['n'], 1) if a['n'] else 0.0
        a['strong_pct'] = round(a['strong_days'] / max(a['days'], 1) * 100, 1)
        out.append(a)
    return sorted(out, key=lambda x: -x['days'])


def range_regime_days(stocks, dfs, cfg, start_date, end_date, max_segs=200):
    """区间内三态天数与波段列表（行情页的“这段时间有多少天在趋势、多少天在震荡”）"""
    r = regime.rcfg(cfg)
    cnt = {regime.BULL: 0, regime.BEAR: 0, regime.RANGE: 0, regime.UNKNOWN: 0}
    per_stock, segs = [], []
    for st in stocks:
        df = dfs.get(st['code'])
        if df is None or not len(df):
            continue
        codes, _raw, flags = regime.regime_full(df, cfg)
        dates = list(df['date'])
        sel = [(d, s) for d, s in zip(dates, codes) if start_date <= d <= end_date]
        c = {k: sum(1 for _d, s in sel if s == k)
             for k in (regime.BULL, regime.BEAR, regime.RANGE, regime.UNKNOWN)}
        n_strong = sum(1 for d, f in zip(dates, flags)
                       if start_date <= d <= end_date and f)
        for k, v in c.items():
            cnt[k] += v
        total = sum(c.values())
        dwin = df[(df['date'] >= start_date) & (df['date'] <= end_date)]
        sg = regime.segments(list(dwin['date']),
                             [s for d, s in zip(dates, codes)
                              if start_date <= d <= end_date],
                             dwin['close'].values, dwin['high'].values,
                             dwin['low'].values,
                             [f for d, f in zip(dates, flags)
                              if start_date <= d <= end_date],
                             r['spike_days']) if len(dwin) else []
        per_stock.append({'code': st['code'], 'name': st.get('name', ''),
                          'bars': total, 'bull': c[regime.BULL], 'bear': c[regime.BEAR],
                          'range': c[regime.RANGE], 'unknown': c[regime.UNKNOWN],
                          'strong': n_strong,
                          'strong_pct': round(n_strong / max(total, 1) * 100, 1),
                          'trend_pct': round((c[regime.BULL] + c[regime.BEAR]) /
                                             max(total, 1) * 100, 1),
                          'range_pct': round(c[regime.RANGE] / max(total, 1) * 100, 1),
                          'n_segments': len(sg)})
        for x in sg[:max_segs]:
            segs.append({'code': st['code'], 'name': st.get('name', ''),
                         'kind': x['kind'], 'label_cn': x['label_cn'],
                         'level_cn': x.get('level_cn', x['label_cn']),
                         'desc': x['desc'], 'start': x['start'], 'end': x['end'],
                         'days': x['days'], 'chg_pct': x['chg_pct'], 'hi': x['hi'],
                         'lo': x['lo'], 'color': x['color'], 'fill': x['fill'],
                         'n_strong': x.get('n_strong', 0),
                         'current': bool(x.get('current'))})
    total = sum(cnt.values())
    n_strong_all = sum(p['strong'] for p in per_stock)
    return {'bull': cnt[regime.BULL], 'bear': cnt[regime.BEAR],
            'range': cnt[regime.RANGE], 'unknown': cnt[regime.UNKNOWN], 'total': total,
            'strong': n_strong_all,
            'strong_pct': n_strong_all / max(total, 1) * 100,
            'trend_pct': (cnt[regime.BULL] + cnt[regime.BEAR]) / max(total, 1) * 100,
            'range_pct': cnt[regime.RANGE] / max(total, 1) * 100,
            'per_stock': per_stock,
            'segments': sorted(segs, key=lambda s: (s['code'], s['start'])),
            'stats': wave_stats(segs)}


def flip_stats(stocks, dfs, cfg, bars=FLIP_BARS, horizon=FLIP_HORIZON):
    """切换分级的实测有效性（页面“判定规则与运行机制”里的数字就是它算出来的）

    口径：取生效三态的波段起点为“切换确认日”，次日收盘买入，看未来 horizon 个交易日的收盘涨跌；
    方向胜率 = 收益方向与新态是否一致（震荡→多头算涨、震荡→空头算跌，
    趋势→震荡算“看有没有回落”）。只是历史统计，不是收益承诺。
    """
    r = regime.rcfg(cfg)
    agg = {}
    n_days = 0
    for st in stocks:
        df = dfs.get(st['code'])
        if df is None or len(df) < r['ma_slow'] + horizon + 10:
            continue
        codes, _raw, flags = regime.regime_full(df, cfg)
        n0 = max(0, len(df) - bars)
        n_days += min(bars, len(df) - n0)
        win = df.iloc[n0:]
        segs = regime.segments(list(win['date']), list(codes[n0:]), win['close'].values,
                               win['high'].values, win['low'].values, flags[n0:],
                               r['spike_days'])
        close = df['close'].values
        for k in range(1, len(segs)):
            sg = segs[k]
            i = n0 + sg['i0']
            entry, out = i + 1, i + 1 + horizon
            if out >= len(close) or entry >= len(close):
                continue
            ret = (close[out] / close[entry] - 1.0) * 100
            key = (segs[k - 1]['kind'], sg['kind'])
            a = agg.setdefault(key, {'n': 0, 'sum': 0.0, 'hit': 0})
            a['n'] += 1
            a['sum'] += ret
            want_up = sg['kind'] == regime.BULL
            # 进震荡按“趋势动能消失”计，回落才算命中
            a['hit'] += int(ret > 0 if want_up else ret < 0)
    rows = []
    for (prev_k, cur_k), a in agg.items():
        g = regime.flip_grade(prev_k, cur_k)
        rows.append({'prev': prev_k, 'cur': cur_k,
                     'prev_cn': regime.LABEL_CN[prev_k], 'cur_cn': regime.LABEL_CN[cur_k],
                     'grade': g['grade'], 'mark': g['mark'], 'ref_ret': g['ret'],
                     'ref_win': g['win'],
                     'n': a['n'], 'avg': round(a['sum'] / a['n'], 2),
                     'win_pct': round(a['hit'] / a['n'] * 100, 0)})
    order = {'strong': 0, 'momentum': 1, 'other': 2}
    rows.sort(key=lambda x: (order.get(x['grade'], 3), -x['n']))
    return {'bars': bars, 'horizon': horizon, 'rows': rows,
            'n_flips': sum(x['n'] for x in rows), 'n_days': n_days}


def latest_lines(scan_out):
    """仪表盘的极简文字版（页面顶部一行流，也供 dry-run 调试）"""
    out = []
    for r in scan_out['results']:
        snap = r.get('regime') or {}
        out.append('%s %s %.2f %s ｜ %s ｜ 本波段第 %s 日' % (
            r['name'], r['code'], r.get('close') or 0.0,
            ('%+.2f%%' % r['chg_pct']) if r.get('chg_pct') is not None else '-',
            snap.get('level_cn') or snap.get('label_cn', '—'), snap.get('days', '-')))
    return out
