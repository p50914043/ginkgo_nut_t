# -*- coding: utf-8 -*-
"""提示层测试：震荡段做T纪律 + 震荡→空头止损回避 + 多头回踩介入（core/tips.py）

需求 v3.2 的纪律原文：%B≤0.10 买 / ≥0.90 卖 / −5% 硬止损 / 最长 15 日；
震荡→空头 = 止损回避；多头回踩 MA20 + 缩量止跌才提示介入，且必须带最高风险。
这里全部手工搭行情（不拉真实K线），逐条验证状态机与“趋势段停手”这条结构约束。
"""
import sys
sys.path.insert(0, 'libs')
sys.path.insert(0, '.')

import numpy as np
import pandas as pd

from core import regime, tips
from core.config import DEFAULT_CONFIG

T = tips.tcfg(DEFAULT_CONFIG)          # 默认纪律：0.10 / 0.90 / 5% / 15 日 / 1.5% / 0.8×


def mk(closes=None, lows=None, pctb=None, ma20=None, volr=None, dates=None):
    """最小行情表：只带提示层真正读的那几列"""
    closes = list(closes if closes is not None else [10.0] * len(pctb or []))
    n = len(closes)
    return pd.DataFrame({
        'date': list(dates) if dates is not None else
                list(pd.date_range('2024-01-01', periods=n).strftime('%Y-%m-%d')),
        'close': closes,
        'high': [c * 1.01 for c in closes],
        'low': list(lows) if lows is not None else [c * 0.99 for c in closes],
        'pctb': list(pctb) if pctb is not None else [0.5] * n,
        'ma20': list(ma20) if ma20 is not None else [c * 1.0 for c in closes],
        'vol_ratio20': list(volr) if volr is not None else [1.0] * n,
    })


def snap(kind=regime.RANGE, i0=0, i1=None, n=None, start='2024-01-01', **over):
    """regime.snapshot 的形状（段边界手工给，不必真跑判定）"""
    s = {'date': '2024-01-99', 'kind': kind,
         'label_cn': regime.LABEL_CN.get(kind, kind),
         'level_cn': regime.LABEL_CN.get(kind, kind), 'strong': False,
         'changed': False, 'prev': kind,
         'segment': {'i0': i0, 'i1': (n - 1) if i1 is None else i1, 'start': start,
                     'days': 1, 'end': '2024-01-99'}}
    s.update(over)
    return s


def tc(**over):
    t = dict(T)
    t.update(over)
    return t


# ---------- 配置 ----------
def test_tcfg_defaults_and_coercion():
    """默认纪律就是需求里那组数字；max_hold 取整、enabled 归布尔、脏值不改结构"""
    d = tips.tcfg({})
    assert (d['buy_pctb'], d['sell_pctb'], d['stop_pct'], d['max_hold']) == \
        (0.10, 0.90, 5.0, 15)
    assert abs(d['pb_touch'] - 1.5) < 1e-9 and abs(d['vol_shrink'] - 0.8) < 1e-9
    assert d['enabled'] is True
    got = tips.tcfg({'t_trade': {'max_hold': 6.7, 'enabled': 0, 'stop_pct': '3.5'}})
    assert got['max_hold'] == 6 and got['enabled'] is False and got['stop_pct'] == 3.5
    assert tips.tcfg({'t_trade': {'max_hold': 0}})['max_hold'] == 1      # 至少 1 日
    assert set(tips.DEF) == set(tips.KEYS)                # 参数量与默认值逐项对应
    assert set(tips.MARK) == set(tips.ACTION_CN)          # 每个动作都有得渲染


# ---------- 震荡段：下沿买入 ----------
def test_buy_tip_at_lower_band_opens_discipline_track():
    """%B 到 0.10 以下 → 🟢 买入提示，同时给出 −5% 硬止损位与最长持有天数"""
    df = mk(closes=[10.0, 10.2, 10.1], pctb=[0.6, 0.5, 0.08])
    t = tips.range_tips(df, snap(n=3), T)
    assert t['action'] == 'buy' and t['mark'] == '🟢'
    assert t['kind'] == regime.RANGE
    assert abs(t['stop_price'] - 10.1 * 0.95) < 1e-6      # 自当日收盘往下 5%
    assert t['entry_date'] == df['date'].values[-1] and abs(t['entry_close'] - 10.1) < 1e-6
    assert t['hold_days'] == 1 and t['left_days'] == T['max_hold'] - 1
    assert abs(t['pctb'] - 0.08) < 1e-6 and t['risk'] == ''


