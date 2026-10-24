# -*- coding: utf-8 -*-
"""数据层测试：日期截断 / 缓存读写约定 / 指数 secid 归一化（全部离线，不触网）"""
import sys
sys.path.insert(0, 'libs')
sys.path.insert(0, '.')

import pandas as pd
import pytest

from core import selldata as sd


def test_d_truncates_datetime(monkeypatch):
    assert sd._d('2026-06-30 00:00:00') == '2026-06-30'
    assert sd._d('2026-06-30') == '2026-06-30'
    assert sd._d(None) == '' and sd._d('') == ''
    assert sd._d('abcdef') == ''            # 非日期串不硬截


def test_key_normalizes_stock_and_keeps_index_key():
    assert sd._key('sh.600519') == '600519'
    assert sd._key('600519') == '600519'
    assert sd._key('1_000300') == '1_000300'      # 指数自定义 key 原样用，不许抛
    p = sd._paths('1.000300'.replace('.', '_'), 'index')
    assert p[0].endswith('1_000300_index.csv') and p[1].endswith('.stamp')


def test_df_sorts_dedups_and_truncates_dates():
    rows = [{'TRADE_DATE': '2026-01-02 00:00:00', 'PE_TTM': 2},
            {'TRADE_DATE': '2026-01-01 00:00:00', 'PE_TTM': 1},
            {'TRADE_DATE': '2026-01-01 00:00:00', 'PE_TTM': 3},
            {'TRADE_DATE': '', 'PE_TTM': 9}]
    df = sd._df(rows, 'TRADE_DATE')
    assert list(df['TRADE_DATE']) == ['2026-01-01', '2026-01-02']    # 升序 + 空日期剔除
    assert list(df['PE_TTM']) == [3, 2]                              # 同日期保留后一条
    assert sd._df([], 'TRADE_DATE').empty


@pytest.fixture
def ctmp(tmp_path, monkeypatch):
    monkeypatch.setattr(sd, 'SELL_DIR', str(tmp_path))
    return tmp_path


def test_cached_fetch_stores_original_column_names(ctmp):
    calls = []

    def fetcher():
        calls.append(1)
        return pd.DataFrame([{'TRADE_DATE': '2026-01-01', 'PE_TTM': '10.5'},
                             {'TRADE_DATE': '2026-01-02', 'PE_TTM': '11.0'}])

    df = sd._cached_fetch('600519', 'value', 'TRADE_DATE', fetcher)
    assert calls == [1]
    assert 'TRADE_DATE' in df.columns          # 落盘保留原列名，回读列名不能漂移
    assert sd._stamp('600519', 'value') == sd._today()
    # 当日已缓存：再取不再调 fetcher
    df2 = sd._cached_fetch('600519', 'value', 'TRADE_DATE', fetcher)
    assert len(calls) == 1 and len(df2) == 2
    # refresh=True 强制重拉
    sd._cached_fetch('600519', 'value', 'TRADE_DATE', fetcher, refresh=True)
    assert len(calls) == 2


def test_cached_fetch_falls_back_to_old_cache_on_error(ctmp):
    good = pd.DataFrame([{'REPORTDATE': '2026-06-30', 'X': '1'}])
    sd._cached_fetch('601318', 'fina', 'REPORTDATE', lambda: good)
    # 把 stamp 调回昨天：走“重新拉取”路径但 fetcher 抛错 → 退回旧缓存而不是炸
    with open(sd._paths('601318', 'fina')[1], 'w') as f:
        f.write('2020-01-01')

    def boom():
        raise RuntimeError('network down')

    df = sd._cached_fetch('601318', 'fina', 'REPORTDATE', boom)
    assert len(df) == 1 and df.iloc[0]['REPORTDATE'] == '2026-06-30'
    # 无缓存可退时原样抛出（调用方按票记“数据失败”）
    with pytest.raises(RuntimeError):
        sd._cached_fetch('000001', 'fina', 'REPORTDATE', boom)


def test_load_cached_filters_since(ctmp):
    df = pd.DataFrame([{'NOTICE_DATE': '2026-01-01'}, {'NOTICE_DATE': '2026-05-01'}])
    sd._store('600001', 'forecast', df, 'NOTICE_DATE')
    got = sd._load_cached('600001', 'forecast', 'NOTICE_DATE', '2026-03-01')
    assert list(got['NOTICE_DATE']) == ['2026-05-01']
    assert sd._load_cached('600001', 'nokey', 'NOTICE_DATE') is None   # 没落过盘给 None


def test_resolve_index_secid():
    assert sd.resolve_index_secid('') == sd.DEFAULT_INDEX
    assert sd.resolve_index_secid('1.000300') == '1.000300'
    assert sd.resolve_index_secid('sh.000300') == '1.000300'
    assert sd.resolve_index_secid('sz.399997') == '0.399997'
    assert sd.resolve_index_secid('399997') == '0.399997'
    assert sd.resolve_index_secid('000905') == '1.000905'      # 中证系列走沪市源
    assert sd.resolve_index_secid('000300.SS') == '1.000300'
    with pytest.raises(ValueError):
        sd.resolve_index_secid('hs300')


def test_fetch_value_keeps_only_needed_columns(monkeypatch):
    monkeypatch.setattr(sd, '_dc_all', lambda *a, **k: [
        {'TRADE_DATE': '2026-06-30 00:00:00', 'PE_TTM': 8.1, 'PB_MRQ': 1.2,
         'CLOSE_PRICE': 40.0, 'TOTAL_MARKET_CAP': 1.0, 'BOARD_NAME': '保险',
         'USELESS_COL': 'x'},
        {'TRADE_DATE': '2020-01-01 00:00:00', 'PE_TTM': 9.9, 'PB_MRQ': 1.5,
         'CLOSE_PRICE': 50.0, 'TOTAL_MARKET_CAP': 2.0, 'BOARD_NAME': '保险'}])
    df = sd.fetch_value('601318', '2026-01-01')
    assert len(df) == 1                              # start_date 之前的行不入库
    assert 'USELESS_COL' not in df.columns
    assert {'TRADE_DATE', 'PE_TTM', 'PB_MRQ', 'BOARD_NAME'} <= set(df.columns)


def test_fetch_forecast_filters_empty_rows(monkeypatch):
    monkeypatch.setattr(sd, '_dc_all', lambda *a, **k: [])
    assert sd.fetch_forecast('600000', '2026-01-01').empty


def test_get_index_kline_em_then_tencent_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(sd, 'SELL_DIR', str(tmp_path))
    cols = ['date', 'open', 'close', 'high', 'low', 'volume', 'amount']
    em_rows = [['2026-06-%02d' % i, '1', '2', '3', '1', '100', '200'] for i in range(1, 4)]
    tx_rows = [['2026-06-03', '1', '2.5', '3', '1', '100']]

    def em(secid, start, end):
        if secid == '1.000300':
            return em_rows, '沪深300'
        raise RuntimeError('RemoteDisconnected')    # 东财对部分指数直接断连

    monkeypatch.setattr(sd, '_index_kline_em', em)
    monkeypatch.setattr(sd, '_index_kline_tx', lambda s, a, b: (tx_rows, ''))
    df = sd.get_index_kline('1.000300', '2026-06-01')
    assert list(df['date']) == ['2026-06-01', '2026-06-02', '2026-06-03']
    assert df['close'].iloc[-1] == 2.0               # 数值列已转 float
    # 东财断连 → 腾讯兜底产数
    df2 = sd.get_index_kline('0.399997', '2026-06-01')
    assert list(df2['date']) == ['2026-06-03'] and df2['close'].iloc[0] == 2.5
