# -*- coding: utf-8 -*-
"""三态行情识别：多头 / 空头 / 震荡（日线收盘判定，只用均线 + ADX）

判据（逐 bar 收盘后判定）：
  多头 bull ：收盘 > MA20 且 MA20 > MA慢线 且 MA20 的 5 日斜率 > +slope_min 且 ADX(14) > adx_min
  空头 bear ：收盘 < MA20 且 MA20 < MA慢线 且 MA20 的 5 日斜率 < -slope_min 且 ADX(14) > adx_min
  震荡 range：其余（价格反复穿越 MA20、斜率走平）

强/弱两档：四条全满足 = 强（结构 + 方向 + 动能都在）；只满足“收盘在 MA20 一侧且斜率同向”
= 弱（结构还没翻或 ADX 不到）。弱档不改变三态归属，只降级标注，避免把刚起步/快收尾的
趋势日直接判成震荡。

为什么不用 MA5≥MA20≥MA60 这种“三线排列”做必要条件：回调日 MA5 几乎必然下穿 MA20，
三线排列会把正常回调都判成“不是多头”，波段被切得稀碎。所以中线固定 MA20，MA5 只展示。

防闪烁两层：
  滞回 smooth()：新状态要连续 confirm_days 日才切换；没凑满的作为 pending（酝酿中）播报；
  毛刺过滤 merge_spikes()：持续不足 spike_days 日的片段并入相邻主状态，不算一个波段。

本工具只做行情识别，不产生交易指令：仪表盘、飞书播报、K 线背景色块与历史波段统计
都读同一份结果，页面/推送/图上三处不会出现口径漂移。
"""
import numpy as np

BULL = 'bull'
BEAR = 'bear'
RANGE = 'range'
UNKNOWN = ''          # 均线未就绪（数据不足 60 根）

MA_MID = 20           # 中线固定 MA20：斜率列 ma20_slope5 是三态判据的一半，写死不开放调整
LABEL_CN = {BULL: '多头', BEAR: '空头', RANGE: '震荡', UNKNOWN: '数据不足'}
# 三态含义（只描述行情，不含任何动作/指令用词）
DESC_CN = {BULL: '中期均线向上 · 趋势行情', BEAR: '中期均线向下 · 趋势行情',
           RANGE: '均线纠缠 · 区间震荡', UNKNOWN: '均线未就绪 · 不判定'}
WEAK_CN = {BULL: '弱多头', BEAR: '弱空头'}
COLOR = {BULL: '#ef5350', BEAR: '#26a69a', RANGE: '#94a3b8', UNKNOWN: '#cbd5e1'}
FILL = {BULL: 'rgba(239,83,80,0.10)', BEAR: 'rgba(38,166,154,0.12)',
        RANGE: 'rgba(148,163,184,0.18)', UNKNOWN: 'rgba(203,213,225,0.10)'}

DEF = {'ma_fast': 5, 'ma_slow': 60, 'slope_min': 0.3, 'confirm_days': 3,
       'adx_min': 25.0, 'spike_days': 3}
KEYS = tuple(DEF)

# 切换分级：震荡→趋势是方向确立（强信号），趋势→震荡是动能衰减（只提示减速），
# 历史统计口径见 501 个交易日样本（切换次日收盘买入、看未来 10 日）
FLIP_STATS = {
    (RANGE, BULL): {'grade': 'strong', 'mark': '🔴🔴', 'ret': 4.55, 'win': 89.0},
    (RANGE, BEAR): {'grade': 'strong', 'mark': '🔴🔴', 'ret': -3.10, 'win': 78.0},
    (BULL, RANGE): {'grade': 'momentum', 'mark': '🟡', 'ret': -0.99, 'win': None},
    (BEAR, RANGE): {'grade': 'momentum', 'mark': '🟡', 'ret': 0.14, 'win': None},
}