def test_wait_when_upper_side_of_range():
    """没到下沿就只是等待提示，不编造任何价位"""
    df = mk(closes=[10.0] * 3, pctb=[0.6, 0.7, 0.55])
    t = tips.range_tips(df, snap(n=3), T)
    assert t['action'] == 'wait' and t['mark'] == '⚪'
    assert '未到下沿' in t['reason']
    assert t['stop_price'] is None and t['entry_date'] is None and t['hold_days'] == 0


def test_upper_band_sell_after_earlier_buy():
    """段内先触发下沿、末日 %B 到上沿 → 🔺 卖出提示，并回指当初的提示买入日"""
    df = mk(closes=[10.0, 10.3, 10.6], pctb=[0.05, 0.5, 0.95],
            dates=['2024-01-02', '2024-01-03', '2024-01-04'])
    t = tips.range_tips(df, snap(n=3, start='2024-01-02'), T)
    assert t['action'] == 'sell' and t['mark'] == '🔺'
    assert t['entry_date'] == '2024-01-02' and abs(t['entry_close'] - 10.0) < 1e-6
    assert '已到上沿' in t['reason']


def test_hard_stop_wins_over_upper_band_and_expiry():
    """−5% 硬止损优先级最高：同日既到上沿又破止损时，仍提示止损"""
    df = mk(closes=[10.0, 9.6], lows=[9.9, 9.4], pctb=[0.05, 0.95])
    t = tips.range_tips(df, snap(n=2, start='2024-01-01'), T)
    assert t['action'] == 'stop' and t['mark'] == '🛑'
    assert abs(t['stop_price'] - 9.5) < 1e-6               # 10.0 × 0.95
    assert '触及硬止损位' in t['reason']


def test_stop_only_triggers_after_a_buy():
    """没触发过下沿就没有“提示买入价”，再深的跌也不给止损提示（不凭空记账）"""
    df = mk(closes=[10.0, 8.0], lows=[9.9, 7.5], pctb=[0.7, 0.5])
    t = tips.range_tips(df, snap(n=2), T)
    assert t['action'] == 'wait' and t['stop_price'] is None


def test_expire_when_max_hold_reached():
    """最长持有日数到了还没到上沿 → ⌛ 超期提示（不让区间单变成长线）"""
    n = 4
    df = mk(closes=[10.0] * n, pctb=[0.05] + [0.5] * (n - 1))
    t = tips.range_tips(df, snap(n=n), tc(max_hold=3))
    assert t['action'] == 'expire' and t['mark'] == '⌛'
    assert '超最长 3 日纪律' in t['reason']


def test_hold_in_the_middle_of_track():
    """段内还在上下沿之间：给 🔵 持仓提示，并说清第几天、还剩几天"""
    df = mk(closes=[10.0, 10.2, 10.1], pctb=[0.05, 0.4, 0.5],
            dates=['2024-01-02', '2024-01-03', '2024-01-04'])
    t = tips.range_tips(df, snap(n=3, start='2024-01-02'), tc(max_hold=10))
    assert t['action'] == 'hold' and t['mark'] == '🔵'
    assert t['hold_days'] == 3 and t['left_days'] == 7
    assert t['entry_date'] == '2024-01-02' and t['stop_price'] is not None


def test_track_restarts_after_position_is_closed():
    """止损离场后段内还可以再给一次买入提示（一笔完结核算完，不累计成第二笔）"""
    df = mk(closes=[10.0, 9.4, 9.5], lows=[9.9, 9.0, 9.45], pctb=[0.05, 0.4, 0.06])
    t = tips.range_tips(df, snap(n=3), T)
    assert t['action'] == 'buy' and t['entry_date'] == df['date'].values[-1]
    assert abs(t['entry_close'] - 9.5) < 1e-6


def test_unformed_segment_and_missing_pctb_fall_back():
    """波段还没成形（没有段边界）或 %B 缺值：只报等待/不提示，不抛异常"""
    df = mk(closes=[10.0, 10.1], pctb=[0.5, 0.5])
    s = snap(n=2)
    s['segment'] = {'i0': None, 'i1': None, 'start': None}
    assert tips.range_tips(df, s, T)['reason'] == '震荡段还没成形，暂不提示'
    nan = mk(closes=[10.0, 10.1], pctb=[np.nan, np.nan])
    t = tips.range_tips(nan, snap(n=2), T)
    assert t['action'] == 'wait' and '缺值' in t['reason'] and t['pctb'] is None
    assert tips.range_tips(df, None, T) is None


