# -*- coding: utf-8 -*-
"""Web 层测试：K线波段数据包(_chart_bundle) / 路由与模板渲染 / 参数落盘 / 口径护栏

需求已去掉交易层：这里同时充当“页面不得再出现买卖指令与模式字样”的护栏。
"""
import sys
sys.path.insert(0, 'libs')
sys.path.insert(0, '.')

import os

import numpy as np
import pandas as pd
import pytest
import yaml

import app as web
from core import config as cfg_mod
from core import indicators as ind
from core import regime
from core import scan as scan_mod

STOCK = [{'code': '000001', 'name': '测试股'}]

# 界面禁止出现的交易/指令用词（需求：只做行情识别）
FORBIDDEN = ('买入', '卖出', '做多', '做空', '低吸', '高抛', '止盈', '止损',
             '模式B', '模式C', '网格', '下单', '盈亏', '胜率', '回测')

# 页面统一的规则区：那一块是“把判定规则与运行机制标清楚”的披露区，
# 允许引用历史统计与区间定位口径；数据区（逐票行、推送文本）仍然严格禁用。
RULES_HEAD = '判定规则与运行机制'


def strip_rules(html):
    """去掉 RULES-BEGIN/END 区间，只剩下必须严格洁净的数据区"""
    a, b = html.find('<!--RULES-BEGIN-->'), html.find('<!--RULES-END-->')
    assert 0 <= a < b, '规则区缺标记'          # 缺标记时不能默默变成“全部不扫描”
    return html[:a] + html[b + len('<!--RULES-END-->'):]


def mkdf(n=80, closes=None):
    closes = np.linspace(10, 16, n) if closes is None else np.asarray(closes, dtype=float)
    n = len(closes)
    df = pd.DataFrame({
        'date': pd.date_range('2020-01-01', periods=n).strftime('%Y-%m-%d'),
        'open': closes * 0.999, 'close': closes,
        'high': closes * 1.01, 'low': closes * 0.99,
        'volume': [1000] * n,
    })
    df['turnover'] = 1.0
    df['amount'] = df['close'] * df['volume']
    return ind.compute_factors(df)


DF = mkdf()
D = list(DF['date'])
CFGW = dict(cfg_mod.DEFAULT_CONFIG)     # K线包里的三态背景色块需要 regime 参数
# 升→平→跌的长序列：默认三态参数下能切出 多头/震荡/空头 多段（验证图上波段与切换点）
SWING_DF = mkdf(closes=list(np.linspace(10, 16, 120)) + list(np.linspace(16, 15.4, 60)) +
                list(np.linspace(15.4, 10.5, 120)))


@pytest.fixture(autouse=True)
def iso_state(tmp_path, monkeypatch):
    """仪表盘会注入盘后运行状态：把状态文件指向空的临时文件，避免历史残留文本混进页面断言"""
    monkeypatch.setattr(web.autorun, 'STATE_PATH', str(tmp_path / 'state.json'))
    yield


def bundle(df=None, start=None, end=None, cfg=None):
    return web._chart_bundle(STOCK, {'000001': DF if df is None else df},
                             D[0] if start is None else start,
                             D[-1] if end is None else end,
                             CFGW if cfg is None else cfg)


def _flat(n=80, level=13.0, amp=0.15, period=10.0):
    """走平来回的小波：新判据下收盘反复穿越 MA20、斜率不足 → 全段震荡"""
    return mkdf(closes=[level + amp * np.sin(i * 2 * np.pi / period) for i in range(n)])


