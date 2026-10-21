# -*- coding: utf-8 -*-
"""飞书通知：加签算法、通道选择、dry-run 行为、趋势播报文案"""
import sys
sys.path.insert(0, 'libs')
sys.path.insert(0, '.')

import base64
import hashlib
import hmac

from core import notifier
from core import regime
from core.notifier import gen_sign, push, describe_channel, format_trend_message
from core.config import DEFAULT_CONFIG

# 交易层已按需求删除：推送里不允许再出现任何指令用词
FORBIDDEN = ('买入', '卖出', '做多', '做空', '低吸', '高抛', '止盈', '止损',
             '模式', '网格', '仓位', '下单', '提醒价')


def test_gen_sign_matches_feishu_algorithm():
    ts, secret = 1700000000, 'sec_demo'
    key = ('%d\n%s' % (ts, secret)).encode('utf-8')
    expect = base64.b64encode(hmac.new(key, b'', digestmod=hashlib.sha256).digest()).decode()
    assert gen_sign(secret, ts) == expect
    # 不同 secret / 时间戳产生不同签名
    assert gen_sign('other', ts) != expect
    assert gen_sign(secret, ts + 1) != expect


def test_push_dry_run_does_not_network(monkeypatch):
    called = []
    monkeypatch.setattr(notifier, '_post', lambda *a, **k: called.append(1) or {'code': 0})
    cfg = dict(DEFAULT_CONFIG, feishu_webhook='https://example.invalid/hook/x')
    res = push('t', ['line1'], cfg, dry_run=True)
    assert res['msg'] == 'dry_run' and 'line1' in res['text']
    assert called == []


def test_push_prefers_app_bot_when_configured(monkeypatch):
    seen = {}

    def fake_post(url, payload, headers=None, timeout=15):
        seen['url'] = url
        seen['auth'] = (headers or {}).get('Authorization', '')
        seen['payload'] = payload
        if 'tenant_access_token' in url:
            return {'code': 0, 'tenant_access_token': 't-abc', 'expire': 7200}
        return {'code': 0, 'msg': 'success', 'data': {'message_id': 'om_x'}}

    monkeypatch.setattr(notifier, '_token_cache', {})
    monkeypatch.setattr(notifier, '_post', fake_post)
    cfg = dict(DEFAULT_CONFIG,
               feishu_app_id='cli_x', feishu_app_secret='sec', feishu_receive_id='ou_abc')
    res = push('标题', ['正文'], cfg)
    assert res['channel'] == 'app_bot' and res['code'] == 0
    assert '/im/v1/messages?receive_id_type=open_id' in seen['url']
    assert seen['auth'] == 'Bearer t-abc'
    assert seen['payload']['receive_id'] == 'ou_abc'
    assert '正文' in seen['payload']['content']


def test_token_cached_across_sends(monkeypatch):
    calls = []

    def fake_post(url, payload, headers=None, timeout=15):
        calls.append(url)
        if 'tenant_access_token' in url:
            return {'code': 0, 'tenant_access_token': 't-cache', 'expire': 7200}
        return {'code': 0, 'msg': 'success'}

    monkeypatch.setattr(notifier, '_token_cache', {})
    monkeypatch.setattr(notifier, '_post', fake_post)
    cfg = dict(DEFAULT_CONFIG, notify_channel='app_bot', feishu_app_id='cli_cache',
               feishu_app_secret='sec', feishu_receive_id='ou_1')
    push('T', ['a'], cfg)
    push('T', ['b'], cfg)
    assert sum('tenant_access_token' in u for u in calls) == 1  # 第二次复用缓存


def test_push_falls_back_to_webhook(monkeypatch):
    captured = {}

    def fake_post(url, payload, headers=None, timeout=15):
        captured['url'] = url
        captured['payload'] = payload
        return {'code': 0, 'msg': 'ok'}

    monkeypatch.setattr(notifier, '_post', fake_post)
    cfg = dict(DEFAULT_CONFIG, feishu_webhook='https://open.feishu.cn/open-apis/bot/v2/hook/tok',
               feishu_webhook_secret='mysecret')
    res = push('T', ['L'], cfg)
    assert res['channel'] == 'webhook' and captured['url'].endswith('/hook/tok')
    assert 'sign' in captured['payload'] and 'timestamp' in captured['payload']