def flip_grade(prev, cur):
    """(前态, 今态) → 分级信息；趋势直接互转按强信号处理，未知组合按其它"""
    info = FLIP_STATS.get((prev, cur))
    if info:
        return dict(info)
    if prev in (BULL, BEAR) and cur in (BULL, BEAR):
        return {'grade': 'strong', 'mark': '🔴🔴', 'ret': None, 'win': None}
    return {'grade': 'other', 'mark': '⚪', 'ret': None, 'win': None}


def rcfg(cfg):
    r = dict(DEF)
    r.update((cfg or {}).get('regime') or {})
    r['ma_fast'] = int(r['ma_fast'])
    r['ma_slow'] = int(r['ma_slow'])
    r['confirm_days'] = max(1, int(r['confirm_days']))
    r['spike_days'] = max(0, int(r['spike_days']))
    r['slope_min'] = float(r['slope_min'])
    r['adx_min'] = float(r['adx_min'])
    return r


def _col(df, name, n):
    return df[name].values if name in df.columns else np.full(n, np.nan)


def _ma(df, window, n):
    """取 MA 列；compute_factors 只算 ma5/ma20/ma60，界面把慢线调成 30/100 等其它周期时
    现场按收盘价算，否则整列 NaN 会让三态默默退化成“数据不足”。"""
    name = 'ma%d' % window
    if name in df.columns:
        return df[name].values
    if 'close' not in df.columns:
        return np.full(n, np.nan)
    return df['close'].rolling(window, min_periods=window).mean().values


def raw_states(df, r=None):
    """返回 (逐bar原始三态, 逐bar是否“强”)；均线未就绪的 bar 给 UNKNOWN / False

    强：收盘在 MA20 正确一侧 + MA20 越过慢线 + 斜率超阈值 + ADX 够动能；
    弱：只满足“收盘在 MA20 正确一侧 + 斜率同向”（结构与动能还差一条）。
    """
    r = r or rcfg({})
    n = len(df)
    out = np.full(n, UNKNOWN, dtype='<U5')
    strong = np.zeros(n, dtype=bool)
    ms = _ma(df, r['ma_slow'], n)
    mm = _col(df, 'ma%d' % MA_MID, n)
    sl = _col(df, 'ma%d_slope5' % MA_MID, n)
    cl = _col(df, 'close', n)
    ax = _col(df, 'adx', n)
    ok = ~(np.isnan(mm) | np.isnan(sl) | np.isnan(cl))     # ADX 缺值不致命：只是不够“强”
    ok_s = ok & ~np.isnan(ms)
    thr = abs(float(r['slope_min']))
    adx_ok = np.where(np.isnan(ax), False, ax > r['adx_min'])
    up = ok_s & (cl > mm) & (sl > 0)
    dn = ok_s & (cl < mm) & (sl < 0)
    bull = up & (mm > ms) & (sl > thr) & adx_ok
    bear = dn & (mm < ms) & (sl < -thr) & adx_ok
    out[ok_s] = RANGE
    out[up] = BULL
    out[dn] = BEAR
    out[bull] = BULL
    out[bear] = BEAR
    strong[bull | bear] = True
    return out, strong


def smooth(raw, confirm_days):
    """滞回：连续 confirm_days 天给出同一个新状态才切换；未就绪 bar 不改状态"""
    need = max(1, int(confirm_days))
    out = np.full(len(raw), UNKNOWN, dtype='<U5')
    cur, pend, cnt = None, None, 0
    for i in range(len(raw)):
        s = raw[i]
        if s == UNKNOWN:
            continue
        if cur is None:
            cur = s
        elif s == cur:
            pend, cnt = None, 0
        else:
            cnt = cnt + 1 if s == pend else 1
            pend = s
            if cnt >= need:
                cur, pend, cnt = s, None, 0
        out[i] = cur
    return out


def regime_arrays(df, cfg):
    """返回 (生效状态, 原始状态)，供扫描/绘图共用"""
    r = rcfg(cfg)
    raw, _strong = raw_states(df, r)
    return smooth(raw, r['confirm_days']), raw