def test_chart_bundle_paints_regime_segments_and_flip_points():
    """行情图必须把三态波段（背景色块）与分级切换点一并带出，前端才能直接画 markArea/◆"""
    b = bundle(_flat(), D[0], D[-1])[0]
    # 走平来回 → 永远判为震荡：整段一个波段，没得切换
    assert len(b['areas']) == 1
    a = b['areas'][0]
    assert a['label_cn'] == '震荡' and a['desc'] == regime.DESC_CN[regime.RANGE]
    assert b['flips'] == [] and b['n_flips'] == 0
    assert b['dates'][0] <= a['x0'] and b['dates'][-1] == a['x1']
    assert a['days'] == sum(1 for d in b['dates'] if d >= a['x0'])
    assert a['fill'] and a['color']            # 前端直接用这两个字段画背景
    assert b['n_range'] + b['n_unknown'] == b['bars'] and b['n_bull'] == 0
    assert b['n_unknown'] == 59                      # 慢线 MA60 没就绪的前 59 根单列
    # 副图数据齐备：量能/ADX/MACD 三轨 + 三条均线 + 布林 + %B（只定位不判定）
    for key in ('vol', 'adx', 'atr', 'dif', 'dea', 'hist', 'ma5', 'ma20', 'ma60',
                'boll_up', 'boll_mid', 'boll_low', 'pctb', 'k', 'adx_min'):
        assert key in b, key
    assert b['adx_min'] == CFGW['regime']['adx_min']    # 前端虚线跟参数走，不写死 25
    assert len(b['k']) == len(b['dates'])
    # 交易类字段一个都不许留（图上不再标买卖点）
    for gone in ('buys', 'sells', 'n_trades', 'pnl', 'marks'):
        assert gone not in b, gone
    # 趋势真实切换时：至少两个波段，且从第二个波段起标出切换点（◆ 自带中文文案）
    trend = bundle(SWING_DF, D[0], '2099-12-31')[0]
    assert len(trend['areas']) >= 2 and trend['flips']
    assert {x['label_cn'] for x in trend['areas']} <= {'多头', '空头', '震荡'}
    assert {x['level_cn'] for x in trend['areas']} <= {
        '多头', '空头', '震荡', '弱多头', '弱空头'}
    assert all('n_strong' in x and 0 <= x['n_strong'] <= x['days'] for x in trend['areas'])
    assert trend['flips'][0]['label_cn'] in ('多头', '空头', '震荡')
    assert '→' in trend['flips'][0]['text']
    assert trend['flips'][0]['prev_cn'] in ('多头', '空头', '震荡')
    assert trend['flips'][0]['mark'] in ('🔴🔴', '🟡', '⚪')      # 分级标记直接给前端
    assert trend['flips'][0]['grade'] in ('strong', 'momentum', 'other')
    assert (trend['n_bull'] + trend['n_bear'] + trend['n_range'] +
            trend['n_unknown']) == trend['bars']
    assert trend['n_segments'] == len(trend['areas'])
    assert 0 <= trend['n_strong'] <= trend['bars']              # 强档天数
    assert trend['n_strong_flips'] <= trend['n_flips']


def test_chart_bundle_caps_bars_and_skips_missing_data():
    """窗口只画最多 max_bars 根（更早的进统计不进图）；没有行情数据的标的不产出图表"""
    b = bundle(SWING_DF, D[0], '2099-12-31')[0]
    assert b['bars'] <= web.CHART_BARS
    got = web._chart_bundle(STOCK, {}, D[0], D[-1], CFGW)
    assert got == []
    # 区间完全没数据也不硬出图
    assert web._chart_bundle(STOCK, {'000001': DF}, '1999-01-01', '1999-12-31', CFGW) == []


def test_kline_payload_windows_and_carries_snapshot():
    """详情页K线：窗口内日期/波段/切换点必须对齐，并带今日三态快照"""
    p = web._kline_payload(SWING_DF, CFGW, n=120, code='000001')
    assert len(p['dates']) == 120 and len(p['k']) == 120
    assert p['code'] == '000001' and p['regime']['kind'] == regime.BEAR
    dates = set(p['dates'])
    for a in p['areas']:
        assert a['x0'] in dates and a['x1'] in dates and a['x0'] <= a['x1']
    for f in p['flips']:
        assert f['date'] in dates
        assert f['value'][0] == f['date']
    # NaN 一律转 null，前端才不会出现断点报错
    assert all(x is None or isinstance(x, float) for x in p['ma60'])