def test_push_app_bot_incomplete_config(monkeypatch):
    monkeypatch.setattr(notifier, '_post', lambda *a, **k: (_ for _ in ()).throw(AssertionError('should not call')))
    cfg = dict(DEFAULT_CONFIG, notify_channel='app_bot', feishu_app_id='cli_x')
    res = push('T', ['L'], cfg)
    assert res['code'] != 0 and '配置不全' in res['msg']


def test_token_error_is_returned(monkeypatch):
    monkeypatch.setattr(notifier, '_token_cache', {})
    monkeypatch.setattr(notifier, '_post',
                        lambda url, payload, headers=None, timeout=15: {'code': 10003, 'msg': 'invalid app_secret'})
    res = notifier.app_send_text('cli_x', 'bad', 'ou_1', 'T', ['L'])
    assert res['code'] == 10003 and res['channel'] == 'app_bot'


def test_push_unconfigured_is_not_silent(monkeypatch):
    monkeypatch.setattr(notifier, '_post', lambda *a, **k: (_ for _ in ()).throw(AssertionError('should not call')))
    res = push('T', ['L'], dict(DEFAULT_CONFIG))
    assert res['code'] == -1 and 'Webhook' in res['msg']


def test_describe_channel():
    assert '未配置' in describe_channel(dict(DEFAULT_CONFIG))
    assert '应用机器人' in describe_channel(dict(DEFAULT_CONFIG, feishu_app_id='a',
                                                feishu_app_secret='b', feishu_receive_id='c'))
    d = describe_channel(dict(DEFAULT_CONFIG, notify_channel='webhook',
                              feishu_webhook='http://x', feishu_webhook_secret='s'))
    assert d == '群机器人Webhook+加签'


def _row(name, code, **over):
    r = {'name': name, 'code': code, 'close': 15.5, 'chg_pct': 0.8,
         'kind': 'bull', 'label_cn': '多头', 'level_cn': '多头', 'strong': True,
         'adx': 31.0, 'adx_min': 25.0,
         'desc': '中期均线向上 · 趋势行情',
         'days': 12, 'segment': {'start': '2026-09-28', 'n_strong': 12},
         'ma': {'mid': 15.2, 'slow': 14.6, 'slope5': 0.42},
         'changed': False, 'prev_cn': '多头', 'grade': None,
         'pending': '', 'pending_cn': '',
         'pending_days': 0, 'confirm_days': 5, 'n_segments': 4}
    r.update(over)
    return r


PING = _row('中国平安', '601318', close=51.67, chg_pct=-0.4, changed=True,
             prev_cn='震荡', days=1, segment={'start': '2026-10-19'},
             grade=regime.flip_grade(regime.RANGE, regime.BULL))
TREND = {'rows': [_row('华域汽车', '600741'),
                 _row('伊利股份', '600887', close=27.76, chg_pct=-0.4, kind='range',
                      label_cn='震荡', level_cn='震荡', strong=False, adx=14.0,
                      desc='均线纠缠 · 区间震荡', days=5, segment={},
                      ma={'mid': 27.9, 'slow': 27.8, 'slope5': -0.02},
                      ind={'pctb': 0.42},
                      pending='bear', pending_cn='空头', pending_days=2),
                 PING],
         'changed': [PING], 'strong_changed': [PING], 'momentum_changed': [],
         'pending': [{'name': '伊利股份', 'code': '600887', 'label_cn': '震荡',
                      'pending_cn': '空头', 'pending_days': 2, 'confirm_days': 5}],
         'n_bull': 2, 'n_bear': 0, 'n_range': 1, 'n_rows': 3, 'n_strong_rows': 2,
         'date': '2026-10-19', 'quiet': [{'name': '盐湖股份', 'status': '数据不足'}]}


