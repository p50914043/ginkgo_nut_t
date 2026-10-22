# -*- coding: utf-8 -*-
"""提示层：只在“本页判定的震荡段内”给做T纪律提示，外加多头回踩的介入提示

纪律来自需求（7 只大盘股、2.1 年实证）：
    震荡段：%B ≤ 0.10 买入 / %B ≥ 0.90 卖出 / −5% 硬止损 / 最长持 15 日
    震荡 → 空头：止损回避（区间下沿那套买入逻辑当场失效）
    多头段：想介入要“回踩 MA20 + 缩量止跌”，且必须提示最高风险
第一纪律：**趋势段停手** —— 生效三态不是震荡时，一律不给区间买卖提示。

这里只出“提示”，不记账、不算收益与胜率、不给仓位与金额、不对接任何交易接口；
回放持仓状态只是为了知道“今天该提示买入、卖出、还是止损”，不产出历史业绩。
"""
import numpy as np

from . import regime

DEF = {'enabled': True,
       'buy_pctb': 0.10,       # 区间下沿：%B 到这儿给买入提示
       'sell_pctb': 0.90,      # 区间上沿：持仓中到这儿给卖出提示
       'stop_pct': 5.0,        # 硬止损：自提示买入那天的收盘算，跌这么多就走
       'max_hold': 15,         # 最长持有交易日数，超了就提示离场
       'pb_touch': 1.5,        # 多头回踩容差%：最低价距 MA20 在这么近以内算“踩到”
       'vol_shrink': 0.8}      # 缩量：当日量比(20) 低于这个倍数才算缩
KEYS = tuple(DEF)

# 提示动作：mark 直接给页面与播报用
ACTION_CN = {
    'buy': '买入提示｜%B 已到区间下沿',
    'sell': '卖出提示｜%B 已到区间上沿',
    'stop': '止损提示｜已到硬止损位',
    'expire': '超期提示｜最长持有日数已到',
    'hold': '持仓提示｜区间内做T进行中',
    'wait': '等待提示｜区间内未到下沿',
    'avoid': '止损回避｜震荡转空头，下沿买入逻辑失效',
    'watch': '介入提示｜回踩 MA20 + 缩量止跌',
    'bull_wait': '介入观察｜回踩 / 缩量 / 止跌未凑满',
    'off': '趋势段停手｜本段不是震荡，不给区间提示',
}
MARK = {'buy': '🟢', 'sell': '🔺', 'stop': '🛑', 'expire': '⌛', 'hold': '🔵',
        'wait': '⚪', 'avoid': '🛑', 'watch': '🟠', 'bull_wait': '⚪', 'off': '⛔'}


def tcfg(cfg):
    t = dict(DEF)
    t.update((cfg or {}).get('t_trade') or {})
    t['enabled'] = bool(t['enabled'])
    for k in ('buy_pctb', 'sell_pctb', 'stop_pct', 'pb_touch', 'vol_shrink'):
        t[k] = float(t[k])
    t['max_hold'] = max(1, int(t['max_hold']))
    return t


def _col(df, name, n, default=np.nan):
    return df[name].values if name in getattr(df, 'columns', []) else np.full(n, default)


def _num(v, nd=3):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return None if v != v else round(v, nd)


def _base(kind, r, **over):
    """提示字典的公共外壳：参数与三态背景先填好，调用方只关心 action 与几个价位"""
    out = {'action': 'off', 'mark': MARK['off'], 'action_cn': ACTION_CN['off'],
           'kind': kind, 'level_cn': regime.LABEL_CN.get(kind, kind),
           'buy_pctb': r['buy_pctb'], 'sell_pctb': r['sell_pctb'],
           'stop_pct': r['stop_pct'], 'max_hold': r['max_hold'],
           'reason': ACTION_CN['off'], 'risk': '', 'need': [],
           'entry_date': None, 'entry_close': None, 'stop_price': None,
           'hold_days': 0, 'left_days': None, 'pctb': None, 'ma20': None,
           'vol_ratio': None}
    out.update(over)
    return out