def test_regime_segs_match_chart_window():
    win, codes, flags = web._regime_window(SWING_DF, CFGW, 60)
    assert len(win) == 60 and len(codes) == 60 and len(flags) == 60
    segs = web._regime_segs(SWING_DF, CFGW, 60)
    assert segs and segs[0]['start'] >= list(win['date'])[0]
    assert segs[-1]['current'] is True
    assert all('n_strong' in s and 'level_cn' in s for s in segs)
    assert web._regime_segs(mkdf(n=10), CFGW, 60) == []
    # 毛刺过滤真的在图上生效：开 spike_days 后段数不会比不开多
    no_merge = web._regime_segs(SWING_DF, dict(CFGW, regime=dict(CFGW['regime'],
                                                                spike_days=0)), 250)
    merged = web._regime_segs(SWING_DF, CFGW, 250)
    assert len(merged) <= len(no_merge)
    assert sum(s['days'] for s in merged) == sum(s['days'] for s in no_merge)


def test_cfg_from_form_only_touches_regime_keys():
    """表单里的三态参数覆盖到配置：整数键取整、非法值忽略、其它配置不动"""
    got = web._cfg_from_form({'ma_slow': '50', 'slope_min': '0.35',
                              'confirm_days': '4', 'ma_fast': 'x',
                              'capital': '999'}, CFGW)
    r = got['regime']
    assert r['ma_slow'] == 50 and r['confirm_days'] == 4
    assert abs(r['slope_min'] - 0.35) < 1e-9
    assert r['ma_fast'] == CFGW['regime']['ma_fast']    # 非法值不改
    assert 'capital' not in got                          # 交易参数根本不认
    # 空表单 = 原配置
    assert web._cfg_from_form({}, CFGW)['regime'] == CFGW['regime']


# ---------- 设置页保存 ----------
@pytest.fixture
def iso_cfg(tmp_path, monkeypatch):
    """将 config.yaml 指向临时文件，避免污染真实配置"""
    base = dict(cfg_mod.DEFAULT_CONFIG)      # auto_run_time 不盖：默认就是盘后 15:35
    base.update({'auto_run_enabled': True})
    path = tmp_path / 'config.yaml'
    with open(path, 'w', encoding='utf-8') as f:
        yaml.safe_dump(base, f, allow_unicode=True, sort_keys=False)
    monkeypatch.setattr(cfg_mod, 'CONFIG_PATH', str(path))
    # 避免保存时真的拉起后台线程
    monkeypatch.setattr(web.autorun, 'start', lambda cfg=None: False)
    return path


def _read(path):
    with open(path, encoding='utf-8') as f:
        return yaml.safe_load(f)


def test_webhook_only_save_keeps_other_params(iso_cfg):
    """只保存 Webhook 不能顺手把三态与自动运行参数清空（表单带隐藏字段回传）"""
    c = web.app.test_client()
    r = c.post('/settings', data={'regime_present': '1', 'auto_run_present': '1',
                                  'ma_fast': '5', 'ma_slow': '60', 'slope_min': '0.1',
                                  'adx_min': '22', 'confirm_days': '5', 'spike_days': '4',
                                  'auto_run_enabled': 'on', 'auto_run_time': '16:40',
                                  'feishu_webhook': 'http://hook',
                                  'feishu_webhook_secret': 'sec'})
    assert r.status_code in (200, 302)
    saved = _read(iso_cfg)
    assert saved['auto_run_enabled'] is True
    assert saved['auto_run_time'] == '16:40'      # 时刻可改，默认才是 15:35
    assert saved['feishu_webhook'] == 'http://hook'
    assert saved['regime'] == {'ma_fast': 5, 'ma_slow': 60, 'slope_min': 0.1,
                               'adx_min': 22.0, 'confirm_days': 5, 'spike_days': 4}


