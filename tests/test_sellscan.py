# -*- coding: utf-8 -*-
"""判定层测试：信号 A/B/C/D 的触发与不触发、跨组计数、回测事件与前瞻收益（全部离线注入）

夹具都对齐 core/selldata 的真实列名（TRADE_DATE/REPORTDATE/REPORT_DATE/EX_DIVIDEND_DATE/
END_DATE/NOTICE_DATE），缓存回读后数值是字符串，判定层必须能吃字符串数。
"""
import sys
sys.path.insert(0, 'libs')
sys.path.insert(0, '.')

import datetime

import pandas as pd
import pytest

from core import sellscan as ss
from core import selldata

SN = {'a_yellow': 70.0, 'a_red': 80.0, 'a_years_pe': 5, 'a_years_pb': 10,
      'b_ocf_ratio': 0.5, 'b_debt_up': 10.0, 'b_growth_gap': 30.0,
      'b_roe_years': 3, 'b_div_drop': 30.0, 'b_red_groups': 3,
      'c_dd_pct': 20.0, 'c_dd_days': 250, 'c_warn_days': 30, 'c_warn_peers': 2,
      'd_on': True}


def vdf(vals, col='PE_TTM', start='2024-01-01'):
    dates = pd.date_range(start, periods=len(vals)).strftime('%Y-%m-%d')
    return pd.DataFrame({'TRADE_DATE': dates, col: vals})


def today(off=0):
    return (datetime.date.today() - datetime.timedelta(days=off)).isoformat()


# ---------------- 信号 A：估值分位 ----------------

def test_percentile_of_ignores_none_and_counts_equal_as_le():
    assert ss.percentile_of([1, 2, 3, 4], 4) == 100.0
    assert ss.percentile_of([1, 2, 3, 4], 2) == 50.0      # ≤ 当前值占比
    assert ss.percentile_of([None, 1, 2], 2) == 100.0
    assert ss.percentile_of([], 1) is None and ss.percentile_of([1], None) is None


def test_signal_a_red_when_top_percentile():
    r = ss.signal_a('600000', 'value', SN, vdf(list(range(1, 201))))
    assert r['status'] == 'ok' and r['level'] == 'red'
    assert r['metric'] == 'PE-TTM' and r['pct'] == 100.0 and r['years'] == 5


def test_signal_a_yellow_band():
    # 末值恰好落在 75% 分位：≥70 黄、<80 不红（100 行窗口，74 个低值 + 25 个高值 + 末值3）
    r = ss.signal_a('600000', 'value', SN, vdf([1] * 74 + [10] * 25 + [3]))
    assert r['pct'] == 75.0 and r['level'] == 'yellow'


def test_signal_a_none_when_low_percentile():
    r = ss.signal_a('600000', 'value', SN, vdf(list(range(200, 0, -1))))
    assert r['level'] == 'none' and r['pct'] == 0.5     # 末值是最小：仅它自己 ≤ 它 → 0.5%


def test_signal_a_uses_pb_window_for_cycle_and_insurance():
    up = list(range(1, 121))
    cyc = ss.signal_a('600585', 'cycle', SN, vdf(up, col='PB_MRQ'))
    assert cyc['metric'] == 'PB' and cyc['years'] == 10
    ins = ss.signal_a('601318', 'insurance', SN, vdf(up, col='PB_MRQ'))
    assert ins['metric'] == 'PB（代理PEV）' and ins['years'] == 5
    # 银行同样取 PB 10 年
    bn = ss.signal_a('601398', 'bank', SN, vdf(up, col='PB_MRQ'))
    assert bn['metric'] == 'PB' and bn['years'] == 10


def test_signal_a_string_numbers_from_cache_work():
    r = ss.signal_a('600000', 'value', SN, vdf(['%d' % v for v in range(1, 101)]))
    assert r['status'] == 'ok' and r['level'] == 'red'