def test_trend_message_structure():
    """需求：每天告知趋势情况与行情 —— 概览 → 逐只（收盘/涨跌/三态强弱/波段第几天/判据）→ 分级切换 → 酝酿 → 未判定"""
    txt = '\n'.join(format_trend_message('2026-10-19', TREND))
    lines = format_trend_message('2026-10-19', TREND)
    assert lines[0] == '2026-10-19 趋势播报｜盘后生成 · 只做行情识别，不产生交易指令'
    assert '📊 趋势：2 多头 · 0 空头 · 1 震荡（共 3 只已判定，其中强趋势 2 只）' in txt
    assert '华域汽车 600741 收盘 15.50+0.80% ｜ 多头 ｜ 本波段自 2026-09-28，第 12 日 ｜ ' \
           'MA20 15.20 / 慢线 14.60 · 斜率 +0.42% · ADX 31.0' in txt
    assert '伊利股份 600887 收盘 27.76-0.40% ｜ 震荡 ｜' in txt
    assert '区间位置%B 0.42' in txt                # 震荡段附加 %B 定位
    assert '已持续 5 日' in txt                       # 无起始日的波段退而求其次
    assert '🔴🔴 强信号｜震荡 → 趋势' in txt
    assert '中国平安 601318：震荡 → 多头（中期均线向上 · 趋势行情）' in txt
    assert '⏳ 酝酿中（新状态还没凑满确认天数，今日口径不变）' in txt
    assert '伊利股份 600887：当前震荡，原始判定已转空头（连续 2/5 日）' in txt
    assert '--- 未纳入判定：盐湖股份(数据不足) ---' in txt


def test_trend_message_weak_tier_and_momentum_section():
    """弱档只降级标注；趋势→震荡归入“🟡 动能变化”，不与强信号混排"""
    down = _row('牧原股份', '002714', strong=False, level_cn='弱多头',
                adx=18.0, changed=True, prev_cn='震荡',
                grade=regime.flip_grade(regime.BULL, regime.RANGE))
    t = {'rows': [down], 'changed': [down], 'strong_changed': [],
         'momentum_changed': [down], 'pending': [], 'quiet': [],
         'n_bull': 1, 'n_bear': 0, 'n_range': 0, 'n_rows': 1, 'n_strong_rows': 0}
    txt = '\n'.join(format_trend_message('d', t))
    assert '｜ 弱多头 ｜' in txt
    assert '· ADX 18.0' in txt
    assert '🟡 动能变化｜趋势 → 震荡' in txt
    assert '强信号' not in txt
    assert '其中强趋势 0 只' in txt


def test_trend_message_has_no_trading_words():
    """推送只描述行情：任何买卖/仓位/模式字样都不允许出现在播报里"""
    txt = '\n'.join(format_trend_message('2026-10-19', TREND))
    for w in FORBIDDEN:
        assert w not in txt, w


def test_trend_message_quiet_when_no_change():
    """没有趋势变化时明确说“延续各自波段”，不要留空白段落"""
    t = dict(TREND, changed=[], strong_changed=[], momentum_changed=[],
             rows=[_row('华域汽车', '600741')],
             pending=[], n_bull=1, n_bear=0, n_range=0, n_rows=1, quiet=[])
    txt = '\n'.join(format_trend_message('d', t))
    assert '今日无趋势变化：1 只标的延续各自波段' in txt
    assert '强信号｜' not in txt and '动能变化｜' not in txt
    assert '其它切换' not in txt
    assert '酝酿中' not in txt
    assert '未纳入判定' not in txt


def test_trend_message_empty_trend_is_safe():
    """空结果（全部数据不足）也要出得来，且不会打印虚假的“无变化”结论"""
    lines = format_trend_message('d', None)
    assert lines[0].endswith('只做行情识别，不产生交易指令')
    assert '趋势：0 多头 · 0 空头 · 0 震荡（共 0 只已判定，其中强趋势 0 只）' in lines[1]
    assert len(lines) == 2