def test_settings_form_persists_regime_params(iso_cfg):
    """三态参数落盘：整数键取整、斜率留小数；交易键即使被旧页面提交回来也必须丢掉"""
    c = web.app.test_client()
    c.post('/settings', data={'regime_present': '1', 'ma_fast': '8', 'ma_slow': '50',
                              'slope_min': '0.35', 'adx_min': '28', 'confirm_days': '4',
                              'spike_days': '6',
                              'capital': '300000', 'sl_atr': '2', 'tp_atr': '0.5',
                              'min_votes': '2', 'levels': '4', 'grid': '1',
                              'fixed_t_amount': '20000', 'enable_base_filter': 'on'})
    saved = _read(iso_cfg)
    r = saved['regime']
    assert r['ma_fast'] == 8 and r['ma_slow'] == 50 and r['confirm_days'] == 4
    assert abs(r['slope_min'] - 0.35) < 1e-9
    assert r['adx_min'] == 28.0 and r['spike_days'] == 6      # 新判据两项也能落盘
    for gone in cfg_mod.OBSOLETE:
        assert gone not in saved, gone
    page = c.get('/settings').get_data(as_text=True)
    assert 'value="50"' in page and 'value="0.35"' in page      # 保存后回显新值
    assert 'name="adx_min"' in page and 'name="spike_days"' in page
    for gone in ('name="sl_atr"', 'name="tp_atr"', 'name="min_votes"',
                 'name="levels"', 'name="capital"', 'name="grid_step_atr"'):
        assert gone not in page, gone


def test_settings_page_drops_unknown_regime_keys(iso_cfg):
    """嵌套段只认 DEFAULT_CONFIG 已知的键，脏键写不进去"""
    c = web.app.test_client()
    c.post('/settings', data={'regime_present': '1', 'ma_slow': '40'})
    assert set(_read(iso_cfg)['regime']) == set(cfg_mod.DEFAULT_CONFIG['regime'])


def test_backtest_alias_redirects_to_market():
    r = web.app.test_client().get('/backtest')
    assert r.status_code == 302 and '/market' in r.headers['Location']


def test_market_page_get_has_only_recognition_controls(iso_cfg):
    """行情页护栏：只有日期/标的/三态参数，没有交易控件，数据区也没有模式与买卖字样"""
    page = web.app.test_client().get('/market').get_data(as_text=True)
    assert 'name="codes"' in page and 'name="ma_slow"' in page
    assert 'name="slope_min"' in page and 'name="confirm_days"' in page
    assert 'name="adx_min"' in page and 'name="spike_days"' in page
    assert 'name="refresh_data"' in page
    for gone in ('bt_mode', 'fixed_t_amount', 'cost_rate', 'capital', 'sl_atr',
                 'tp_atr', 'min_votes', 'levels', 'grid_step_atr', 'sizing_mode',
                 'enable_base_filter', 'pos_frac'):
        assert gone not in page, gone
    for word in FORBIDDEN:
        assert word not in strip_rules(page), word
    assert page.count('<label title="') >= 6        # 参数必须可悬停看解释


def test_stocks_page_is_code_and_name_only(iso_cfg, tmp_path, monkeypatch):
    path = tmp_path / 'stocklist.yaml'
    with open(path, 'w', encoding='utf-8') as f:
        yaml.safe_dump([{'code': '601318', 'name': '中国平安'},
                        {'code': '600741', 'name': '华域汽车', 'tier': 'mid',
                         'base_shares': 2000}], f, allow_unicode=True, sort_keys=False)
    monkeypatch.setattr(cfg_mod, 'STOCKLIST_PATH', str(path))
    c = web.app.test_client()
    page = c.get('/stocks').get_data(as_text=True)
    for gone in ('tier', 'base_shares', 'base_cost', 'min_votes'):
        assert 'name="%s"' % gone not in page, gone
    assert '买入' not in page and '卖出' not in page
    # 改名与删除都落盘，且写回的清单只剩 code/name
    c.post('/stocks', data={'action': 'rename', 'code': '601318', 'name': '平安'})
    c.post('/stocks', data={'action': 'del', 'code': '600741'})
    assert cfg_mod.load_stocks() == [{'code': '601318', 'name': '平安'}]