def range_tips(df, snap, t=None):
    """在当前震荡段内回放一遍纪律，只回答“今天该提示什么”

    段边界用生效三态的波段（含毛刺并入后的 i0/i1），所以“趋势段停手”是结构性的：
    不是震荡段就根本不会走到买入分支。
    """
    r = t or tcfg({})
    kind = (snap or {}).get('kind')
    if not snap or kind == regime.UNKNOWN:
        return None
    if kind != regime.RANGE:
        return _base(kind, r, level_cn=snap.get('level_cn') or kind)
    seg = snap.get('segment') or {}
    i0, i1 = seg.get('i0'), seg.get('i1')
    n = len(df)
    if i0 is None or i1 is None:
        return _base(kind, r, reason='震荡段还没成形，暂不提示')
    close = _col(df, 'close', n)
    low = _col(df, 'low', close)
    pctb = _col(df, 'pctb', n)
    dates = list(df['date'])
    base = _base(kind, r, pctb=_num(pctb[i1], 2), segment=seg.get('start'),
                 level_cn=snap.get('level_cn') or regime.LABEL_CN.get(kind, kind))
    hold = None                      # {'i','date','entry','stop'}
    out = dict(base, action='wait', mark=MARK['wait'], action_cn=ACTION_CN['wait'],
               reason='段内还没触发过下沿')

    def close_out(i, action, why, **kw):
        """今天正好走到这条纪律上，就直接把提示开出来"""
        return dict(base, action=action, mark=MARK[action], action_cn=ACTION_CN[action],
                    reason=why, **kw)

    for i in range(int(i0), int(i1) + 1):
        p, c, l = pctb[i], close[i], low[i]
        if hold is None:
            if p == p and p <= r['buy_pctb']:
                hold = {'i': i, 'date': dates[i], 'entry': float(c),
                        'stop': float(c) * (1.0 - r['stop_pct'] / 100.0)}
                if i == i1:          # 今天刚触发下沿
                    return close_out(i, 'buy', ACTION_CN['buy'],
                                     entry_date=hold['date'],
                                     entry_close=_num(hold['entry']),
                                     stop_price=_num(hold['stop']),
                                     hold_days=1, left_days=r['max_hold'] - 1)
            elif i == i1:
                return close_out(i, 'wait',
                                 '%%B %s 未到下沿 %.2f，先等' % (
                                     ('%.2f' % p) if p == p else '缺值', r['buy_pctb']))
            continue
        # 持仓中：止损 > 上沿卖出 > 超期，谁先出现听谁的
        ent, ecl, stp = hold['date'], hold['entry'], hold['stop']
        if l == l and l <= stp:
            hold = None
            if i == i1:
                return close_out(i, 'stop',
                                 '最低 %.2f 触及硬止损位 %.2f（%s 提示买入价 %.2f，−%.1f%%）'
                                 % (l, stp, ent, ecl, r['stop_pct']),
                                 entry_date=ent, entry_close=_num(ecl), stop_price=_num(stp))
            continue
        if p == p and p >= r['sell_pctb']:
            hold = None
            if i == i1:
                return close_out(i, 'sell',
                                 '%%B %.2f 已到上沿 %.2f，本段 %s 那一笔到位' % (
                                     p, r['sell_pctb'], ent),
                                 entry_date=ent, entry_close=_num(ecl), stop_price=_num(stp))
            continue
        if i - hold['i'] >= r['max_hold']:
            hold = None
            if i == i1:
                return close_out(i, 'expire',
                                 '自 %s 已持 %d 日，超最长 %d 日纪律' % (ent, r['max_hold'],
                                                              r['max_hold']),
                                 entry_date=ent, entry_close=_num(ecl), stop_price=_num(stp))
            continue
        if i == i1:
            days = i - hold['i'] + 1
            return close_out(i, 'hold',
                             '本段内 %s 触发下沿，第 %d/%d 日' % (ent, days, r['max_hold']),
                             entry_date=ent, entry_close=_num(ecl), stop_price=_num(stp),
                             hold_days=days, left_days=r['max_hold'] - days)
    return out


