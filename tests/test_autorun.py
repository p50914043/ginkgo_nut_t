# -*- coding: utf-8 -*-
"""盘后自动运行开关：触发条件、交易日保护、状态落盘、每日趋势播报内容"""
import datetime as dt
import sys
sys.path.insert(0, 'libs')
sys.path.insert(0, '.')

import pytest

from core import autorun
from core import regime
from core.config import DEFAULT_CONFIG


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """状态与日志写到临时目录，并把“今日”钉住为 2026-10-19（避免跨零点跑用例时假失败）"""
    monkeypatch.setattr(autorun, 'STATE_PATH', str(tmp_path / 'state.json'))
    monkeypatch.setattr(autorun, 'LOG_PATH', str(tmp_path / 'autorun.log'))
    monkeypatch.setattr(autorun, '_now', lambda: MON_19_1600)
    yield


def cfg(**kw):
    c = dict(DEFAULT_CONFIG)
    c.update(kw)
    return c


MON_19_1600 = dt.datetime(2026, 10, 19, 16, 0)     # 周一 16:00
MON_19_1500 = dt.datetime(2026, 10, 19, 15, 0)     # 周一 15:00
SAT_1530 = dt.datetime(2026, 10, 24, 15, 30)       # 周六


def test_disabled_never_runs():
    due, why = autorun.should_run(now=MON_19_1600, cfg=cfg(auto_run_enabled=False), state={})
    assert not due and '未启用' in why


def test_weekend_skipped():
    due, why = autorun.should_run(now=SAT_1530, cfg=cfg(auto_run_enabled=True), state={})
    assert not due and '周末' in why


def test_before_time_skipped():
    due, why = autorun.should_run(now=MON_19_1500, cfg=cfg(auto_run_enabled=True, auto_run_time='15:30'),
                                  state={})
    assert not due and '未到' in why


def test_due_after_time_on_weekday():
    due, why = autorun.should_run(now=MON_19_1600, cfg=cfg(auto_run_enabled=True), state={})
    assert due, why


def test_default_trigger_time_is_after_close():
    """需求：每个交易日盘后 15:35 自动重跑一次本识别"""
    assert DEFAULT_CONFIG['auto_run_time'] == '15:35'
    early = dt.datetime(2026, 10, 19, 15, 34)
    due, why = autorun.should_run(now=early, cfg=cfg(auto_run_enabled=True), state={})
    assert not due and '未到触发时间' in why


def test_only_once_per_day():
    state = {'last_run_date': '2026-10-19'}
    due, why = autorun.should_run(now=MON_19_1600, cfg=cfg(auto_run_enabled=True), state=state)
    assert not due and '已运行' in why


def test_bad_time_format():
    due, why = autorun.should_run(now=MON_19_1600, cfg=cfg(auto_run_enabled=True, auto_run_time='25:99'),
                                  state={})
    assert not due and '非法' in why


def _snap(**over):
    """regime.snapshot 的字段形状（这里手工搭，避免拉真实行情）"""
    s = {'date': '2026-10-19', 'kind': 'bull', 'label_cn': '多头',
         'strong': True, 'level_cn': '多头', 'adx': 31.0, 'adx_min': 25.0,
         'desc': '中期均线向上 · 趋势行情', 'color': '#ef5350', 'fill': 'rgba(0,0,0,.1)',
         'days': 7, 'changed': True, 'prev': 'range', 'prev_label_cn': '震荡',
         'grade': regime.flip_grade(regime.RANGE, regime.BULL),
         'pending': '', 'pending_label_cn': '', 'pending_days': 0, 'confirm_days': 5,
         'slope_min': 0.3, 'spike_days': 3, 'ma_fast': 5, 'ma_slow': 60,
         'ma': {'fast': 15.1, 'mid': 15.2, 'slow': 14.6, 'slope5': 0.42},
         'segment': {'start': '2026-10-13', 'chg_pct': 3.2, 'hi': 15.9, 'lo': 14.8,
                     'n_strong': 5},
         'n_segments': 3}
    s.update(over)
    return s


def _item(**over):
    it = {'code': '600741', 'name': '华域汽车', 'date': '2026-10-19', 'close': 15.5,
          'chg_pct': 0.8, 'ind': {}, 'adx': 28.0, 'status': '正常', 'reason': '',
          'regime': _snap()}
    it.update(over)
    return it


def _buy_tip():
    """core/tips.py 的买入提示形状（手工搭，不拉行情）"""
    return {'action': 'buy', 'mark': '🟢', 'action_cn': '买入提示｜%B 已到区间下沿',
            'kind': 'range', 'level_cn': '震荡', 'buy_pctb': 0.1, 'sell_pctb': 0.9,
            'stop_pct': 5.0, 'max_hold': 15, 'pctb': 0.08, 'stop_price': 14.6,
            'entry_date': '2026-10-19', 'entry_close': 15.35, 'hold_days': 1,
            'left_days': 14, 'reason': '买入提示｜%B 已到区间下沿', 'risk': '', 'need': []}


