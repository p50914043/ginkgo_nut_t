# -*- coding: utf-8 -*-
"""三态行情引擎测试：均线 + ADX 判据（多头/空头/震荡、强/弱两档）/ 滞回 / 毛刺过滤 / 切换分级

这是本工具唯一的环境判定层：仪表盘、飞书播报、历史波段统计、图上色块
必须都是同一份结果（只做行情识别，不产生任何交易指令）。
"""
import sys
sys.path.insert(0, 'libs')
sys.path.insert(0, '.')

import collections
import numpy as np
import pandas as pd

from core import indicators as ind
from core import regime
from core.config import DEFAULT_CONFIG

CFG = dict(DEFAULT_CONFIG)


def mk(closes, start='2020-01-01'):
    closes = np.asarray(closes, dtype=float)
    n = len(closes)
    df = pd.DataFrame({
        'date': pd.date_range(start, periods=n).strftime('%Y-%m-%d'),
        'open': np.r_[closes[0], closes[:-1]], 'close': closes,
        'high': closes * 1.012, 'low': closes * 0.988,
        'volume': [1000000] * n,
    })
    df['turnover'] = 1.0
    df['amount'] = df['close'] * df['volume']
    return ind.compute_factors(df)


def _wave(n=150, level=13.0, amp=0.15, period=10.0):
    """高频小幅来回：MA20 与 MA60 都贴着中枢走平，均线判据下就是纯震荡"""
    return [level + amp * np.sin(i * 2 * np.pi / period) for i in range(n)]


UP = list(np.linspace(10, 16, 140))          # 中期结构向上且正在上行
DOWN = list(np.linspace(16, 10, 140))        # 中期结构向下且正在下行
SIDE = _wave()                               # 走平：无方向
RISE_AFTER_SIDE = SIDE + [13.0 + 0.15 * i for i in range(1, 21)]   # 震荡末端突然起涨


def kinds(closes, **over):
    cfg = dict(CFG, regime=dict(CFG['regime'], **over))
    codes, _raw = regime.regime_arrays(mk(closes), cfg)
    return [c for c in codes if c != regime.UNKNOWN]


def states(closes, **over):
    """返回 (生效三态, 逐日强/弱)，用来验证“阈值只改强档、不改三态归属”"""
    cfg = dict(CFG, regime=dict(CFG['regime'], **over))
    codes, _raw, flags = regime.regime_full(mk(closes), cfg)
    live = [(c, f) for c, f in zip(codes, flags) if c != regime.UNKNOWN]
    return [c for c, _f in live], [f for _c, f in live]


def test_three_states_from_moving_averages_only():
    """判据只用均线 + ADX：收盘在 MA20 一侧且斜率同向 = 多/空，其余 = 震荡"""
    assert kinds(UP)[-1] == regime.BULL
    assert kinds(DOWN)[-1] == regime.BEAR
    assert set(kinds(SIDE)) == {regime.RANGE}
    # 三态只描述行情本身：含义文案里不能出现动作/指令用词
    assert set(regime.DESC_CN.values()) == {
        '中期均线向上 · 趋势行情', '中期均线向下 · 趋势行情',
        '均线纠缠 · 区间震荡', '均线未就绪 · 不判定'}
    for word in ('买入', '卖出', '做多', '做空', '低吸', '高抛', '模式'):
        assert all(word not in v for v in regime.DESC_CN.values())
    # 波段字段直接带上含义，图上色块与页面文案不用自己再翻译
    df = mk(SIDE)
    codes, _raw = regime.regime_arrays(df, CFG)
    segs = regime.segments(list(df['date']), codes, df['close'].values)
    assert all(s['desc'] == regime.DESC_CN[regime.RANGE] for s in segs)


def test_unknown_until_slow_ma_ready():
    """慢线未就绪的 bar 一律 UNKNOWN（不能拿前半段乱判成多头）"""
    codes, _raw = regime.regime_arrays(mk(UP), CFG)
    assert all(c == regime.UNKNOWN for c in codes[:59])   # MA60 首根有效=索引59
    assert codes[59] == regime.BULL
    # UNKNOWN 既不算多头也不算震荡，天数统计里单列（回测页要交代清楚）
    days = regime.regime_arrays(mk(UP), CFG)[0]
    assert collections.Counter(list(days))[regime.UNKNOWN] == 59


def test_slope_and_adx_only_gate_the_strong_tier():
    """slope_min / adx_min 只决定“强/弱”两档，不改三态归属：弱趋势日仍算多头/空头"""
    seq = UP + _wave(60, level=16.0)
    lo, lo_strong = states(seq, slope_min=0.02)
    hi, hi_strong = states(seq, slope_min=5.0)
    assert lo == hi                         # 三态划分完全不变
    assert sum(hi_strong) < sum(lo_strong)  # 抬高阈值只把“强档”吃掉
    assert hi[-1] == regime.RANGE                  # 末段走平：与阈值无关，都是震荡
    hi_up, hi_up_strong = states(UP, slope_min=5.0)
    assert hi_up[-1] == regime.BULL and not hi_up_strong[-1]   # 只算弱多头
    # ADX 门槛同理：顶到 100 后没有一天够“强”，但三态照旧
    ax, ax_strong = states(seq, adx_min=100.0)
    assert ax == lo and sum(ax_strong) == 0
    # 默认参数下：单边上涨是强多头，单边下跌是强空头，来回走平既不强也无方向
    up_c, up_f = states(UP)
    dn_c, dn_f = states(DOWN)
    assert up_c[-1] == regime.BULL and up_f[-1]
    assert dn_c[-1] == regime.BEAR and dn_f[-1]
    assert not any(states(SIDE)[1])


