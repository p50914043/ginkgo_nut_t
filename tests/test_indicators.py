# -*- coding: utf-8 -*-
"""指标计算正确性测试（对照手工计算值）"""
import sys
sys.path.insert(0, 'libs')
sys.path.insert(0, '.')

import math
import numpy as np
import pandas as pd
import pytest

from core import indicators as ind


def mkdf(closes, highs=None, lows=None, opens=None, vols=None):
    n = len(closes)
    closes = np.asarray(closes, dtype=float)
    d = pd.DataFrame({
        'date': pd.date_range('2020-01-01', periods=n).strftime('%Y-%m-%d'),
        'open': opens if opens is not None else closes,
        'close': closes,
        'high': highs if highs is not None else closes * 1.01,
        'low': lows if lows is not None else closes * 0.99,
        'volume': vols if vols is not None else [1000] * n,
    })
    return d


def test_ma_ema():
    s = pd.Series([1, 2, 3, 4, 5], dtype=float)
    assert ind.ma(s, 3).iloc[-1] == 4.0
    ema = ind.ema(s, 3)
    # alpha=0.5: e1=1, e2=1.5, e3=2.25, e4=3.125, e5=4.0625
    assert abs(ema.iloc[-1] - 4.0625) < 1e-9


def test_macd_hist_definition():
    df = mkdf(list(range(1, 61)))  # 一路上涨
    dif, dea, hist = ind.macd(df['close'])
    assert hist.iloc[-1] == pytest.approx(2 * (dif.iloc[-1] - dea.iloc[-1]))
    assert dif.iloc[-1] > dea.iloc[-1]  # 上升趋势中 DIF 在 DEA 上方


def test_boll_manual():
    close = pd.Series([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], dtype=float)
    mid, up, low = ind.boll(close, n=5, k=2)
    m = close[-5:].mean()
    sd = close[-5:].std(ddof=0)
    assert mid.iloc[-1] == m
    assert up.iloc[-1] == pytest.approx(m + 2 * sd)
    assert low.iloc[-1] == pytest.approx(m - 2 * sd)


def test_atr_manual():
    # 恒定区间: high-low=2, TR=2 -> ATR=2
    df = mkdf([10.0] * 30, highs=[11.0] * 30, lows=[9.0] * 30)
    a = ind.atr(df, 14)
    assert a.iloc[-1] == pytest.approx(2.0, rel=1e-6)


def test_adx_range_and_trend():
    up = mkdf([10 + 0.3 * i for i in range(80)])
    adx_u, pdi, mdi = ind.adx(up)
    assert 0 <= adx_u.iloc[-1] <= 100
    assert pdi.iloc[-1] > mdi.iloc[-1]          # 上升趋势 +DI 占优
    flat = mkdf([10 + (0.05 if i % 2 else -0.05) for i in range(80)])
    adx_f, _, _ = ind.adx(flat)
    assert adx_f.iloc[-1] < adx_u.iloc[-1]      # 震荡市 ADX 更低


def test_compute_factors_columns():
    df = mkdf([10 + math.sin(i / 5) for i in range(120)])
    out = ind.compute_factors(df)
    for col in ('ma20', 'dif', 'dea', 'macd_hist', 'boll_mid', 'boll_up', 'boll_low',
                'atr', 'adx', 'amp', 'amp_ma20', 'amp_med20', 'vol_ma20'):
        assert col in out.columns
    assert out['atr'].iloc[-1] > 0
    assert out['boll_up'].iloc[-1] > out['boll_low'].iloc[-1]