def bull_watch(df, snap, t=None):
    """多头段的介入条件：回踩 MA20 + 缩量 + 止跌；三条缺一不提示，提示就附最高风险"""
    r = t or tcfg({})
    n = len(df)
    kind = (snap or {}).get('kind')
    if not snap or kind != regime.BULL or n < 2:
        return None
    close = _col(df, 'close', n)
    low = _col(df, 'low', close)
    ma20 = _col(df, 'ma20', n)
    vol_ratio = _col(df, 'vol_ratio20', n)
    i = n - 1
    if ma20[i] != ma20[i] or close[i] != close[i]:
        return _base(kind, r, reason='MA20 或收盘缺值，暂不判回踩')
    prev = close[i - 1] if close[i - 1] == close[i - 1] else None
    touched = low[i] <= ma20[i] * (1.0 + r['pb_touch'] / 100.0)
    shrink = vol_ratio[i] == vol_ratio[i] and vol_ratio[i] < r['vol_shrink']
    steadied = prev is not None and close[i] >= prev
    base = _base(kind, r, ma20=_num(ma20[i]), vol_ratio=_num(vol_ratio[i], 2),
                 level_cn=snap.get('level_cn') or regime.LABEL_CN.get(kind, kind),
                 pctb=_num(_col(df, 'pctb', n)[i], 2))
    need = []
    if not touched:
        need.append('回踩 MA20（最低 %.2f 还差 %.2f%%）' % (
            low[i], (low[i] / ma20[i] - 1.0) * 100.0))
    if not shrink:
        need.append('缩量（量比 %.2f× 未到 %.2f×）' % (
            vol_ratio[i] if vol_ratio[i] == vol_ratio[i] else -1.0, r['vol_shrink']))
    if not steadied:
        need.append('止跌（收盘未收回前一日上方）')
    if not need:
        return dict(base, action='watch', mark=MARK['watch'], action_cn=ACTION_CN['watch'],
                    reason='最低 %.2f 踩到 MA20 %.2f · 量比 %.2f× 缩量 · 收盘站上前收' % (
                        low[i], ma20[i], vol_ratio[i]),
                    risk='最高风险｜趋势段做区间介入历史有效性最差，破 MA20 当场失效')
    return dict(base, action='bull_wait', mark=MARK['bull_wait'],
                action_cn=ACTION_CN['bull_wait'],
                reason='回踩介入条件未凑满：' + '、'.join(need), need=need)


def for_stock(df, cfg, snap):
    """统一入口：震荡段给区间提示，多头段给回踩介入提示，空头段只声明“趋势段停手”"""
    r = tcfg(cfg)
    if not r['enabled'] or df is None or not len(df) or not snap:
        return None
    kind = snap.get('kind')
    if kind == regime.UNKNOWN:
        return None
    lvl = {'level_cn': snap.get('level_cn') or regime.LABEL_CN.get(kind, kind)}
    # 震荡 → 空头：区间买入逻辑失效，优先级高于区间提示本身
    if snap.get('changed') and snap.get('prev') == regime.RANGE and kind == regime.BEAR:
        return dict(_base(kind, r, action='avoid', mark=MARK['avoid'],
                          action_cn=ACTION_CN['avoid'], reason=ACTION_CN['avoid'],
                          pctb=_num(_col(df, 'pctb', len(df))[-1], 2)), **lvl)
    if kind == regime.RANGE:
        return range_tips(df, snap, r)
    if kind == regime.BULL:
        return bull_watch(df, snap, r)
    return dict(_base(kind, r, reason='空头段：趋势向下，区间提示不适用'), **lvl)