def test_holiday_skip_recorded_and_not_retried():
    """数据日期 != 今天 → 跳过推送并记录，同日不再重试"""
    calls = []
    results = [_item(date='2026-10-16', status='数据不足', regime=None)]
    monkey_scan = lambda cfg=None, stocks=None, refresh=True: {'results': results, 'cfg': cfg}
    orig_scan, orig_push = autorun.scan_mod.scan, autorun.push
    autorun.scan_mod.scan = monkey_scan
    autorun.push = lambda *a, **k: calls.append(1) or {'code': 0}
    try:
        c = cfg(auto_run_enabled=True)
        r = autorun.run_once(c)
        assert r['skipped'] and not r['pushed'] and calls == []
        st = autorun.load_state()
        assert st['last_skip_date'] == '2026-10-19' and st['last_result'] == '跳过'
        due, why = autorun.should_run(now=MON_19_1600, cfg=c, state=st)
        assert not due and '非交易日' in why
        # 打开重试开关后应再次尝试
        due2, _ = autorun.should_run(now=MON_19_1600, cfg=cfg(auto_run_enabled=True, auto_run_retry=True), state=st)
        assert due2
    finally:
        autorun.scan_mod.scan, autorun.push = orig_scan, orig_push


def test_trading_day_pushes_and_persists():
    """交易日：推的是趋势播报（含三态、本波段第几天、今日变化、做T提示），状态里记下行数与各类计数"""
    got = {}
    results = [_item(), _item(code='600887', name='伊利股份', close=27.76, chg_pct=-0.4,
                              regime=_snap(kind='range', label_cn='震荡', level_cn='震荡',
                                           strong=False, adx=14.0,
                                           desc='均线纠缠 · 区间震荡', days=5,
                                           changed=False, prev='range', grade=None,
                                           prev_label_cn='震荡'),
                              tip=_buy_tip())]
    orig_scan, orig_push = autorun.scan_mod.scan, autorun.push
    autorun.scan_mod.scan = lambda cfg=None, stocks=None, refresh=True: {
        'results': results, 'cfg': cfg or DEFAULT_CONFIG, 'date': '2026-10-19'}
    autorun.push = lambda title, lines, c, dry_run=False: got.update(title=title, lines=lines) or \
        {'code': 0, 'msg': 'ok', 'channel': 'webhook'}
    try:
        r = autorun.run_once(cfg(auto_run_enabled=True))
        assert r['pushed'] and r['rows'] == 2 and got['title'] == autorun.TITLE
        txt = '\n'.join(got['lines'])
        assert '趋势播报' in txt and '2 多头' not in txt   # 1 多头 + 1 震荡
        assert '趋势：1 多头 · 0 空头 · 1 震荡（共 2 只已判定，其中强趋势 1 只）' in txt
        assert '华域汽车 600741 收盘 15.50+0.80%' in txt
        assert '本波段自 2026-10-13，第 7 日' in txt
        assert '· ADX 31.0' in txt                  # 强档门槛判据一同带出
        assert '🔴🔴 强信号｜震荡 → 趋势' in txt and '震荡 → 多头' in txt
        assert '🎯 提示：1 只到动作（🟢 买入 1' in txt      # 提示只数进概览行
        assert '伊利股份 600887｜🟢 买入提示｜%B 已到区间下沿｜%B 0.08 · 硬止损位 14.60 · 第 1/15 日' in txt
        st = autorun.load_state()
        assert st['last_run_date'] == '2026-10-19' and st['last_result'] == '成功'
        assert st['last_pushed'] and 'last_skip_date' not in st
        assert st['last_rows'] == 2 and st['last_trend'] == '1/0/1'
        assert st['last_trend_changed'] == ['华域汽车']
        assert st['last_data_date'] == '2026-10-19'
        assert '数据日期 2026-10-19' in st['last_detail']
        assert '🔴🔴 1 / 🟡 0' in st['last_detail']      # 摘要也带分级计数
        assert '提示 1 只' in st['last_detail']            # 提示只数同样进摘要
        assert st['last_strong_flips'] == 1 and st['last_momentum_flips'] == 0
        assert st['last_tips'] == 1
        assert 'webhook' in open(autorun.LOG_PATH, encoding='utf-8').read()
    finally:
        autorun.scan_mod.scan, autorun.push = orig_scan, orig_push


def test_force_run_ignores_holiday_guard():
    results = [_item(date='2026-10-16')]
    orig_scan, orig_push = autorun.scan_mod.scan, autorun.push
    autorun.scan_mod.scan = lambda cfg=None, stocks=None, refresh=True: {
        'results': results, 'cfg': cfg or DEFAULT_CONFIG}
    autorun.push = lambda *a, **k: {'code': 0, 'msg': 'ok', 'channel': 'webhook'}
    try:
        r = autorun.run_once(cfg(), force=True, manual=True)
        assert not r['skipped'] and r['pushed']
    finally:
        autorun.scan_mod.scan, autorun.push = orig_scan, orig_push


def test_parse_hhmm():
    assert autorun.parse_hhmm('15:30') == (15, 30)
    assert autorun.parse_hhmm('9:05') == (9, 5)
    assert autorun.parse_hhmm('24:00') is None
    assert autorun.parse_hhmm('abc') is None
    assert autorun.parse_hhmm('') is None


def test_status_shape_without_thread():
    st = autorun.status(cfg(auto_run_enabled=False, auto_run_time='15:30'))
    assert st['enabled'] is False and st['alive'] is False
    assert st['time'] == '15:30' and '未启用' in st['why']
    assert st['last_rows'] == 0 and st['last_detail'] == '-'


def test_start_stop_idempotent(monkeypatch):
    import time
    monkeypatch.setattr(autorun, 'loop', lambda interval=30: time.sleep(3))  # 假循环，不真扫描
    assert autorun.start(cfg(auto_run_enabled=False)) is False
    assert autorun.start(cfg(auto_run_enabled=True)) is True
    assert autorun.start(cfg(auto_run_enabled=True)) is True         # 幂等
    assert autorun.status(cfg(auto_run_enabled=True))['alive'] is True
    autorun.stop()
    assert autorun._thread is None