# ---------- 模板渲染（含缺数据兜底） ----------
def _snap(**over):
    s = {'date': '2026-10-19', 'kind': regime.BULL, 'label_cn': '多头',
         'strong': True, 'level_cn': '多头', 'adx': 31.0, 'adx_min': 25.0,
         'desc': regime.DESC_CN[regime.BULL], 'color': regime.COLOR[regime.BULL],
         'fill': regime.FILL[regime.BULL], 'days': 7, 'changed': True,
         'prev': regime.RANGE, 'prev_label_cn': '震荡',
         'grade': regime.flip_grade(regime.RANGE, regime.BULL),
         'pending': regime.BEAR,
         'pending_label_cn': '空头', 'pending_days': 2, 'confirm_days': 5,
         'slope_min': 0.3, 'spike_days': 3, 'ma_fast': 5, 'ma_slow': 60,
         'ma': {'fast': 15.1, 'mid': 15.2, 'slow': 14.6, 'slope5': 0.42},
         'segment': {'start': '2026-10-13', 'end': '2026-10-19', 'days': 7,
                     'chg_pct': 3.2, 'hi': 15.9, 'lo': 14.8, 'kind': regime.BULL,
                     'label_cn': '多头', 'level_cn': '多头', 'n_strong': 5,
                     'weak_days': 2, 'desc': regime.DESC_CN[regime.BULL],
                     'color': regime.COLOR[regime.BULL], 'current': True},
         'n_segments': 3}
    s.update(over)
    return s


def test_index_template_eats_missing_metrics():
    """仪表盘必须能吃下“数据失败 / 指标为 None / 无三态”的行：缺数据显示 '-'，不抛 TypeError"""
    results = [
        {'code': '600741', 'name': '华域汽车', 'status': '正常', 'reason': '',
         'date': '2026-10-19', 'close': 15.5, 'chg_pct': 0.8, 'adx': 28.0,
         'ind': {'ma20': 15.2, 'ma60': 14.6, 'slope5': 0.42, 'adx': 28.0, 'rsi': 55.0,
                 'bb_width': 8.0, 'amp20': 1.8, 'vol_ratio': 1.2, 'turnover': 0.6,
                 'boll_low': 14.4, 'boll_up': 16.0},
         'regime': _snap()},
        {'code': '601318', 'name': '中国平安', 'status': '数据失败', 'reason': '下载超时',
         'date': None, 'close': None, 'chg_pct': None, 'adx': None, 'ind': None,
         'regime': None},
        {'code': '600887', 'name': '伊利股份', 'status': '均线未就绪', 'reason': '慢线还没算出来',
         'date': '2026-10-19', 'close': 27.7, 'chg_pct': None, 'adx': None,
         'ind': {'bb_width': None, 'vol_ratio': None, 'slope5': None},
         'regime': _snap(kind=regime.UNKNOWN, label_cn='数据不足',
                         desc=regime.DESC_CN[regime.UNKNOWN])},
    ]
    trend = {'rows': [], 'changed': [], 'strong_changed': [], 'momentum_changed': [],
             'pending': [], 'quiet': [],
             'n_bull': 1, 'n_bear': 0, 'n_range': 0, 'n_rows': 1, 'n_strong_rows': 1,
             'date': '2026-10-19'}
    with web.app.test_request_context('/'):
        page = web.render_template('index.html', results=results, cfg=CFGW, trend=trend)
    assert '数据失败' in page and '下载超时' in page
    assert '多头' in page and '2026-10-13' in page and '第 <b>7</b> 日' in page
    assert '今日切换：震荡 → 多头' in page and '酝酿空头 2/5 日' in page
    assert 'ADX' in page and '区间位置 %B' in page      # 强档门槛与震荡定位都得上页
    assert '带宽 -%' in page and '-×' in page
    assert page.count('<col style=') == page.count('<th>') == 11
    assert RULES_HEAD in page                        # 需求：判定规则与运行机制要在页面标明
    for word in FORBIDDEN:
        assert word not in strip_rules(page), word
    assert '不产生任何买卖指令' in page            # 页面自己把边界说清楚