def test_weak_labels_and_spike_merge():
    """弱档只降级标注（弱多头/弱空头）；不足 spike_days 日的毛刺并入相邻主状态"""
    assert regime.WEAK_CN[regime.BULL] == '弱多头'
    assert regime.WEAK_CN[regime.BEAR] == '弱空头'
    dates = ['d%d' % i for i in range(9)]
    codes = np.array(['bull'] * 4 + ['bear'] * 1 + ['bull'] * 4, dtype='<U5')
    close = np.ones(9)
    # spike_days=0：那段 1 天的空头算一个波段；=3：被并入更长的左边多头段
    raw = regime.segments(dates, codes, close)
    assert [s['kind'] for s in raw] == [regime.BULL, regime.BEAR, regime.BULL]
    merged = regime.segments(dates, codes, close, spike_days=3)
    assert [s['kind'] for s in merged] == [regime.BULL]
    assert merged[0]['i0'] == 0 and merged[0]['i1'] == 8 and merged[0]['days'] == 9
    # 强/弱标记逐段计数：末日不强则段叫“弱多头”
    strong = np.array([True] * 6 + [False] * 3, dtype=bool)
    seg = regime.segments(dates, np.array(['bull'] * 9, dtype='<U5'), np.ones(9),
                          strong=strong, spike_days=3)
    assert seg[0]['n_strong'] == 6 and seg[0]['weak_days'] == 3
    assert seg[0]['strong_now'] is False and seg[0]['level_cn'] == '弱多头'


def test_flip_grade_marks_range_to_trend_as_strong():
    """切换分级：震荡→趋势是强信号（🔴🔴），趋势→震荡只是动能变化（🟡）"""
    g = regime.flip_grade(regime.RANGE, regime.BULL)
    assert (g['grade'], g['mark']) == ('strong', '🔴🔴')
    assert regime.flip_grade(regime.RANGE, regime.BEAR)['grade'] == 'strong'
    assert regime.flip_grade(regime.BULL, regime.RANGE)['grade'] == 'momentum'
    assert regime.flip_grade(regime.BEAR, regime.RANGE)['mark'] == '🟡'
    assert regime.flip_grade(regime.BULL, regime.BEAR)['grade'] == 'strong'   # 直接互转也算强
    assert regime.flip_grade(regime.RANGE, regime.RANGE)['grade'] == 'other'


def test_slow_ma_period_is_configurable_without_breaking():
    """慢线周期改成非默认值时必须在现场补算，不能因为缺列而全体退化成“数据不足”"""
    for slow in (30, 60, 100):
        got = kinds(UP, ma_slow=slow)
        assert got and got[-1] == regime.BULL, slow
    # 慢线周期变短 → 更快认趋势：同样数据下多头天数不会变少
    assert (kinds(UP, ma_slow=30).count(regime.BULL) >=
            kinds(UP, ma_slow=100).count(regime.BULL))


def test_hysteresis_requires_confirmation_days():
    """滞回：新状态要连续 confirm_days 天才切换；期间生效态不变，原始判定作为“酝酿中”"""
    raw = np.array(['bull'] * 5 + ['range'] * 3 + ['bull'] * 4, dtype='<U5')
    cur = list(regime.smooth(raw, 3))
    assert cur[:7] == ['bull'] * 7              # 震荡头两天不改生效态
    assert cur[7:10] == ['range'] * 3           # 第 3 天凑满→从当天起生效
    assert cur[10:] == ['bull'] * 2             # 再转多头又要重新数三天
    # confirm_days=1 即无滞回，逐日照原始判定
    assert list(regime.smooth(raw, 1)) == list(raw)
    # 未就绪（UNKNOWN）的 bar 不改状态，也不打断已累积的天数
    gap = np.array(['bull'] * 3 + ['bear', 'bear', regime.UNKNOWN, 'bear', 'bear'],
                   dtype='<U5')
    g = list(regime.smooth(gap, 3))
    assert g[:5] == ['bull'] * 5                # 空头头两天不改生效态
    assert g[5] == regime.UNKNOWN               # 未就绪的 bar 不强行贴上一个状态
    assert g[6:] == ['bear'] * 2                # 跳开一天也没把已攒的天数清零


