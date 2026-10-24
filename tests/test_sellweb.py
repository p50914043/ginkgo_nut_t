# -*- coding: utf-8 -*-
"""卖出通知工具 Web 层测试：路由渲染 / 因子落盘 / 清单增删改 / dry 发送记录（全部离线，不触网、不真推送）"""
import sys
sys.path.insert(0, 'libs')
sys.path.insert(0, '.')

import pytest
import yaml

import app as web
from core import config as cfg_mod
from core import sellscan
from core import notifier


def _rep(**over):
    """scan_sell 结果的形状（手工搭，不拉行情）"""
    row = {'code': '600585', 'name': '海螺水泥', 'type': 'cycle',
           'type_cn': '周期', 'industry': '水泥', 'error': '',
           'quote': {'date': '2026-10-22', 'close': 24.1, 'chg_pct': -1.2},
           'level': 'yellow', 'state': 'yellow', 'verified': True,
           'a': {'level': 'none', 'status': 'ok', 'metric': 'PB', 'value': 0.8,
                 'pct': 30.0, 'years': 10, 'n_window': 2000, 'dy': 4.2,
                 'pe': {'val': 12.3, 'pct': 55.0, 'years': 5},
                 'pb': {'val': 0.8, 'pct': 30.0, 'years': 10},
                 'detail': 'PB 0.800，近 10 年 30.0% 分位'},
           'b': {'level': 'yellow', 'status': 'ok', 'n_groups': 1, 'groups': [],
                 'latest_report': '2026-06-30', 'detail': '营收连续2期负增长'},
           'c': {'level': 'none', 'status': 'ok', 'dd': 5.0, 'dd_ok': False, 'dd_thr': 20.0,
                 'index': '1.000300', 'n_peers': 0, 'peers_ok': False, 'detail': '指数回撤 5.0%'},
           'd': {'level': 'none', 'status': 'ok', 'n_inst': 0, 'hits': [],
                 'prev_end': '2026-03-31', 'cur_end': '2026-06-30', 'detail': '无机构减持'}}
    r = {'disabled': False, 'date': '2026-10-22', 'rows': [row], 'cards': [row],
         'alerts': [], 'n_yellow': 0, 'n_red': 0,
         'n_monitor': 1, 'n_notrig': 0, 'n_pending': 0,
         'cfg': dict(cfg_mod.DEFAULT_CONFIG),
         'stocks': [{'code': '600585', 'name': '海螺水泥', 'type': 'cycle', 'enabled': True}]}
    r.update(over)
    return r


@pytest.fixture
def sell_env(tmp_path, monkeypatch):
    """config.yaml / stocklist.yaml / sell_log.jsonl 全指向临时文件；扫描与推送都 mock 掉（卖出预警复用主清单）"""
    base = dict(cfg_mod.DEFAULT_CONFIG)
    cpath = tmp_path / 'config.yaml'
    with open(cpath, 'w', encoding='utf-8') as f:
        yaml.safe_dump(base, f, allow_unicode=True, sort_keys=False)
    lpath = tmp_path / 'stocklist.yaml'
    with open(lpath, 'w', encoding='utf-8') as f:
        yaml.safe_dump([{'code': '600585', 'name': '海螺水泥', 'type': 'cycle',
                         'index': '', 'enabled': True},
                        {'code': '601318', 'name': '中国平安', 'type': 'insurance',
                         'index': '1.000300', 'enabled': True}], f,
                       allow_unicode=True, sort_keys=False)
    monkeypatch.setattr(cfg_mod, 'CONFIG_PATH', str(cpath))
    monkeypatch.setattr(cfg_mod, 'STOCKLIST_PATH', str(lpath))
    monkeypatch.setattr(sellscan, 'LOG_PATH', str(tmp_path / 'sell_log.jsonl'))
    monkeypatch.setattr(web.autorun, 'start', lambda cfg=None: False)
    return cpath, lpath


