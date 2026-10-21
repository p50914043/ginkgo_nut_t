# -*- coding: utf-8 -*-
"""技术指标计算：MA/MACD/BOLL/ATR/ADX/振幅/量比等（全部向量化）"""
import numpy as np
import pandas as pd


def ma(s, n):
    return s.rolling(n).mean()


def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def macd(close, fast=12, slow=26, signal=9):
    dif = ema(close, fast) - ema(close, slow)
    dea = ema(dif, signal)
    hist = (dif - dea) * 2.0
    return dif, dea, hist


def boll(close, n=20, k=2.0):
    mid = ma(close, n)
    std = close.rolling(n).std(ddof=0)
    return mid, mid + k * std, mid - k * std


def true_range(df):
    prev_close = df['close'].shift(1)
    tr = pd.concat([
        df['high'] - df['low'],
        (df['high'] - prev_close).abs(),
        (df['low'] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr


def atr(df, n=14):
    """Wilder ATR"""
    return true_range(df).ewm(alpha=1.0 / n, adjust=False).mean()


def adx(df, n=14):
    """Wilder ADX，返回 (adx, plus_di, minus_di)"""
    high, low = df['high'], df['low']
    up = high.diff()
    down = -low.diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=df.index)
    tr_smooth = true_range(df).ewm(alpha=1.0 / n, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1.0 / n, adjust=False).mean() / tr_smooth
    minus_di = 100 * minus_dm.ewm(alpha=1.0 / n, adjust=False).mean() / tr_smooth
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx_ = dx.ewm(alpha=1.0 / n, adjust=False).mean()
    return adx_, plus_di, minus_di


def rsi(close, n=14):
    """Wilder RSI"""
    diff = close.diff()
    up = diff.clip(lower=0)
    dn = (-diff).clip(lower=0)
    au = up.ewm(alpha=1.0 / n, adjust=False).mean()
    ad = dn.ewm(alpha=1.0 / n, adjust=False).mean()
    rs = au / ad.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def compute_factors(df):
    """在日线 DataFrame 上追加全部因子列（不修改原对象）"""
    out = df.copy()
    c = out['close']
    # 均线体系（三态行情识别用）：快线 MA5 / 中线 MA20 / 慢线 MA60
    out['ma5'] = ma(c, 5)
    out['ma20'] = ma(c, 20)
    out['ma60'] = ma(c, 60)
    out['dif'], out['dea'], out['macd_hist'] = macd(c)
    out['boll_mid'], out['boll_up'], out['boll_low'] = boll(c)
    out['atr'] = atr(out)
    out['adx'], out['plus_di'], out['minus_di'] = adx(out)
    out['prev_close'] = c.shift(1)
    # 振幅：(high-low)/前收
    out['amp'] = (out['high'] - out['low']) / out['prev_close'] * 100
    out['amp_ma20'] = out['amp'].rolling(20).mean()
    out['amp_med20'] = out['amp'].rolling(20).median()
    out['turn_ma20'] = out['turnover'].rolling(20).mean() if 'turnover' in out else np.nan
    out['amt_ma20'] = out['amount'].rolling(20).mean() if 'amount' in out else np.nan
    out['vol_ma20'] = ma(out['volume'], 20)
    out['vol_ratio20'] = out['volume'] / out['vol_ma20']
    out['ma20_slope5'] = (out['ma20'] - out['ma20'].shift(5)) / out['ma20'].shift(5).abs() * 100
    # 盘面参考指标（只用于描述行情）：RSI、唐奇安通道、布林带宽
    out['rsi14'] = rsi(c, 14)
    out['dc_hi'] = out['high'].rolling(20).max()
    out['dc_lo'] = out['low'].rolling(20).min()
    out['dc_hi10'] = out['high'].rolling(10).max()
    out['dc_lo10'] = out['low'].rolling(10).min()
    with np.errstate(invalid='ignore', divide='ignore'):
        out['bb_width'] = (out['boll_up'] - out['boll_low']) / out['boll_mid'] * 100
        # %B：收盘在布林带内的相对位置（0=下轨、1=上轨）。不参与三态判定，
        # 只用于震荡段的区间定位（页面与详情页展示）。
        out['pctb'] = (c - out['boll_low']) / (out['boll_up'] - out['boll_low']).replace(0, np.nan)
        out['box_pos'] = (c - out['dc_lo']) / (out['dc_hi'] - out['dc_lo']).replace(0, np.nan)
        # Kaufman 效率比：净位移/路径总长，越低越“震荡”，越高越“趋势”
        out['er10'] = (c - c.shift(10)).abs() / c.diff().abs().rolling(10).sum().replace(0, np.nan)
        out['er5'] = (c - c.shift(5)).abs() / c.diff().abs().rolling(5).sum().replace(0, np.nan)
    return out


def snapshot(row):
    """当日截面指标快照（仪表盘与飞书播报共用）：缺列/NaN 一律给 None，交给模板的 num 过滤器"""
    def f(name, nd=3):
        try:
            v = float(row.get(name))
        except (TypeError, ValueError):
            return None
        return None if v != v else round(v, nd)

    close, ma20 = f('close'), f('ma20')
    return {
        'date': row.get('date'), 'close': close, 'open': f('open'),
        'chg_pct': (round((close / f('prev_close') - 1) * 100, 2)
                    if close is not None and f('prev_close') else None),
        'ma5': f('ma5'), 'ma20': ma20, 'ma60': f('ma60'),
        'bias20': (round((close / ma20 - 1) * 100, 2)
                   if close is not None and ma20 else None),
        'slope5': f('ma20_slope5', 2),
        'adx': f('adx', 1), 'plus_di': f('plus_di', 1), 'minus_di': f('minus_di', 1),
        'atr': f('atr'), 'rsi': f('rsi14', 1), 'er10': f('er10'),
        'bb_width': f('bb_width', 1), 'amp20': f('amp_ma20', 2),
        'pctb': f('pctb', 2),
        'vol_ratio': f('vol_ratio20', 2),
        'dif': f('dif', 3), 'dea': f('dea', 3), 'macd_hist': f('macd_hist', 3),
        'boll_mid': f('boll_mid'), 'boll_up': f('boll_up'), 'boll_low': f('boll_low'),
        'box_lo': f('dc_lo'), 'box_hi': f('dc_hi'), 'box_pos': f('box_pos', 2),
        'turnover': f('turnover', 2), 'amount': f('amount', 0),
    }