def test_signal_a_missing_data_paths():
    assert ss.signal_a('600000', 'value', SN, pd.DataFrame())['status'] == '缺数据'
    short = ss.signal_a('600000', 'value', SN, vdf(list(range(1, 31))))
    assert short['status'] == '缺数据' and '不足' in short['detail']
    assert ss.signal_a('600000', 'value', SN, vdf([None] * 80))['status'] == '缺数据'


def test_signal_a_fetch_error_reported_not_raised(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError('net')
    monkeypatch.setattr(selldata, 'get_value', boom)
    r = ss.signal_a('600000', 'value', SN)
    assert r['status'] == '数据失败' and '拉取失败' in r['detail']


def test_signal_a_shows_pe_and_pb_for_all_types():
    # 无论什么类型，PE-TTM 与 PB-MRQ 两套分位都要返回供展示；主判仍按类型口径
    dates = pd.date_range('2016-01-01', periods=2600).strftime('%Y-%m-%d')
    df = pd.DataFrame({'TRADE_DATE': dates,
                       'PE_TTM': list(range(1, 2601)),        # 末值最高 → PE 分位 100
                       'PB_MRQ': list(range(2600, 0, -1))})   # 末值最低 → PB 分位 ~0
    r = ss.signal_a('601398', 'bank', SN, df)   # 银行主判看 PB(10年) 低位 → 不亮灯
    assert r['metric'] == 'PB' and r['level'] == 'none'
    assert r['pe']['years'] == 5 and r['pb']['years'] == 10
    assert r['pe']['pct'] == 100.0 and r['pb']['pct'] is not None and r['pb']['pct'] <= 1.0


def test_dividend_yield_ttm():
    d1 = datetime.date.today()
    d2 = d1 - datetime.timedelta(days=200)
    df = pd.DataFrame([{'EX_DIVIDEND_DATE': d1.isoformat(), 'PRETAX_BONUS_RMB': 5.0},
                       {'EX_DIVIDEND_DATE': d2.isoformat(), 'PRETAX_BONUS_RMB': 3.0}])
    assert ss.dividend_yield_ttm(df, 20.0) == 4.0     # 每股 0.5+0.3=0.8 / 20 → 4%
    old = pd.DataFrame([{'EX_DIVIDEND_DATE': (d1 - datetime.timedelta(days=400)).isoformat(),
                         'PRETAX_BONUS_RMB': 5.0}])
    assert ss.dividend_yield_ttm(old, 20.0) == 0.0    # 超一年，TTM 内无派息
    assert ss.dividend_yield_ttm(None, 20.0) is None and ss.dividend_yield_ttm(df, None) is None


# ---------------- 信号 B：跨组计数 ----------------

def _fm(**kw):
    """默认帧：2023→2025 年报 + 2025H1/2026H1 两期，营收净利连降、ROE 连降"""
    fm = {'2023-12-31': {'TOTAL_OPERATE_INCOME': 1200, 'PARENT_NETPROFIT': 150,
                         'WEIGHTAVG_ROE': 30.0, 'BOARD_NAME': '水泥'},
          '2024-12-31': {'TOTAL_OPERATE_INCOME': 1100, 'PARENT_NETPROFIT': 130,
                         'WEIGHTAVG_ROE': 25.0, 'BOARD_NAME': '水泥'},
          '2025-06-30': {'TOTAL_OPERATE_INCOME': 500, 'PARENT_NETPROFIT': 60,
                         'WEIGHTAVG_ROE': 6.0, 'BOARD_NAME': '水泥'},
          '2025-12-31': {'TOTAL_OPERATE_INCOME': 1000, 'PARENT_NETPROFIT': 100,
                         'WEIGHTAVG_ROE': 20.0, 'BOARD_NAME': '水泥',
                         'NOTICE_DATE': '2026-03-20', 'ASSIGNDSCRPT': '10派5'},
          '2026-06-30': {'TOTAL_OPERATE_INCOME': 450, 'PARENT_NETPROFIT': 50,
                         'WEIGHTAVG_ROE': 5.0, 'BOARD_NAME': '水泥'}}
    fm.update(kw)
    return fm


def test_signal_b_group1_yellow_with_two_consecutive_declines():
    r = ss.signal_b_parts(_fm(), {}, {}, {}, SN, roe_ref=25.0)
    assert r['n_groups'] == 1 and r['level'] == 'yellow'
    hits = r['groups'][0]['hits']
    assert any('营收连续2期负增长' in h for h in hits)
    assert any('归母净利连续2期负增长' in h for h in hits)
    assert any('ROE 近 3 年连降且低于行业均值' in h for h in hits)
    assert r['detail'].startswith('命中 1 组')


def test_signal_b_group1_none_when_healthy():
    # 把“最近两期”与最新年报都改成正增长 + ROE 回升，三组应全部不命中
    fm = _fm(**{'2025-12-31': {'TOTAL_OPERATE_INCOME': 1300, 'PARENT_NETPROFIT': 160,
                               'WEIGHTAVG_ROE': 32.0, 'BOARD_NAME': '水泥',
                               'NOTICE_DATE': '2026-03-20'},
                '2026-06-30': {'TOTAL_OPERATE_INCOME': 700, 'PARENT_NETPROFIT': 90,
                               'WEIGHTAVG_ROE': 8.0, 'BOARD_NAME': '水泥'}})
    r = ss.signal_b_parts(fm, {}, {}, {}, SN, roe_ref=10.0)
    assert r['n_groups'] == 0 and r['level'] == 'none'
    assert r['detail'] == '三组均未命中'


def test_signal_b_group1_ocf_needs_positive_profit_both_periods():
    cf = {'2025-12-31': {'NETCASH_OPERATE': 10}, '2026-06-30': {'NETCASH_OPERATE': 20}}
    r = ss.signal_b_parts(_fm(), cf, {}, {}, SN)
    assert any('经营现金流/净利润' in h for h in r['groups'][0]['hits'])
    # 净利为负的期不参与比值判定（避免负负得正的假信号）
    fm = _fm(**{'2026-06-30': {'TOTAL_OPERATE_INCOME': 450, 'PARENT_NETPROFIT': -50,
                               'WEIGHTAVG_ROE': 5.0}})
    r2 = ss.signal_b_parts(fm, cf, {}, {}, SN)
    assert not any('经营现金流' in h for h in r2['groups'][0]['hits'])


def test_signal_b_group2_debt_and_growth_gap():
    bal = {'2024-12-31': {'DEBT_ASSET_RATIO': '40', 'ACCOUNTS_RECE': '100', 'INVENTORY': '50'},
           '2025-12-31': {'DEBT_ASSET_RATIO': '60', 'ACCOUNTS_RECE': '300', 'INVENTORY': '100'}}
    fm = _fm(**{'2025-12-31': dict(_fm()['2025-12-31'], TOTAL_OPERATE_INCOME=1110)})
    r = ss.signal_b_parts(fm, {}, bal, {}, SN)
    hits = r['groups'][1]['hits']
    assert any('资产负债率同比升' in h for h in hits)
    assert any('应收/存货平均增速超营收' in h for h in hits)
    assert r['n_groups'] == 2                       # 同组两条只计 1 组（跨组才计数）


def test_signal_b_group3_dividend_cut_and_cancel():
    d10 = {'REPORT_DATE': '2024-12-31', 'PRETAX_BONUS_RMB': '10.0', 'TOTAL_SHARES': '1000000000'}
    d5 = {'REPORT_DATE': '2025-12-31', 'PRETAX_BONUS_RMB': '5.0', 'TOTAL_SHARES': '1000000000'}
    # 2025 年度 5亿 vs 2024 年度 10亿 → 降50% > 30% 命中
    r = ss.signal_b_parts(_fm(), {}, {}, {'2025-07-01': d10, '2026-07-01': d5}, SN)
    hits = r['groups'][2]['hits']
    assert any('2025 年度分红 5.0 亿' in h and '降50.0%' in h for h in hits)
    # 取消：最新年报写明“不分配”→ 当年分红记 0，较上年命中“取消”
    fm2 = _fm(**{'2025-12-31': dict(_fm()['2025-12-31'], ASSIGNDSCRPT='不分配不转增')})
    r2 = ss.signal_b_parts(fm2, {}, {}, {'2025-07-01': d10}, SN)
    assert any('2025 年度分红 取消' in h for h in r2['groups'][2]['hits'])


def test_signal_b_group3_midyear_dividend_not_false_positive():
    """护栏：只比“已披露年报的完整财年 vs 上一个有分红年度”，
    当年仅披露中期分红绝不能被误当成完整财年腰斩（平安 2026 假阳性的回归用例）。"""
    d10 = {'REPORT_DATE': '2024-12-31', 'PRETAX_BONUS_RMB': '10.0', 'TOTAL_SHARES': '1000000000'}
    d5 = {'REPORT_DATE': '2025-12-31', 'PRETAX_BONUS_RMB': '10.0', 'TOTAL_SHARES': '1000000000'}
    mid = {'REPORT_DATE': '2026-06-30', 'PRETAX_BONUS_RMB': '5.0', 'TOTAL_SHARES': '1000000000'}
    # 三年年报分红都是 10亿；额外一笔 2026 中期 5亿不应触发（2026 还没年报）
    div = {'k2024': d10, 'k2025': d5, 'kmid': mid}
    div = {('2025-07-01' if r['REPORT_DATE'] == '2024-12-31' else
            '2026-07-01' if r['REPORT_DATE'] == '2025-12-31' else '2026-08-01'): r
           for r in div.values()}
    r = ss.signal_b_parts(_fm(), {}, {}, div, SN)
    assert r['groups'][2]['hits'] == []


def test_signal_b_red_when_groups_reach_threshold():
    fm = _fm()
    bal = {'2024-12-31': {'DEBT_ASSET_RATIO': '40', 'ACCOUNTS_RECE': '100', 'INVENTORY': '50'},
           '2025-12-31': {'DEBT_ASSET_RATIO': '60', 'ACCOUNTS_RECE': '300', 'INVENTORY': '100'}}
    div = {'2025-07-01': {'REPORT_DATE': '2024-12-31', 'PRETAX_BONUS_RMB': '10.0',
                          'TOTAL_SHARES': '1000000000'}}
    r = ss.signal_b_parts(fm, {}, bal, div, SN)
    assert r['n_groups'] == 3 and r['level'] == 'red'


def test_signal_b_frames_injection_and_missing(monkeypatch):
    fina = pd.DataFrame([{'REPORTDATE': '2026-06-30', 'TOTAL_OPERATE_INCOME': '450',
                          'PARENT_NETPROFIT': '50', 'WEIGHTAVG_ROE': '5',
                          'BOARD_NAME': '水泥'}])
    empty = pd.DataFrame()
    r = ss.signal_b('600585', SN, frames={'fina': fina, 'cashflow': empty,
                                          'balance': empty, 'dividend': empty})
    assert r['status'] == 'ok' and r['latest_report'] == '2026-06-30'
    assert ss.signal_b('600585', SN, frames={'fina': empty, 'cashflow': empty,
                                             'balance': empty, 'dividend': empty})['status'] == '缺数据'

    def boom(code):
        raise RuntimeError('net')
    monkeypatch.setattr(selldata, 'get_fina', boom)
    assert ss.signal_b('600585', SN)['status'] == '数据失败'


# ---------------- 信号 C：回撤 × 同行预警（与门） ----------------

def _idf(closes):
    dates = pd.date_range('2025-01-01', periods=len(closes)).strftime('%Y-%m-%d')
    return pd.DataFrame({'date': dates, 'close': closes})


def _peers(n=3, off=2):
    return [{'code': '6000%02d' % i, 'name': '同%d' % i, 'notice_date': today(off),
             'predict_type': '预减'} for i in range(n)]


def test_signal_c_yellow_only_when_both_conditions_hold():
    idf = _idf(list(range(100, 120)) + list(range(119, 79, -1)))     # 高点 139 → 79，回撤 >20%
    both = ss.signal_c('600585', '1.000300', '水泥', SN, _peers(3), idf=idf)
    assert both['level'] == 'yellow' and both['dd_ok'] and both['peers_ok']
    one = ss.signal_c('600585', '1.000300', '水泥', SN, _peers(1), idf=idf)
    assert one['level'] == 'none' and one['n_peers'] == 1            # 同行不足 → 不报
    flat = ss.signal_c('600585', '1.000300', '水泥', SN, _peers(3),
                       idf=_idf([100] * 60 + [110] * 30))
    assert flat['level'] == 'none' and not flat['dd_ok']             # 没回撤 → 不报


def test_signal_c_old_or_turnaround_warnings_do_not_count():
    idf = _idf(list(range(100, 120)) + list(range(119, 79, -1)))
    stale = [{'code': '600001', 'name': '旧', 'notice_date': today(90),
              'predict_type': '预减'}] * 3
    assert ss.signal_c('600585', '', '水泥', SN, stale, idf=idf)['n_peers'] == 0
    turn = [{'code': '6000%02d' % i, 'name': '扭%d' % i, 'notice_date': today(2),
             'predict_type': '扭亏'} for i in range(3)]
    assert ss.signal_c('600585', '', '水泥', SN, turn, idf=idf)['n_peers'] == 0


def test_is_neg_warn_words():
    assert ss.is_neg_warn('预减') and ss.is_neg_warn('续亏') and ss.is_neg_warn('首亏')
    assert not ss.is_neg_warn('预增') and not ss.is_neg_warn('扭亏')
    assert not ss.is_neg_warn('') and not ss.is_neg_warn(None)       # 空类型绝不判预警


def test_signal_c_missing_index(monkeypatch):
    r = ss.signal_c('600585', '', '水泥', SN, [], idf=pd.DataFrame())
    assert r['status'] == '缺数据'

    def boom(*a, **k):
        raise RuntimeError('net')
    monkeypatch.setattr(selldata, 'get_index_kline', boom)
    assert ss.signal_c('600585', '', '水泥', SN, [])['status'] == '数据失败'


# ---------------- 信号 D：社保/保险减持或退出 ----------------

def _hdf(rows):
    return pd.DataFrame([{'END_DATE': e, 'HOLDER_NAME': n, 'HOLD_NUM': h, 'HOLD_RATIO': 1.0}
                         for e, n, h in rows])


def test_holder_kind_recognition():
    assert ss.holder_kind('全国社保基金一一八组合') == '社保'
    assert ss.holder_kind('中国人寿保险股份有限公司') == '保险'
    assert ss.holder_kind('xx养老保险') == '保险'
    assert ss.holder_kind('安徽海螺集团') == ''


def test_signal_d_yellow_on_reduction_and_exit():
    hdf = _hdf([('2025-09-30', '全国社保基金一零一组合', 100),
                ('2025-09-30', '中国平安人寿-传统保险', 200),
                ('2025-09-30', '某制造集团', 300),
                ('2025-12-31', '全国社保基金一零一组合', 80),
                ('2025-12-31', '某制造集团', 300)])
    r = ss.signal_d('600000', SN, hdf=hdf)
    assert r['level'] == 'yellow' and len(r['hits']) == 2
    assert r['cur_end'] == '2025-12-31' and r['prev_end'] == '2025-09-30'
    assert any('减持' in h['note'] for h in r['hits'])
    assert any('退出十大' in h['note'] for h in r['hits'])


def test_signal_d_none_when_flat_or_non_inst():
    hdf = _hdf([('2025-09-30', '某制造集团', 300), ('2025-09-30', '社保一', 100),
                ('2025-12-31', '某制造集团', 350), ('2025-12-31', '社保一', 100)])
    assert ss.signal_d('600000', SN, hdf=hdf)['level'] == 'none'


def test_signal_d_off_and_insufficient_periods():
    assert ss.signal_d('600000', dict(SN, d_on=False))['status'] == 'off'
    one = ss.signal_d('600000', SN, hdf=_hdf([('2025-12-31', '社保一', 100)]))
    assert one['status'] == '缺数据'
    assert ss.signal_d('600000', SN, hdf=pd.DataFrame())['status'] == '缺数据'


def test_signal_d_fetch_error(monkeypatch):
    def boom(code):
        raise RuntimeError('net')
    monkeypatch.setattr(selldata, 'get_holders', boom)
    assert ss.signal_d('600000', SN)['status'] == '数据失败'


# ---------------- 回测构件 ----------------

def test_fwd_returns_after_event_window():
    k = pd.DataFrame({'date': ['2026-01-%02d' % i for i in range(1, 31)],
                      'close': [10.0] * 10 + [12.0] * 20})
    r = ss._fwd_returns(k, '2026-01-05')
    assert r['ret5'] == 0.0 and r['ret10'] == 20.0 and r['ret20'] == 20.0
    assert ss._fwd_returns(None, '2026-01-05') == {}
    assert ss._fwd_returns(k, '2030-01-01') == {}                    # 事件在数据之外


def test_a_events_fire_on_percentile_crossing_only():
    # 一路新高的序列：分位长期 100%（一个持续预警区间）→ 只记首个上穿事件，不逐日报
    vals = [10 + i * 0.1 for i in range(200)] + [50.0] + [50.0] * 10
    ev = ss._a_events('600000', 'value', SN, vdf(vals), '2024-01-01')
    assert len(ev) == 1 and ev[0]['sig'] == 'A' and ev[0]['level'] == 'red'
    assert '分位' in ev[0]['detail']


def test_b_events_dedup_same_hits_and_respect_start():
    fm = _fm(**{'2025-12-31': dict(_fm()['2025-12-31'], NOTICE_DATE='2026-03-20'),
                '2026-06-30': dict(_fm()['2026-06-30'], NOTICE_DATE='2026-08-30')})
    ev = ss._b_events('600585', SN, fm, {}, {}, {}, '2026-01-01', None)
    assert len(ev) == 1 and ev[0]['sig'] == 'B' and ev[0]['level'] == 'yellow'
    assert ss._b_events('600585', SN, fm, {}, {}, {}, '2027-01-01', None) == []


def test_d_events_per_quarter_pair():
    hdf = _hdf([('2025-06-30', '社保一', 100),
                ('2025-09-30', '社保一', 100),
                ('2025-12-31', '社保一', 80)])
    ev = ss._d_events('600000', SN, hdf, '2025-01-01')
    assert [e['date'] for e in ev] == ['2025-12-31']                 # 上一对持平不记事件
    assert ev[0]['sig'] == 'D' and ev[0]['level'] == 'yellow'


def test_c_events_first_day_of_both_conditions():
    closes = [100] * 40 + [60] * 30
    dates = pd.date_range('2026-01-01', periods=70).strftime('%Y-%m-%d')
    idf = pd.DataFrame({'date': dates, 'close': closes})
    warn = [('600001', True, dates[-5]), ('600002', True, dates[-4]),
            ('600003', False, dates[-3])]
    ev = ss._c_events('600585', SN, idf, warn, '2026-01-01')
    assert len(ev) == 1 and ev[0]['sig'] == 'C' and ev[0]['level'] == 'yellow'
    assert '同行预警 2 家' in ev[0]['detail']


def test_append_and_read_log(tmp_path, monkeypatch):
    p = str(tmp_path / 'sell_log.jsonl')
    monkeypatch.setattr(ss, 'LOG_PATH', p)
    assert ss.read_log(5) == []
    ss.append_log({'ts': '2026-10-22 20:00:00', 'dry': True, 'ok': True,
                   'code': 0, 'msg': '', 'channel': 'webhook', 'text': 'a\nb'})
    ss.append_log({'ts': '2026-10-22 20:01:00', 'dry': False, 'ok': True,
                   'code': 0, 'msg': '', 'channel': 'webhook', 'text': 'c'})
    logs = ss.read_log(5)
    assert len(logs) == 2 and logs[0]['ts'] == '2026-10-22 20:01:00'  # 倒序：最新在前
    assert len(ss.read_log(1)) == 1