def test_sell_nav_present_and_pages_render(sell_env, monkeypatch):
    """卖出预警入口在导航里；主页面（含可折叠因子配置）与清单/回测页都能离线渲染出关键结构"""
    monkeypatch.setattr(sellscan, 'scan_sell', lambda cfg=None, refresh=False: _rep())
    monkeypatch.setattr(sellscan, 'backtest', lambda cfg=None, codes=None, years=None:
                        {'events': [], 'summary': [{'sig': 'A', 'sig_cn': 'A 估值过高', 'n': 0,
                                                    'avg5': None, 'avg20': None, 'drop_pct': None}],
                         'start': '2023-10-22', 'years': 3, 'stocks': [], 'n_all': 0,
                         'avg20_all': None})
    c = web.app.test_client()
    base = c.get('/').get_data(as_text=True)
    assert '卖出预警' in base and 'href="/sell"' in base
    ov = c.get('/sell').get_data(as_text=True)
    assert '卖出信号监控' in ov and '海螺水泥' in ov
    assert 'A 估值' in ov and 'B 基本面' in ov and 'C 行业与宏观' in ov and 'D 社保' in ov
    # 逐票卡片按 b=yellow 显示🟡
    assert '🟡' in ov
    # 因子配置已折叠在主页面内
    assert 'name="a_yellow"' in ov and 'name="b_ocf_ratio"' in ov and 'name="c_dd_pct"' in ov
    assert 'name="sell_present"' in ov
    # /sell/factors 不再是独立页，GET 跳回总览
    assert c.get('/sell/factors').status_code == 302
    # 股票清单已统一到 /stocks（卖出预警直接读主清单，不再有 /sell/stocks）
    assert c.get('/sell/stocks').status_code == 404
    st = c.get('/stocks').get_data(as_text=True)
    assert 'value="update_all"' in st
    bt = c.get('/sell/backtest').get_data(as_text=True)
    assert '历史回测' in bt and 'name="years"' in bt


def test_sell_page_trading_terms_guard(sell_env, monkeypatch):
    """卖出预警是风险管理口径，页面不得混入旧交易话术（网格/止盈/加减仓/下单指令等）"""
    monkeypatch.setattr(sellscan, 'scan_sell', lambda cfg=None, refresh=False: _rep())
    page = web.app.test_client().get('/sell').get_data(as_text=True)
    for word in ('网格', '止盈', '低吸', '高抛', '加仓', '减仓', '满仓', '下单指令', '目标价'):
        assert word not in page, word


def test_sell_factors_persist_and_gate(sell_env):
    """带 sell_present 才写 sell_notify：数值取对类型、复选框缺席即 False；不带标记不动配置"""
    c = web.app.test_client()
    r = c.post('/sell/factors', data={'sell_present': '1', 'sell_enabled': 'on',
                                      'a_yellow': '75', 'a_red': '85', 'a_years_pe': '6',
                                      'b_ocf_ratio': '0.6', 'c_dd_pct': '25',
                                      'c_warn_peers': '3', 'bt_years': '5', 'sell_d_on': 'on'})
    assert r.status_code == 302
    sn = cfg_mod.load_config()['sell_notify']
    assert sn['enabled'] is True and sn['d_on'] is True
    assert sn['a_yellow'] == 75.0 and sn['a_years_pe'] == 6 and sn['c_warn_peers'] == 3
    assert sn['bt_years'] == 5
    # 只提交 sell_enabled 不带 → enabled False；未列出的键保持原值
    c.post('/sell/factors', data={'sell_present': '1', 'a_yellow': '70'})
    sn2 = cfg_mod.load_config()['sell_notify']
    assert sn2['enabled'] is False and sn2['a_yellow'] == 70.0 and sn2['a_red'] == 85.0
    # 不带 sell_present 的表单不能洗掉 sell_notify
    c.post('/sell/factors', data={'a_yellow': '99'})
    assert cfg_mod.load_config()['sell_notify']['a_yellow'] == 70.0