def test_detail_template_renders_wave_table_without_signals():
    """详情页：今日趋势 + 图上波段明细 + 因子快照，不含任何信号/网格/漏斗卡片"""
    segs = web._regime_segs(SWING_DF, CFGW, 250)
    ctx = {'st': {'code': '000001', 'name': '测试股'},
           'df': SWING_DF.tail(160).reset_index(drop=True),
           'ind': ind.snapshot(SWING_DF.iloc[-1]),
           'rgsnap': regime.snapshot(SWING_DF, CFGW),
           'rsegs': segs,
           'k_json': '{"dates": [], "k": []}'}
    with web.app.test_request_context('/stock/000001'):
        page = web.render_template('detail.html', **ctx)
    assert '三态 ·' in page and '本波段第' in page
    assert '图上波段明细' in page and '今日趋势' in page
    assert '近20日明细' in page and '今日盘面快照' in page
    assert 'renderKChart' in page
    for gone in ('闸门漏斗', '网格档位', '买点提醒', '投票', '明日动作', '适用模式'):
        assert gone not in page, gone
    for word in FORBIDDEN:
        assert word not in strip_rules(page), word
    assert RULES_HEAD in page and '段内强日' in page
    assert 'ADX(14)' in page and '区间位置 %B' in page
    assert page.count('<colgroup>') == 2             # 图上波段表 + 近20日表


def test_market_template_renders_charts_and_stats():
    """行情页渲染：区间天数 + 波段统计 + 每票K线容器 + 明细表"""
    charts = web._chart_bundle(STOCK, {'000001': SWING_DF}, D[0], '2099-12-31', CFGW)
    stats = scan_mod.range_regime_days(STOCK, {'000001': SWING_DF}, CFGW,
                                       D[0], '2099-12-31')
    with web.app.test_request_context('/market', method='POST'):
        page = web.render_template('market.html', stocks=STOCK, form={}, cfg=CFGW,
                                   charts=charts, stats=stats, sel_codes={'000001'},
                                   start=D[0], end='2099-12-31', rg=CFGW['regime'])
    assert 'id="k-000001"' in page and 'mkt-data' in page
    assert '区间三态构成' in page and '波段性格统计' in page and '波段明细' in page
    assert '多头天' in page and '震荡占比' in page
    assert '强档天' in page and '段内强日' in page      # 强/弱两档在区间统计里可见
    assert RULES_HEAD in page
    for word in FORBIDDEN:
        assert word not in strip_rules(page), word
    assert page.count('<colgroup>') == page.count('<table class="fit">')


def test_rules_block_is_stated_with_current_params(iso_cfg):
    """需求：“判定规则与运行机制”要在页面标明，且写的就是当前生效参数（不能与引擎口径漂移）"""
    c = web.app.test_client()
    for path in ('/settings', '/market'):
        page = c.get(path).get_data(as_text=True)
        assert RULES_HEAD in page, path
        rg = CFGW['regime']
        assert ('ADX(14)' in page and 'MA' + str(rg['ma_slow']) in page and
                str(rg['slope_min']) in page and str(int(rg['adx_min'])) in page), path
        assert ('%d 日' % rg['spike_days']) in page and ('%d 个交易日' % rg['confirm_days']) in page
        assert '🔴🔴' in page and '🟡' in page          # 切换分级两档都列出
        assert '给定' in page and '+4.55%' in page, path     # 需求给定的历史参考也要写明（无行情时走退化分支）
        assert '震荡做T的第一纪律' in page and '趋势段停手' in page
        assert '15:35' in page                      # 盘后自动重跑时刻
        assert 'BOLL(20,2) 不参与三态判定' in page