def test_segments_merge_days_and_carry_chart_fields():
    """波段合并：K线背景色块与详情页波段表都吃这一个结果（天数/段内涨跌/高低/含义/配色）"""
    df = mk(UP + SIDE)
    codes, _raw = regime.regime_arrays(df, CFG)
    segs = regime.segments(list(df['date']), codes, df['close'].values,
                           df['high'].values, df['low'].values)
    assert len(segs) >= 2
    for s in segs:
        assert s['days'] == s['i1'] - s['i0'] + 1
        assert s['start'] <= s['end']
        assert s['label_cn'] == regime.LABEL_CN[s['kind']]
        assert s['desc'] == regime.DESC_CN[s['kind']]
        assert s['color'] == regime.COLOR[s['kind']] and s['fill']
        assert s['hi'] >= s['lo'] and s['chg_pct'] == round(
            (df['close'].values[s['i1']] / df['close'].values[s['i0']] - 1) * 100, 2)
    assert sum(1 for s in segs if s.get('current')) == 1
    assert segs[-1]['current'] is True
    # 相邻同状态必须并成一段，不能一天一块
    merged = regime.segments(['d%d' % i for i in range(4)],
                             np.array(['bull'] * 4, dtype='<U5'),
                             np.array([1.0, 1.1, 1.2, 1.3]))
    assert len(merged) == 1 and merged[0]['days'] == 4
    assert merged[0]['chg_pct'] == 30.0
    # UNKNOWN 不进波段
    assert regime.segments(['d'], np.array([regime.UNKNOWN], dtype='<U5'),
                           np.array([1.0])) == []


def test_snapshot_answers_daily_questions():
    """每日播报要的字段：今天什么态、第几天、是否刚切换、是否在酝酿、判据数值"""
    df = mk(UP)
    snap = regime.snapshot(df, CFG)
    assert snap['date'] == df['date'].values[-1]
    assert snap['kind'] == regime.BULL and snap['label_cn'] == '多头'
    assert snap['desc'] == regime.DESC_CN[regime.BULL] and snap['days'] >= 1
    assert snap['segment']['kind'] == snap['kind']
    assert snap['n_segments'] >= 1 and snap['confirm_days'] == CFG['regime']['confirm_days']
    assert snap['ma']['mid'] and snap['ma']['slow'] and snap['ma']['slope5'] is not None
    assert snap['ma']['mid'] > snap['ma']['slow']       # 多头判据本身就写在这里
    assert snap['changed'] in (True, False) and snap['prev'] in regime.LABEL_CN
    # 强/弱档与分级阈值也得给齐（页面与播报直接拿这几个字段解释“为什么是弱”）
    assert snap['strong'] is True and snap['level_cn'] == '多头'
    assert snap['adx'] is not None and snap['adx_min'] == CFG['regime']['adx_min']
    assert snap['spike_days'] == CFG['regime']['spike_days']
    assert snap['grade'] is None            # 没切换就没分级
    # 震荡日：只说“区间震荡”，不给出任何动作
    r = regime.snapshot(mk(SIDE), CFG)
    assert r['kind'] == regime.RANGE and r['desc'] == regime.DESC_CN[regime.RANGE]
    # 数据不足 / 空表：界面按“无三态”渲染，不得抛异常
    assert regime.snapshot(mk(UP[:30]), CFG)['kind'] == regime.UNKNOWN
    assert regime.snapshot(pd.DataFrame(), CFG) is None


def test_snapshot_flags_change_and_pending():
    """趋势变化提醒：凑满确认日→changed；没凑满→pending（先别动手）"""
    df = mk(RISE_AFTER_SIDE)
    # confirm_days=1：找到真正切换成多头的那一天，只截到那天，snapshot 必须报“今日变化”
    one = dict(CFG, regime=dict(CFG['regime'], confirm_days=1))
    codes, _raw = regime.regime_arrays(df, one)
    flip = next(i for i in range(1, len(codes))
                if codes[i] == regime.BULL and codes[i - 1] != regime.BULL)
    assert codes[flip - 1] == regime.RANGE
    fast = regime.snapshot(df.iloc[:flip + 1], one)
    assert fast['kind'] == regime.BULL and fast['changed'] is True
    assert fast['prev'] == regime.RANGE and fast['prev_label_cn'] == '震荡'
    assert fast['pending'] == regime.UNKNOWN        # 已经切换，不再报“酝酿中”
    assert fast['days'] == 1 and fast['desc'] == regime.DESC_CN[regime.BULL]   # 本波段第 1 日
    assert fast['changed'] and fast['grade']['grade'] == 'strong'              # 震荡→多头：🔴🔴
    slow = regime.snapshot(df, dict(CFG, regime=dict(CFG['regime'], confirm_days=30)))
    assert slow['kind'] == regime.RANGE             # 30 天没凑满，生效态还是震荡
    assert slow['changed'] is False
    assert slow['pending'] == regime.BULL
    assert 0 < slow['pending_days'] < 30
    assert slow['pending_label_cn'] == '多头' and slow['confirm_days'] == 30
    assert slow['days'] >= 1                        # 本波段（震荡）已持续的天数