def test_sell_stocks_add_update_all_del(sell_env):
    """卖出预警复用主清单：/stocks 整表维护 type/index/enabled，scan_sell 直接读 load_stocks()"""
    c = web.app.test_client()
    # 添加（带交易所前缀也能归一化）
    c.post('/stocks', data={'action': 'add', 'code': 'sh.600000', 'name': '浦发银行',
                            'type': 'bank', 'index': '1.000300'})
    stocks = cfg_mod.load_stocks()
    assert any(s['code'] == '600000' and s['type'] == 'bank' for s in stocks)
    # update_all：一次提交整表，改类型/指数、把某只取消纳入
    c.post('/stocks', data={'action': 'update_all',
                            'type_600000': 'value', 'index_600000': '',
                            'name_600000': '浦发',
                            'type_600585': 'cycle', 'index_600585': '0.399997'})
    stocks = {s['code']: s for s in cfg_mod.load_stocks()}
    assert stocks['600000']['type'] == 'value' and stocks['600000']['name'] == '浦发'
    # 未勾选 enabled_600000 → 关闭；600585 同样没勾 → 关闭
    assert stocks['600000']['enabled'] is False and stocks['600585']['enabled'] is False
    assert stocks['600585']['index'] == '0.399997'
    # 删除
    c.post('/stocks', data={'action': 'del', 'code': '600000'})
    assert not any(s['code'] == '600000' for s in cfg_mod.load_stocks())
    # 非法类型写不进去（回落 value）
    c.post('/stocks', data={'action': 'add', 'code': '600001', 'name': 'x', 'type': 'hedge'})
    assert next(s for s in cfg_mod.load_stocks() if s['code'] == '600001')['type'] == 'value'


def test_sell_send_dry_writes_log_no_push(sell_env, monkeypatch):
    """dry=1 只记预览、绝不调用真实推送；发送记录回读展示在总览页"""
    calls = []
    monkeypatch.setattr(sellscan, 'scan_sell', lambda cfg=None, refresh=False: _rep())
    monkeypatch.setattr(web, 'notify_push',
                        lambda title, lines, cfg, dry_run=False: (
                            calls.append(dry_run) or {'code': 0, 'msg': 'ok',
                                                      'channel': 'webhook'}))
    c = web.app.test_client()
    r = c.post('/sell/send', data={'dry': '1'})
    assert r.status_code == 302
    assert calls == [True]                        # 只有 dry 通道被调用
    logs = sellscan.read_log(5)
    assert len(logs) == 1 and logs[0]['dry'] is True and logs[0]['ok'] is True
    page = c.get('/sell').get_data(as_text=True)
    assert '最近发送记录' in page


def test_sell_page_disabled_hint(sell_env, monkeypatch):
    """工具关闭时总览页给提示而不是空表崩溃"""
    rep = _rep(disabled=True, rows=[], cards=[], alerts=[], n_red=0, n_yellow=0,
               n_monitor=0, n_notrig=0, n_pending=0, date='')
    monkeypatch.setattr(sellscan, 'scan_sell', lambda cfg=None, refresh=False: rep)
    page = web.app.test_client().get('/sell').get_data(as_text=True)
    assert '关闭' in page


def test_format_sell_message_lists_each_hit_signal():
    """有预警时逐只逐信号列出（锁定 SIG_CN 大小写键回归：'a' 小写行键 → 'A 估值过高' 标签）"""
    row = _rep()['rows'][0]
    row['a']['level'] = 'yellow'
    rep = _rep(alerts=[row], n_yellow=1, n_red=0)
    msg = notifier.format_sell_message('2026-10-22', rep)
    assert msg[0].startswith('2026-10-22 卖出预警')
    assert any('A 估值过高' in ln for ln in msg)
    assert any('B 基本面恶化' in ln for ln in msg)
    assert any('海螺水泥' in ln for ln in msg)
    assert '--- 数据截至' in msg[-1]
    # disabled 走单行提示
    assert notifier.format_sell_message('2026-10-22', {'disabled': True})[0].find('关闭') > 0