def regime_full(df, cfg):
    """返回 (生效状态, 原始状态, 强/弱标记)：强弱按逐 bar 原始判定，与生效态一一对齐"""
    r = rcfg(cfg)
    raw, strong = raw_states(df, r)
    return smooth(raw, r['confirm_days']), raw, strong


def _stats(seg, dates, codes, close, high, low):
    """按 i0/i1 重算一个波段的展示字段（合并毛刺后必须重算，不能用增量凑）"""
    a, b = seg['i0'], seg['i1'] + 1
    seg['start'], seg['end'] = dates[a], dates[b - 1]
    seg['days'] = b - a
    seg['kind'] = codes[a]
    c0, c1 = close[a], close[b - 1]
    with np.errstate(invalid='ignore', divide='ignore'):
        seg['chg_pct'] = round((c1 / c0 - 1.0) * 100, 2) if c0 == c0 and c0 > 0 else 0.0
        hh, ll = high[a:b], low[a:b]
        seg['hi'] = round(float(np.nanmax(hh)), 3) if np.isfinite(hh).any() else None
        seg['lo'] = round(float(np.nanmin(ll)), 3) if np.isfinite(ll).any() else None
    seg['label_cn'] = LABEL_CN[seg['kind']]
    seg['desc'] = DESC_CN[seg['kind']]
    seg['color'] = COLOR[seg['kind']]
    seg['fill'] = FILL[seg['kind']]
    return seg


def _join_same_kind(segs):
    """把相邻且同态的段并起来（毛刺并入后会出现“同色相邻两段”，不并就会多算一个波段）"""
    out = []
    for s in segs:
        if out and out[-1]['kind'] == s['kind'] and out[-1]['i1'] + 1 == s['i0']:
            out[-1]['i1'] = s['i1']
            out[-1]['n_strong'] += s['n_strong']
        else:
            out.append(s)
    return out


def merge_spikes(segs, spike_days):
    """毛刺过滤：持续不足 spike_days 日的片段并入相邻主状态（相邻两段里取更长的一方）"""
    need = int(spike_days or 0)
    if need <= 1:
        return segs
    i = 0
    while i < len(segs) and len(segs) > 1:
        s = segs[i]
        if s['days'] >= need:
            i += 1
            continue
        prev_ = segs[i - 1] if i > 0 else None
        next_ = segs[i + 1] if i + 1 < len(segs) else None
        keep = None
        if prev_ is not None and next_ is not None:
            keep = i - 1 if prev_['days'] >= next_['days'] else i + 1
        elif prev_ is not None:
            keep = i - 1
        elif next_ is not None:
            keep = i + 1
        if keep is None:
            break
        # 幸存者吸收被并段：往前并就延后终点，往后并就提前起点
        if keep < i:
            segs[keep]['i1'] = s['i1']
        else:
            segs[keep]['i0'] = s['i0']
        segs[keep]['n_strong'] += s['n_strong']
        segs.pop(i)                       # 毛刺已并进幸存者，自己不再占一段
        segs = _join_same_kind(segs)      # 并完可能让两个同态段相邻，再并一次
        i = max(0, min(keep, len(segs) - 1))
    return segs


def segments(dates, codes, close, high=None, low=None, strong=None, spike_days=0):
    """把逐日状态合并成波段（K 线背景色块与详情页波段列表都用它）

    strong 为逐 bar 的强/弱标记（与 codes 等长）：每段记 n_strong（段内强日数）与
    strong_now（段末日是否强，决定当前叫“多头”还是“弱多头”）。
    spike_days>0 时先把毛刺并入相邻主状态，再按新边界重算全部字段。
    """
    segs = []
    close = np.asarray(close, dtype=float)
    high = close if high is None else np.asarray(high, dtype=float)
    low = close if low is None else np.asarray(low, dtype=float)
    codes = list(codes)
    strong = np.zeros(len(codes), dtype=bool) if strong is None else np.asarray(strong, dtype=bool)
    for i, s in enumerate(codes):
        if s == UNKNOWN:
            continue
        if segs and segs[-1]['kind'] == s and i == segs[-1]['i1'] + 1:
            segs[-1]['i1'] = i
            segs[-1]['days'] += 1
            segs[-1]['n_strong'] += int(strong[i])
        else:
            segs.append({'kind': s, 'start': dates[i], 'end': dates[i],
                         'i0': i, 'i1': i, 'days': 1, 'n_strong': int(strong[i])})
    segs = merge_spikes(segs, spike_days)
    for sg in segs:
        _stats(sg, dates, codes, close, high, low)
        sg['weak_days'] = sg['days'] - sg['n_strong']
        sg['strong_now'] = bool(strong[sg['i1']])
        sg['level_cn'] = (LABEL_CN[sg['kind']] if sg['strong_now']
                          else WEAK_CN.get(sg['kind'], LABEL_CN[sg['kind']]))
    if segs:
        segs[-1]['current'] = True
    return segs