# ---------- 需求文档渲染 ----------
def test_md_html_renders_doc_blocks():
    h = web._md_html(u"# 标题\n\n## 口径\n- 第一点 **重点**\n- 第二点 `code`\n\n"
                     u"| a | b |\n|---|---|\n| 1 | 2 |\n\n> 提示\n\n```\nx: 1\n```\n\n---\n")
    assert '<h2>标题</h2>' in h                      # 层级下移一级
    assert '<li>第一点 <b>重点</b></li>' in h
    assert '<code>code</code>' in h
    assert '<table class="mdtab">' in h and '<th>a</th>' in h and '<td>1</td>' in h
    assert '<blockquote>提示</blockquote>' in h
    assert '<pre class="mdcode">x: 1</pre>' in h
    assert h.count('<hr>') == 1
    assert '|' not in h.split('<table')[0]           # 表头分隔行不应漏到正文


def test_md_html_escapes_and_survives_real_doc():
    assert web._md_html(u'<script>alert(1)</script>') == \
        '<p>&lt;script&gt;alert(1)&lt;/script&gt;</p>'
    assert web._md_html('') == ''
    c = web.app.test_client()
    page = c.get('/doc').get_data(as_text=True)
    assert '需求文档读取失败' not in page               # 真文件存在
    assert '<table class="mdtab">' in page
    assert web.DOC_PATH.endswith('需求文档.md')
    # 渲染区不允许出现“没被转成 <b> 的星号”：跨行加粗必须能渲染（原文区带 ** 正常）
    rendered = web._md_html(open(web.DOC_PATH, encoding='utf-8').read())
    assert '**' not in rendered


def test_md_html_joins_wrapped_list_items():
    """条目折行不能断成两个 <li>，段落里跨行的 **加粗** 也不能露出星号"""
    got = web._md_html('- **甲**：第一行，\n  第二行接上去\n- 乙')
    assert got.count('<li>') == 2 and '**' not in got and '<b>甲</b>' in got
    assert '第二行接上去' in got.split('<li>')[1]
    # 缩进表格属于块，不能被当成续行吞掉
    got2 = web._md_html('1. 说明：\n\n   | a | b |\n   |---|---|\n   | 1 | 2 |')
    assert '<table class="mdtab">' in got2 and '<b>' not in got2


def test_num_chg_filters():
    """指标过滤器：缺数据/NaN 都给 '-'，模板不会把 nan 印到页面上"""
    assert web.num_filter(None) == '-' and web.num_filter(15.5) == '15.50'
    assert web.num_filter(float('nan')) == '-'
    assert web.num_filter(np.nan, '%.1f') == '-'
    assert web.num_filter(28.3, '%.1f') == '28.3'
    assert web.num_filter('2026-10-19') == '2026-10-19'
    assert web.chg_filter(1.23) == '+1.23%' and web.chg_filter(-0.4) == '-0.40%'
    assert web.chg_filter(None) == '-'
    assert not hasattr(web, 'pf_filter')             # 盈亏比过滤器已随交易层删除


# ---------- 模板可编译 / 各页面可打开 ----------
def _tpl_dir():
    return os.path.join(os.path.dirname(os.path.abspath(web.__file__)), 'templates')


def test_every_template_compiles():
    """模板里少打一个右花括号就整页 500，而单页断言只看得到走过的分支：
    这里把 templates/ 下所有模板逐个编译一遍（不渲染）兜底。"""
    td = _tpl_dir()
    names = sorted(f for f in os.listdir(td) if f.endswith('.html'))
    assert 'backtest.html' not in names and 'optimize.html' not in names
    assert 'market.html' in names
    for n in names:
        with open(os.path.join(td, n), encoding='utf-8') as f:
            web.app.jinja_env.from_string(f.read())


def test_nav_has_no_trading_pages():
    page = web.app.test_client().get('/settings').get_data(as_text=True)
    assert 'href="/market"' in page and 'href="/optimize"' not in page
    assert 'href="/backtest"' not in page
    data_only = strip_rules(page)
    for word in ('买入', '卖出', '模式B', '模式C', '做T'):
        assert word not in data_only, word


@pytest.mark.parametrize('path', ['/settings', '/stocks', '/market', '/doc'])
def test_get_pages_render(iso_cfg, path):
    c = web.app.test_client()
    r = c.get(path)
    assert r.status_code == 200, (path, r.get_data(as_text=True)[:300])