# ---------- 多头段：回踩介入 ----------
def test_bull_watch_needs_all_three_conditions():
    """回踩 MA20 + 缩量 + 止跌三条全凑才给 🟠 介入提示，并且必须带最高风险警示"""
    df = mk(closes=[10.0, 10.2], lows=[10.4, 10.05], ma20=[10.1, 10.1],
            volr=[1.5, 0.6])
    t = tips.bull_watch(df, snap(kind=regime.BULL, n=2), T)
    assert t['action'] == 'watch' and t['mark'] == '🟠'
    assert '最高风险' in t['risk'] and t['need'] == []
    assert abs(t['ma20'] - 10.1) < 1e-6 and abs(t['vol_ratio'] - 0.6) < 1e-6


def test_bull_watch_lists_what_is_missing():
    """缺一条就只给 ⚪ 介入观察，并把缺的那条写清楚（不含任何价位与仓位）"""
    df = mk(closes=[10.0, 10.2], lows=[10.4, 10.05], ma20=[10.1, 10.1],
            volr=[1.5, 1.2])
    t = tips.bull_watch(df, snap(kind=regime.BULL, n=2), T)
    assert t['action'] == 'bull_wait' and '缩量' in t['need'][0]
    assert t['risk'] == ''
    # 回踩没到 + 收盘没收上来：两条都列出来
    df2 = mk(closes=[10.0, 9.8], lows=[10.4, 10.4], ma20=[10.1, 10.1], volr=[1.5, 0.5])
    t2 = tips.bull_watch(df2, snap(kind=regime.BULL, n=2), T)
    assert t2['action'] == 'bull_wait' and len(t2['need']) == 2
    assert any('回踩' in x for x in t2['need']) and any('止跌' in x for x in t2['need'])


def test_bull_watch_without_ma20_is_safe():
    """MA20 还没算出来的多头段：不硬判回踩"""
    df = mk(closes=[10.0, 10.1], ma20=[np.nan, np.nan])
    t = tips.bull_watch(df, snap(kind=regime.BULL, n=2), T)
    assert t['action'] == 'off' and '缺值' in t['reason']


# ---------- 第一纪律：趋势段停手 ----------
def test_trend_segments_get_no_range_tips():
    """多头/空头段都不会走到区间买入分支（结构性停手，不是文案过滤）"""
    df = mk(closes=[10.0, 10.1], pctb=[0.02, 0.02])       # 就算 %B 到了下沿
    for kind in (regime.BULL, regime.BEAR):
        t = tips.range_tips(df, snap(kind=kind, n=2), T)
        assert t['action'] == 'off' and t['mark'] == '⛔', kind
        assert t['stop_price'] is None and t['pctb'] is None


def test_range_to_bear_is_stop_avoidance():
    """震荡 → 空头（当日确认切换）：止损回避优先于区间提示本身"""
    df = mk(closes=[10.0, 9.4], pctb=[0.5, 0.06])
    s = snap(kind=regime.BEAR, n=2, changed=True, prev=regime.RANGE)
    t = tips.for_stock(df, DEFAULT_CONFIG, s)
    assert t['action'] == 'avoid' and t['mark'] == '🛑'
    assert '止损回避' in t['action_cn'] and t['level_cn'] == regime.LABEL_CN[regime.BEAR]


def test_for_stock_dispatch_and_disabled_switch():
    """统一入口的分派：震荡给区间提示、多头给回踩、空头给停手；关掉开关或无数据一律 None"""
    cfg = dict(DEFAULT_CONFIG)
    rng = mk(closes=[10.0] * 3, pctb=[0.6, 0.5, 0.07])
    assert tips.for_stock(rng, cfg, snap(n=3))['action'] == 'buy'
    bull = mk(closes=[10.0, 10.2], lows=[10.4, 10.05], ma20=[10.1, 10.1], volr=[1.5, 0.6])
    assert tips.for_stock(bull, cfg, snap(kind=regime.BULL, n=2))['action'] == 'watch'
    bear = mk(closes=[10.0, 9.9], pctb=[0.5, 0.4])
    assert tips.for_stock(bear, cfg, snap(kind=regime.BEAR, n=2))['action'] == 'off'
    # 开关关掉 → 页面与播报都不出提示
    off = dict(cfg, t_trade=dict(cfg['t_trade'], enabled=False))
    assert tips.for_stock(rng, off, snap(n=3)) is None
    # 数据不足（三态还没判定）→ 不提示
    assert tips.for_stock(rng, cfg, snap(kind=regime.UNKNOWN, n=3)) is None
    assert tips.for_stock(rng, cfg, None) is None
    assert tips.for_stock(None, cfg, snap(n=3)) is None