def _run_days(codes, i):
    """从第 i 天往回数连续相同状态的天数"""
    k = 0
    while i - k >= 0 and codes[i - k] == codes[i]:
        k += 1
    return k


def _pending_days(raw, target):
    """末尾连续 target 的天数（新状态已累积几天）"""
    k = 0
    for s in raw[::-1]:
        if s != target:
            break
        k += 1
    return k


def val_asis(df, name, nd=1):
    """取最后一根 bar 的某个指标值（缺列/NaN → None），供快照里展示 ADX 这类辅助判据"""
    if name not in getattr(df, 'columns', []):
        return None
    v = df[name].values[-1]
    return None if v != v else round(float(v), nd)


def snapshot(df, cfg, ma=None):
    """今日（最后一根bar）的三态快照：状态、强/弱、持续天数、是否刚切换、待确认的新状态"""
    r = rcfg(cfg)
    codes, raw, flags = regime_full(df, cfg)
    n = len(df)
    if not n:
        return None
    cur = codes[-1]
    sg = segments(list(df['date']), codes, df['close'].values,
                  _col(df, 'high', n), _col(df, 'low', n), flags, r['spike_days'])
    this = sg[-1] if sg and sg[-1]['kind'] == cur else None
    prev = codes[-2] if n >= 2 else UNKNOWN
    pend = raw[-1] if raw[-1] != cur else UNKNOWN
    strong_now = bool(flags[-1])
    if ma is None:
        def val(name, nd=3):
            arr = _col(df, name, n)
            v = arr[-1]
            return round(float(v), nd) if v == v else None

        def mamval(window):
            arr = _ma(df, window, n)
            v = arr[-1]
            return round(float(v), 3) if v == v else None
        ma = {'fast': mamval(r['ma_fast']), 'mid': val('ma%d' % MA_MID),
              'slow': mamval(r['ma_slow']),
              'slope5': val('ma%d_slope5' % MA_MID, 2)}
    return {
        'date': df['date'].values[-1], 'kind': cur, 'label_cn': LABEL_CN[cur],
        'desc': DESC_CN[cur], 'color': COLOR[cur], 'fill': FILL[cur],
        'strong': strong_now, 'level_cn': (LABEL_CN[cur] if strong_now else WEAK_CN.get(cur, LABEL_CN[cur])),
        'adx': val_asis(df, 'adx'), 'adx_min': r['adx_min'],
        'days': _run_days(codes, n - 1), 'changed': bool(cur != UNKNOWN and prev != cur),
        'prev': prev, 'prev_label_cn': LABEL_CN.get(prev, '数据不足'),
        'grade': flip_grade(prev, cur) if cur != UNKNOWN and prev != cur else None,
        'pending': pend, 'pending_label_cn': LABEL_CN.get(pend, ''),
        'pending_days': _pending_days(raw, pend) if pend != UNKNOWN else 0,
        'confirm_days': r['confirm_days'], 'spike_days': r['spike_days'],
        'slope_min': r['slope_min'], 'ma_fast': r['ma_fast'], 'ma_slow': r['ma_slow'],
        'ma': ma, 'segment': this, 'n_segments': len(sg),
    }