def test_trend_message_missing_ma_and_chg_do_not_crash():
    """缺指标的日子（MA 还没算出来 / 停牌无涨跌）按 '-' 输出，不能抛异常"""
    t = {'rows': [_row('中国移动', '600941', ma={}, chg_pct=None, close=None,
                       segment={}, days=1)],
         'changed': [], 'pending': [], 'quiet': [],
         'n_bull': 1, 'n_bear': 0, 'n_range': 0, 'n_rows': 1}
    txt = '\n'.join(format_trend_message('d', t))
    assert '中国移动 600941 收盘 0.00 ｜ 多头 ｜ 已持续 1 日 ｜ MA20 - / 慢线 - · 斜率 -' in txt


def test_build_trend_report_counts_and_splits_flags():
    """build_trend_report：changed / pending / 无三态三类必须分干净（仪表盘与推送共用）"""
    from core.scan import build_trend_report

    def snap(kind, label, **over):
        s = {'kind': kind, 'label_cn': label, 'level_cn': label, 'strong': True,
             'adx': 30.0, 'adx_min': 25.0, 'desc': 'd-' + label, 'days': 4,
             'changed': False, 'prev_label_cn': label, 'grade': None,
             'pending': '',
             'pending_label_cn': '', 'pending_days': 0, 'confirm_days': 5,
             'segment': {'start': '2026-09-01'}, 'ma': {'mid': 1.0}, 'n_segments': 2}
        s.update(over)
        return s
    results = [
        {'code': '600741', 'name': '华域汽车', 'close': 15.5, 'chg_pct': 0.8,
         'date': '2026-10-19', 'ind': {}, 'status': '正常', 'regime': snap('bull', '多头')},
        {'code': '601318', 'name': '中国平安', 'close': 51.67, 'chg_pct': -0.4,
         'date': '2026-10-19', 'ind': {}, 'status': '正常',
         'regime': snap('bull', '多头', changed=True, prev_label_cn='震荡',
                        grade=regime.flip_grade(regime.RANGE, regime.BULL))},
        {'code': '600887', 'name': '伊利股份', 'close': 27.7, 'chg_pct': 1.0,
         'date': '2026-10-19', 'ind': {}, 'status': '正常',
         'regime': snap('range', '震荡', pending='bear', pending_label_cn='空头',
                        pending_days=2)},
        {'code': '000792', 'name': '盐湖股份', 'close': 20.0, 'date': '2026-10-19',
         'ind': {}, 'status': '均线未就绪', 'regime': snap('', '数据不足')},
        {'code': '600941', 'name': '中国移动', 'close': 100.0, 'date': None,
         'ind': {}, 'status': '数据不足', 'regime': None},
    ]
    t = build_trend_report({'results': results, 'date': '2026-10-19'})
    assert (t['n_bull'], t['n_bear'], t['n_range']) == (2, 0, 1)
    assert t['n_rows'] == 3 and len(t['rows']) == 3
    assert t['n_strong_rows'] == 3            # 三只都给了 strong=True
    assert [x['code'] for x in t['changed']] == ['601318']
    assert [x['code'] for x in t['strong_changed']] == ['601318']    # 震荡→多头→强信号
    assert t['momentum_changed'] == []
    assert [x['code'] for x in t['pending']] == ['600887']
    assert [x['code'] for x in t['quiet']] == ['000792', '600941']
    assert t['date'] == '2026-10-19' and t['quiet']
    assert t['rows'][0]['desc'] == 'd-多头'
    assert t['rows'][0]['segment']['start'] == '2026-09-01'
    assert t['rows'][0]['ma']['mid'] == 1.0
    # 播报文案能直接吃这份结果（同一份口径，不再二次翻译）
    txt = '\n'.join(format_trend_message(t['date'], t))
    assert '趋势：2 多头 · 0 空头 · 1 震荡（共 3 只已判定，其中强趋势 3 只）' in txt
    assert '中国平安 601318：震荡 → 多头（d-多头）' in txt


def test_push_title_declares_recognition_only():
    """需求：工具只做行情识别 —— 标题与首行都要把这件事说清楚"""
    from core import autorun
    assert '趋势播报' in autorun.TITLE and '不产生交易指令' in autorun.TITLE
    for w in FORBIDDEN:
        assert w not in autorun.TITLE, w
    first = format_trend_message('d', None)[0]
    assert '趋势播报' in first and '只做行情识别' in first
