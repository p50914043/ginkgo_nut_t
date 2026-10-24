# -*- coding: utf-8 -*-
"""日线趋势识别工具 Web入口

判定层（core/regime.py，均线 + ADX）只有一层：多头 / 空头 / 震荡（内含强/弱两档）。
页面与推送做三件事：
  网页仪表盘与飞书播报：每天盘后给出当前三态、本波段第几天、是否发生趋势变化（按 🔴🔴/🟡 分级）
  日K图：背景色块标出三态波段，◆ 标出趋势切换的那天
  提示层（core/tips.py）：震荡段给做T纪律提示（%B 上下沿 / 硬止损 / 最长持有），
  多头段给“回踩 MA20 + 缩量止跌”介入提示并标最高风险；趋势段停手。
依旧不算收益与胜率、不给仓位金额、不对接任何下单接口。
"""
import sys
sys.path.insert(0, 'libs')

import json
import os
import re as _re

import jinja2
from html import escape as _esc

import numpy as np
from flask import Flask, render_template, request, redirect, url_for, jsonify, flash

from core import data, config, regime, scan as scan_mod, tips
from core import autorun
from core import sellscan, selldata
from core.indicators import compute_factors, snapshot as ind_snapshot
from core.notifier import (push as notify_push, format_trend_message, describe_channel,
                           format_sell_message, SELL_TITLE)
from core import notifier

app = Flask(__name__)
app.secret_key = 'ginkgo-nut-t'
DOC_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '需求文档.md')

# 图表口径：详情页窗口多少根、行情页最多画几只（行情页 K 线根数 = 所选 start~end 区间，不截断）
DETAIL_BARS = 250
MAX_CHARTS = 9

# 三态参数（斜率与 ADX 门槛是浮点，周期与天数取整）
REGIME_NUM_KEYS = ('ma_fast', 'ma_slow', 'slope_min', 'confirm_days', 'adx_min', 'spike_days')
REGIME_INT_KEYS = ('ma_fast', 'ma_slow', 'confirm_days', 'spike_days')
# 提示层参数（%B 上下沿与回踩容差是浮点，持有日数取整）
TIP_NUM_KEYS = ('buy_pctb', 'sell_pctb', 'stop_pct', 'max_hold', 'pb_touch', 'vol_shrink')
TIP_INT_KEYS = ('max_hold',)
# 卖出通知工具的分位与阈值参数
SELL_NUM_KEYS = ('a_yellow', 'a_red', 'b_ocf_ratio', 'b_debt_up', 'b_growth_gap',
                 'b_div_drop', 'c_dd_pct')
SELL_INT_KEYS = ('a_years_pe', 'a_years_pb', 'b_roe_years', 'b_red_groups',
                 'c_dd_days', 'c_warn_days', 'c_warn_peers', 'bt_years')


def _form_section(form, keys, int_keys, base):
    """表单里的一小段数值参数：空值与非法值都不改原配置，整数键取整"""
    out = dict(base or {})
    for k in keys:
        if form.get(k) in (None, ''):
            continue
        try:
            v = float(form[k])
        except (TypeError, ValueError):
            continue
        out[k] = int(v) if k in int_keys else v
    return out

# 参数悬停解释（设置页/行情页共用，模板里写 title="{{ ph.get('键名') }}"）
PARAM_HELP = {
    'ma_fast': '快线周期：只在图上画出来做辅助参考，不参与三态判据（判据看收盘价/MA20/慢线/斜率/ADX）。',
    'ma_slow': '慢线周期：MA20 越过它（同向斜率 + 足量 ADX）才计入“强”趋势；不越过则只能算弱档。改它等于换一套中期结构定义。',
    'slope_min': 'MA20 的 5 日斜率阈值（单位 %，取绝对值）：斜率的正负决定算多头还是空头，大小只决定强/弱——达不到它仍算弱多/弱空，不会变震荡。调大→强档更少。',
    'confirm_days': '滞回确认天数：新状态要连续成立这么多天才真正切换，防止一天一改。凑满之前叫“酝酿中”。',
    'adx_min': 'ADX(14) 动能门槛：结构、方向都对了，ADX 还要超过它才计“强”；不超过则只算弱多/弱空（三态不变）。',
    'spike_days': '毛刺过滤天数：持续不足这么多日的片段并入相邻主状态，不成波段、不参与提醒（避免标注闪烁）。',
    'buy_pctb': '震荡段买入提示门槛：%B ≤ 这个值（靠布林下轨）才给“买入提示”。只在生效三态为震荡时适用。',
    'sell_pctb': '震荡段卖出提示门槛：持仓中 %B ≥ 这个值（靠布林上轨）就给“卖出提示”。',
    'stop_pct': '硬止损%：自提示买入当日收盘往下跌这么多就提示止损，不看当时市场情绪。',
    'max_hold': '最长持有交易日数：到天数还没到上沿，也提示离场（不让区间单变成长线）。',
    'pb_touch': '多头回踩容差%：当日最低价距 MA20 在这么近以内算“踩到均线”。调小→要贴得更紧才提示。',
    'vol_shrink': '缩量门槛：当日量比（成交量/20日均量）低于这个倍数才算缩量。调大→对“缩量”要求更严。',
    'data_start': '本地行情数据的起始日期（只读；改动后需重新拉数据）。',
    'auto_run_enabled': '工作日到点自动“更新数据→三态扫描→飞书播报”一次；需保持本网页服务（run.sh）在跑。',
    'auto_run_time': '盘后触发时刻（HH:MM，24 小时制）。建议晚于数据源出日线的时间，否则会判定非交易日而跳过。',
    'auto_run_retry': '当天第一次跑到时发现最新数据不是今天（判定非交易日）后，是否在本轮继续重试。',
    'notify_channel': '推送通道：auto 优先用应用机器人（可私聊），退回群机器人 Webhook。',
    'feishu_webhook': '飞书群机器人 Webhook 地址（自定义机器人）。',
    'feishu_webhook_secret': '群机器人“加签”密钥；机器人未启用签名则留空。',
    'feishu_app_id': '企业自建应用 App ID（用应用机器人私聊直达时才需要）。',
    'feishu_app_secret': '企业自建应用 App Secret，明文存在 config.yaml，勿提交公开仓库。',
    'feishu_receive_id': '消息接收者 ID：个人 open_id(ou_xxx) 或群 chat_id(oc_xxx)。',
    'feishu_receive_id_type': '接收者 ID 的类型，需与 feishu_receive_id 对应。',
    # ---- 卖出通知工具（/sell 因子配置页）----
    'sell_enabled': '总开关：关闭后 /sell 页不再扫描，推送提示“无内容可播”；不影响趋势识别主功能。',
    'a_yellow': '信号 A 黄色预警线：估值在回看窗口内的历史分位 ≥ 此值（%）提示黄色。分位 = 窗口内低于当前值的交易日占比。',
    'a_red': '信号 A 红色预警线：历史分位 ≥ 此值（%）升为红色。建议高于黄线，两线相等时黄线失效。',
    'a_years_pe': 'PE / PB（代理PEV）分位回看年数：窗口越长，“高估”的参照系越旧。',
    'a_years_pb': 'PB 分位回看年数：银行/周期股用 10 年窗口，避开单一牛市高点干扰。',
    'b_ocf_ratio': '经营现金流/净利润 阈值：连续 2 个报告期低于它，说明利润里没有现金，计入第1组命中。净利润≤0 的期间不计入连续判定。',
    'b_debt_up': '资产负债率同比升幅阈值（百分点）：最新报告期较上年同期升超它，计入第2组。',
    'b_growth_gap': '应收+存货平均增速超营收增速的Gap（百分点）：扩大它→容忍度更高，只抓最悬殊的膨胀。',
    'b_roe_years': 'ROE 连降判定用的年报份数：3 = 最近三个年报逐级下降且低于行业均值（清单内同行业平均，样本有限）。',
    'b_div_drop': '分红降幅阈值（%）：最近一个已披露完整财年对比上一个有分红年度，总额降超它或取消→计入第3组。只比完整财年，避免中期分红被错当陡降。',
    'b_red_groups': 'B 信号升红色的命中组数：默认 3（三组全中才红），任一组命中即黄色。',
    'c_dd_pct': '行业指数回撤阈值（%）：自回看窗口内最高点回撤超它，条件①成立。',
    'c_dd_days': '“近期高点”的回看交易日数：默认 250 ≈ 一年。',
    'c_warn_days': '业绩预警公告的回看自然日数：只算这段时间内发过的预警。',
    'c_warn_peers': '“同行多家”的家数门槛：清单内同行业（不含本票）发预亏/预减类预警的公司数 ≥ 它，条件②成立。两条件同时成立才黄色预警。',
    'd_on': '信号 D 开关：比较最近两个季度末的十大流通股东，社保/保险机构减持或退出十大数据 → 黄色。仅覆盖十大，未进十大的机构变动看不到。',
    'bt_years': '历史回测默认年数：回测只复盘“预警当时是否成立”，附事件后 5/10/20 个交易日涨跌作参考，不是收益承诺。',
    'sell_index': '行业指数代码：沪市/中证系列写 1.000300（或 sh.000300），深市/国证写 399xxx；留空默认沪深300。同行按东财行业板块名匹配，只统计监控清单内的公司。',
}

@app.template_filter('num')
def num_filter(v, fmt='%.2f'):
    """指标统一输出：缺数据/缺键/NaN 都给 '-'，不让模板因 None、Undefined 报 TypeError"""
    if v is None or isinstance(v, jinja2.Undefined):
        return '-'
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if f != f or f in (float('inf'), float('-inf')):   # NaN / Inf：均线未就绪的那些天
        return '-'
    try:
        return fmt % f
    except (TypeError, ValueError):
        return str(v)


@app.template_filter('chg')
def chg_filter(v):
    """涨跌幅统一带符号输出（缺数据给 '-'）"""
    if v is None or isinstance(v, jinja2.Undefined):
        return '-'
    try:
        return '%+.2f%%' % v
    except (TypeError, ValueError):
        return str(v)


@app.context_processor
def inject_cfg():
    c = config.load_config()
    return {'cfg': c, 'notifier_desc': describe_channel(c),
            'autorun_st': autorun.status(c), 'ph': PARAM_HELP,
            'regime_cn': regime.LABEL_CN, 'regime_color': regime.COLOR,
            'regime_desc': regime.DESC_CN, 'flip_ref': regime.FLIP_STATS}


@app.route('/')
def dashboard():
    force = request.args.get('refresh') == '1'
    out = scan_mod.scan(refresh=force)
    return render_template('index.html', results=out['results'],
                           trend=scan_mod.build_trend_report(out),
                           fstats=scan_mod.flip_stats(out['stocks'], out['dfs'], out['cfg']),
                           cfg=out['cfg'], rg=(out['cfg'].get('regime') or {}),
                           tt=(out['cfg'].get('t_trade') or {}))


@app.route('/stocks', methods=['GET', 'POST'])
def stocks_page():
    stocks = config.load_stocks()
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'add':
            code = (request.form.get('code') or '').strip()
            try:
                mk, num = data.to_secid(code).split('.')
                code = num
                name = (request.form.get('name') or '').strip() or data.get_name(num)
                stype = request.form.get('type') if request.form.get('type') in config.SELL_TYPES else 'value'
                stocks.append({'code': code, 'name': name, 'type': stype,
                               'index': (request.form.get('index') or '').strip(),
                               'enabled': request.form.get('enabled') == 'on'})
                config.save_stocks(stocks)
                flash('已添加 %s %s' % (code, name))
            except Exception as e:
                flash('添加失败: %s' % e)
        elif action == 'del':
            code = request.form.get('code')
            stocks = [s for s in stocks if s['code'] != code]
            config.save_stocks(stocks)
            flash('已删除 %s' % code)
        elif action == 'rename':
            code = request.form.get('code')
            name = (request.form.get('name') or '').strip()
            for s in stocks:
                if s['code'] == code and name:
                    s['name'] = name
            config.save_stocks(stocks)
            flash('已更新 %s' % code)
        elif action == 'update_all':
            # 整表一次性提交（name_/type_/index_/enabled_ 按 code 命名，勾选框缺席即不纳入卖出预警）
            for s in stocks:
                c = s['code']
                nm = (request.form.get('name_' + c) or '').strip()
                if nm:
                    s['name'] = nm
                if request.form.get('type_' + c) in config.SELL_TYPES:
                    s['type'] = request.form.get('type_' + c)
                s['index'] = (request.form.get('index_' + c) or '').strip()
                s['enabled'] = request.form.get('enabled_' + c) == 'on'
            config.save_stocks(stocks)
            flash('清单修改已保存')
        return redirect(url_for('stocks_page'))
    return render_template('stocks.html', stocks=stocks,
                           types=list(config.SELL_TYPES), type_cn=sellscan.TYPE_CN)


@app.route('/settings', methods=['GET', 'POST'])
def settings_page():
    cfg = config.load_config()
    if request.method == 'POST':
        if 'regime_present' in request.form:      # 只重置三态段，避免误清其它配置
            cfg['regime'] = _form_section(request.form, REGIME_NUM_KEYS, REGIME_INT_KEYS,
                                          cfg.get('regime'))
        if 't_trade_present' in request.form:     # 提示层参数（含开关）
            tt = _form_section(request.form, TIP_NUM_KEYS, TIP_INT_KEYS, cfg.get('t_trade'))
            tt['enabled'] = request.form.get('t_trade_enabled') == 'on'
            cfg['t_trade'] = tt
        if 'auto_run_present' in request.form:
            cfg['auto_run_enabled'] = request.form.get('auto_run_enabled') == 'on'
            cfg['auto_run_retry'] = request.form.get('auto_run_retry') == 'on'
            t = (request.form.get('auto_run_time') or '').strip()
            if autorun.parse_hhmm(t):
                cfg['auto_run_time'] = t
        for k in ('feishu_webhook', 'feishu_webhook_secret', 'feishu_app_id',
                  'feishu_app_secret', 'feishu_receive_id', 'notify_channel',
                  'feishu_receive_id_type'):
            v = request.form.get(k)
            if v is not None:
                cfg[k] = v.strip()
        config.save_config(cfg)
        autorun.start(cfg)          # 开关改动立即生效
        flash('参数已保存')
        return redirect(request.form.get('next') or url_for('settings_page'))
    return render_template('settings.html', cfg=cfg, rg=cfg.get('regime') or {},
                           tt=cfg.get('t_trade') or {})


@app.route('/stock/<code>')
def stock_detail(code):
    cfg = config.load_config()
    st = next((s for s in config.load_stocks() if s['code'] == code),
              {'code': code, 'name': data.get_name(code)})
    df = data.get_kline(code, cfg['data_start'])
    full = compute_factors(df)
    fdf = full.tail(160).reset_index(drop=True)
    return render_template('detail.html', st=st, df=fdf,
                           ind=ind_snapshot(fdf.iloc[-1]) if len(fdf) else None,
                           rgsnap=regime.snapshot(full, cfg) if len(full) else None,
                           tip=_tip_pack(full, cfg),
                           rsegs=_regime_segs(full, cfg, DETAIL_BARS),
                           k_json=json.dumps(_kline_payload(full, cfg), ensure_ascii=False))


def _nan2none(arr, nd=3):
    """指标序列 -> JSON 可用列表（NaN 一律给 null，前端不显示断点）"""
    return [None if x is None or (isinstance(x, float) and np.isnan(x))
            else round(float(x), nd) for x in arr]


def _regime_window(full, cfg, n=DETAIL_BARS):
    """最近 n 根日线 + 对齐的三态数组与强/弱标记（图表与波段表共用同一切片口径）"""
    win = full.tail(n)
    codes, _raw, flags = regime.regime_full(full, cfg)
    tail = len(full) - len(win)
    return win, codes[tail:], flags[tail:]


def _regime_segs(full, cfg, n=DETAIL_BARS):
    win, codes, flags = _regime_window(full, cfg, n)
    if not len(win):
        return []
    return regime.segments(list(win['date']), codes, win['close'].values,
                           win['high'].values, win['low'].values, flags,
                           regime.rcfg(cfg)['spike_days'])


def _regime_paint(dates, codes, close, high, low, strong=None, spike_days=0):
    """三态逐日状态 -> ECharts 需要的波段色块(areas) 与趋势切换点(flips)"""
    segs = regime.segments(dates, codes, close, high, low, strong, spike_days)
    areas, flips = [], []
    for k, sg in enumerate(segs):
        areas.append({'x0': sg['start'], 'x1': sg['end'], 'fill': sg['fill'],
                      'color': sg['color'], 'label_cn': sg['label_cn'],
                      'level_cn': sg['level_cn'], 'desc': sg['desc'],
                      'days': sg['days'], 'n_strong': sg['n_strong'],
                      'chg_pct': sg['chg_pct']})
        if k:
            i = sg['i0']
            y = float(low[i]) if low[i] == low[i] else float(close[i])
            g = regime.flip_grade(segs[k - 1]['kind'], sg['kind'])
            flips.append({'date': sg['start'], 'value': [sg['start'], round(y * 0.985, 3)],
                          'color': sg['color'], 'label_cn': sg['label_cn'],
                          'desc': sg['desc'], 'prev_cn': segs[k - 1]['label_cn'],
                          'grade': g['grade'], 'mark': g['mark'],
                          'ret': g['ret'], 'win': g['win'],
                          'text': '%s｜%s %s → %s' % (sg['start'], g['mark'],
                                                     segs[k - 1]['label_cn'],
                                                     sg['label_cn'])})
    return areas, flips


def _kline_payload(full, cfg, n=DETAIL_BARS, code=None):
    """一页三态K线数据：K线 + 三均线 + 布林 + 波段色块/分级切换点 + ADX/MACD/量能"""
    win, codes, flags = _regime_window(full, cfg, n)
    areas, flips = _regime_paint(list(win['date']), codes, win['close'].values,
                                 win['high'].values, win['low'].values, flags,
                                 regime.rcfg(cfg)['spike_days'])
    return {'code': code, 'dates': list(win['date']),
            'k': win[['open', 'close', 'low', 'high']].round(3).values.tolist(),
            'vol': [int(x) for x in win['volume']],
            'ma5': _nan2none(win['ma5']), 'ma20': _nan2none(win['ma20']),
            'ma60': _nan2none(win['ma60']),
            'boll_up': _nan2none(win['boll_up']), 'boll_mid': _nan2none(win['boll_mid']),
            'boll_low': _nan2none(win['boll_low']),
            'pctb': _nan2none(win['pctb']) if 'pctb' in win.columns else [],
            'adx': _nan2none(win['adx'], 2), 'atr': _nan2none(win['atr']),
            'dif': _nan2none(win['dif'], 4), 'dea': _nan2none(win['dea'], 4),
            'hist': _nan2none(win['macd_hist'], 4),
            'areas': areas, 'flips': flips,
            'adx_min': regime.rcfg(cfg)['adx_min'],
            'regime': regime.snapshot(full, cfg) if len(full) else None,
            'tip': _tip_pack(full, cfg)}


@app.route('/api/kline/<code>')
def api_kline(code):
    """详情页K线数据：K线 + 三均线 + 布林 + 三态波段/切换点 + ADX/MACD/量能副图"""
    cfg = config.load_config()
    full = compute_factors(data.get_kline(code, cfg['data_start']))
    return jsonify(_kline_payload(full, cfg, code=code))


@app.route('/api/waves/<code>')
def api_waves(code):
    """三态波段列表（起止日/天数/段内涨跌/高低），与图上背景色块一一对应"""
    cfg = _cfg_from_form(request.args, config.load_config())
    full = compute_factors(data.get_kline(code, cfg['data_start']))
    return jsonify({'code': code, 'regime': regime.snapshot(full, cfg),
                    'segments': _regime_segs(full, cfg, int(request.args.get('bars')
                                                            or DETAIL_BARS))})


def _load_ctx(cfg=None, stocks=None, refresh=True):
    cfg = cfg or config.load_config()
    stocks = stocks or config.load_stocks()
    dfs = {}
    for st in stocks:
        try:
            dfs[st['code']] = compute_factors(data.get_kline(st['code'], cfg['data_start'],
                                                             refresh=refresh))
        except Exception:
            continue
    return cfg, stocks, dfs


def _cfg_from_form(form, cfg=None):
    """表单里的三态/提示参数覆盖到配置上：图上波段、波段统计、日K标注、提示必须同一份口径"""
    cfg = cfg or config.load_config()
    rg = _form_section(form, REGIME_NUM_KEYS, REGIME_INT_KEYS, cfg.get('regime'))
    tt = _form_section(form, TIP_NUM_KEYS, TIP_INT_KEYS, cfg.get('t_trade'))
    if form.get('t_trade_enabled') is not None:
        tt['enabled'] = form.get('t_trade_enabled') in ('on', '1', 'true', 'True')
    elif form.get('t_trade_present') is not None:
        tt['enabled'] = False        # 提示区被提交但没有勾选开关
    out = dict(cfg, regime=rg)
    if any(form.get(k) not in (None, '') for k in TIP_NUM_KEYS) or 't_trade_present' in form:
        out['t_trade'] = tt
    return out


def _tip_pack(df, cfg):
    """今日提示包：给页面与图用（附带把 %B 上下沿换算成当日布林轨道上的价位）"""
    snap = regime.snapshot(df, cfg)
    tip = tips.for_stock(df, cfg, snap) if snap else None
    if not tip:
        return {}
    out = dict(tip)
    try:
        lo = float(df['boll_low'].values[-1])
        up = float(df['boll_up'].values[-1])
        if lo == lo and up == up and up > lo:
            out['t_buy_px'] = round(lo + tip['buy_pctb'] * (up - lo), 3)
            out['t_sell_px'] = round(lo + tip['sell_pctb'] * (up - lo), 3)
    except (KeyError, IndexError, TypeError, ValueError):
        pass
    return out


def _chart_bundle(stocks, dfs, start, end, cfg, max_charts=MAX_CHARTS):
    """股票行情页K线：K线 + 三均线/布林 + 三态波段与切换点 + ADX/RSI/量能（第4副图给 RSI）
    画图范围严格等于所选 start~end 区间（不再截断到最近 N 根）"""
    bundles = []
    for st in stocks[:max_charts]:
        df = dfs.get(st['code'])
        if df is None or not df.size:
            continue
        dates_all = list(df['date'])
        pos = [i for i, d in enumerate(dates_all) if start <= d <= end]
        if not pos:
            continue
        codes_all, _raw, flags_all = regime.regime_full(df, cfg)
        d = df.iloc[pos]
        codes = [codes_all[i] for i in pos]
        flags = [flags_all[i] for i in pos]
        areas, flips = _regime_paint(list(d['date']), codes, d['close'].values,
                                     d['high'].values, d['low'].values, flags,
                                     regime.rcfg(cfg)['spike_days'])
        cnt = {regime.BULL: codes.count(regime.BULL), regime.BEAR: codes.count(regime.BEAR),
               regime.RANGE: codes.count(regime.RANGE), regime.UNKNOWN: codes.count(regime.UNKNOWN)}
        bundles.append({
            'code': st['code'], 'name': st['name'],
            'bars': len(pos), 'n_bull': cnt[regime.BULL], 'n_bear': cnt[regime.BEAR],
            'n_range': cnt[regime.RANGE], 'n_unknown': cnt[regime.UNKNOWN],
            'n_strong': int(sum(1 for i, c in enumerate(codes)
                                if c and flags[i])),
            'n_segments': len(areas), 'n_flips': len(flips),
            'n_strong_flips': sum(1 for f in flips if f['grade'] == 'strong'),
            'dates': list(d['date']),
            'k': d[['open', 'close', 'low', 'high']].round(3).values.tolist(),
            'ma5': _nan2none(d['ma5']), 'ma20': _nan2none(d['ma20']),
            'ma60': _nan2none(d['ma60']),
            'boll_up': _nan2none(d['boll_up']), 'boll_mid': _nan2none(d['boll_mid']),
            'boll_low': _nan2none(d['boll_low']),
            'pctb': _nan2none(d['pctb']) if 'pctb' in d.columns else [],
            'adx': _nan2none(d['adx'], 2), 'atr': _nan2none(d['atr']),
            'vol': [int(x) for x in d['volume']],
            'dif': _nan2none(d['dif'], 4), 'dea': _nan2none(d['dea'], 4),
            'hist': _nan2none(d['macd_hist'], 4),
            'rsi': _nan2none(d['rsi14'], 1), 'sub4': 'rsi',
            'areas': areas, 'flips': flips,
            'adx_min': regime.rcfg(cfg)['adx_min'],
            'tip': _tip_pack(df, cfg),
        })
    return bundles


@app.route('/market', methods=['GET', 'POST'])
def market_page():
    """历史日K上的波段标注与统计（原回测页：交易层已按需求删除，只留行情识别）"""
    cfg = config.load_config()
    stocks = config.load_stocks()
    form, charts, stats = {}, [], None
    sel_codes = {s['code'] for s in stocks}
    start = form.get('start', '2022-01-01')
    end = form.get('end', '2026-12-31')
    if request.method == 'POST':
        form = request.form.to_dict()
        start = form.get('start', '2022-01-01')
        end = form.get('end', '2026-12-31')
        pcfg = _cfg_from_form(form, cfg)
        # 默认使用本地缓存（秒级）；勾选“更新行情”才增量拉取
        _c2, stocks2, dfs = _load_ctx(pcfg, stocks, refresh=form.get('refresh_data') == 'on')
        codes = request.form.getlist('codes') or [s['code'] for s in stocks]
        sel_codes = set(codes)
        sel = [s for s in stocks2 if s['code'] in codes]
        charts = _chart_bundle(sel, dfs, start, end, pcfg)
        stats = scan_mod.range_regime_days(sel, dfs, pcfg, start, end)
    return render_template('market.html', stocks=stocks, form=form, cfg=cfg,
                           charts=charts, stats=stats, sel_codes=sel_codes,
                           start=start, end=end,
                           rg=cfg.get('regime') or {}, tt=cfg.get('t_trade') or {})


@app.route('/backtest')
def backtest_alias():
    """旧地址兼容：回测页已改造为“股票行情”页"""
    return redirect(url_for('market_page'))


@app.route('/doc')
def doc_page():
    """需求文档（识别规则/参数/波段统计/边界）在线查看，原文为仓库根目录 需求文档.md"""
    try:
        with open(DOC_PATH, encoding='utf-8') as f:
            text = f.read()
    except Exception as e:
        text = '需求文档读取失败：%s' % e
    return render_template('doc.html', text=text, html=_md_html(text))


def _md_inline(s):
    """行内元素：先转义，再处理 `code` 与 **bold**"""
    s = _esc(s, quote=False)
    s = _re.sub(r'`([^`]+)`', lambda m: '<code>%s</code>' % m.group(1), s)
    s = _re.sub(r'\*\*([^*]+)\*\*', lambda m: '<b>%s</b>' % m.group(1), s)
    return s


_MD_BLOCK_START = _re.compile(r'^(#{1,6}\s|```|>\s*|[-*]\s|\d+\.\s|\||---$)')


def _md_html(text):
    """需求文档轻量渲染（libs 内无 markdown 库，只支持本文档用到的语法）"""
    lines = (text or '').split('\n')
    out, i = [], 0
    while i < len(lines):
        s = lines[i].strip()
        if not s:
            i += 1
            continue
        if s == '---':
            out.append('<hr>')
            i += 1
            continue
        if s.startswith('```'):
            i += 1
            buf = []
            while i < len(lines) and not lines[i].strip().startswith('```'):
                buf.append(_esc(lines[i], quote=False))
                i += 1
            i += 1
            out.append('<pre class="mdcode">%s</pre>' % '\n'.join(buf))
            continue
        m = _re.match(r'^(#{1,6})\s+(.*)$', s)
        if m:
            lv = min(len(m.group(1)) + 1, 6)      # 页面已有 h2 标题，文档层级下移一级
            out.append('<h%d>%s</h%d>' % (lv, _md_inline(m.group(2)), lv))
            i += 1
            continue
        if s.startswith('|') and i + 1 < len(lines) and \
                _re.match(r'^\|[\s:\-|]+\|$', lines[i + 1].strip()):
            cells = lambda x: [c.strip() for c in x.strip().strip('|').split('|')]
            head = cells(s)
            i += 2
            body = []
            while i < len(lines) and lines[i].strip().startswith('|'):
                body.append(cells(lines[i]))
                i += 1
            thead = ''.join('<th>%s</th>' % _md_inline(c) for c in head)
            tb = ''.join('<tr>%s</tr>' % ''.join('<td>%s</td>' % _md_inline(c) for c in r)
                         for r in body)
            out.append('<table class="mdtab"><thead><tr>%s</tr></thead>'
                       '<tbody>%s</tbody></table>' % (thead, tb))
            continue
        if s.startswith('>'):
            buf = []
            while i < len(lines) and lines[i].strip().startswith('>'):
                buf.append(_md_inline(lines[i].strip().lstrip('>').strip()))
                i += 1
            out.append('<blockquote>%s</blockquote>' % '<br>'.join(buf))
            continue
        if _re.match(r'^([-*]|\d+\.)\s+', s):
            ordered = bool(_re.match(r'^\d+\.', s))
            items = []
            while i < len(lines) and _re.match(r'^\s*([-*]|\d+\.)\s+', lines[i] or ' '):
                item = _re.sub(r'^\s*([-*]|\d+\.)\s+', '', lines[i])
                i += 1
                # 续行：同一条目写折行时不能断成两个 <li>（否则 **加粗** 跨行就露出星号）
                while (i < len(lines) and lines[i].strip()
                       and lines[i][:1].isspace()
                       and not _MD_BLOCK_START.match(lines[i].strip())):
                    item += ' ' + lines[i].strip()
                    i += 1
                items.append(_md_inline(item))
            tag = 'ol' if ordered else 'ul'
            out.append('<%s>%s</%s>' % (tag,
                                        ''.join('<li>%s</li>' % x for x in items), tag))
            continue
        buf = []
        while i < len(lines) and lines[i].strip() and \
                not _MD_BLOCK_START.match(lines[i].strip()):
            buf.append(lines[i].strip())
            i += 1
        # 先拼行再上样式：否则 **加粗** 跨行会露出星号
        out.append('<p>%s</p>' % _md_inline(' '.join(buf)))
    return '\n'.join(out)


@app.route('/notify', methods=['POST'])
def notify_page():
    dry = request.form.get('dry') == '1'
    out = scan_mod.scan(refresh=False)
    trend = scan_mod.build_trend_report(out)
    lines = format_trend_message(trend['date'], trend)
    result = notify_push(autorun.TITLE, lines, out['cfg'], dry_run=dry)
    ch = describe_channel(out['cfg'])
    code = result.get('code')
    if dry:
        msg = 'dry-run 预览已生成(通道:%s)' % ch
    elif code == 0:
        msg = '已发送飞书播报(通道:%s)' % result.get('channel', ch)
    else:
        msg = '发送失败 code=%s msg=%s（当前通道:%s，请到设置页配置）' % (code, result.get('msg'), ch)
    flash('%s，趋势 %d多头/%d空头/%d震荡（今日变化 %d 只 · 酝酿 %d 只 · 未判定 %d 只）' % (
        msg, trend['n_bull'], trend['n_bear'], trend['n_range'],
        len(trend['changed']), len(trend['pending']), len(trend['quiet'])))
    return redirect(url_for('dashboard'))


@app.route('/notify/setup', methods=['POST'])
def notify_setup():
    """探测飞书应用机器人配置：取 token / 列群 / 手机号换 open_id"""
    cfg = config.load_config()
    form = request.form.to_dict()
    for k in ('feishu_app_id', 'feishu_app_secret', 'feishu_receive_id',
              'feishu_receive_id_type', 'feishu_webhook', 'feishu_webhook_secret',
              'notify_channel'):
        if form.get(k) is not None:
            cfg[k] = form.get(k).strip()
    config.save_config(cfg)

    app_id, app_secret = cfg['feishu_app_id'], cfg['feishu_app_secret']
    report = {'channel': describe_channel(cfg), 'token_ok': False, 'token_msg': '',
              'chats_ok': False, 'chats': [], 'chats_msg': '',
              'users_ok': False, 'users': [], 'users_msg': '', 'probe': None}
    token, tok_res = notifier.get_tenant_access_token(app_id, app_secret)
    report['token_ok'] = bool(token)
    report['token_msg'] = '获取成功(已缓存2小时)' if token else 'code=%s %s' % (tok_res.get('code'), tok_res.get('msg'))
    if token:
        chats = notifier.app_list_chats(app_id, app_secret)
        if chats.get('code') == 0:
            report['chats_ok'] = True
            report['chats'] = chats['chats']
        else:
            report['chats_msg'] = 'code=%s %s' % (chats.get('code'), chats.get('msg'))
        mobiles = [m.strip() for m in form.get('probe_mobiles', '').replace(',', ' ').split() if m.strip()]
        if mobiles:
            res = notifier.app_resolve_user_id(app_id, app_secret, mobiles=mobiles,
                                               id_type=cfg.get('feishu_receive_id_type', 'open_id'))
            if res.get('code') == 0:
                report['users_ok'] = True
                report['users'] = res['users']
            else:
                report['users_msg'] = 'code=%s %s' % (res.get('code'), res.get('msg'))
        if form.get('probe_send') == '1':
            rcv = cfg['feishu_receive_id']
            if rcv:
                r = notifier.app_send_text(app_id, app_secret, rcv, '趋势工具连接测试',
                                           ['如收到此条，说明应用机器人通道已打通'],
                                           cfg.get('feishu_receive_id_type', 'open_id'))
                report['probe'] = {'ok': r.get('code') == 0, 'code': r.get('code'),
                                   'msg': r.get('msg', ''), 'channel': r.get('channel', '-')}
            else:
                report['probe'] = {'ok': False, 'code': -1, 'msg': '接收者 ID 未填写', 'channel': '-'}
    return render_template('setup_result.html', report=report, cfg=cfg)


@app.route('/autorun', methods=['POST'])
def autorun_route():
    """盘后自动运行开关：启用/停用/立即手动跑一轮"""
    cfg = config.load_config()
    action = request.form.get('action')
    if action in ('on', 'off'):
        cfg['auto_run_enabled'] = action == 'on'
        t = (request.form.get('auto_run_time') or '').strip()
        if t and autorun.parse_hhmm(t):
            cfg['auto_run_time'] = t
        config.save_config(cfg)
        started = autorun.start(cfg)
        flash(('已启用盘后自动播报，每日 %s 一次' % cfg['auto_run_time']) if started
              else '已停用盘后自动运行')
    elif action == 'run':
        try:
            r = autorun.run_once(cfg, force=True, manual=True)
            extra = ''
            if not r.get('skipped'):
                sr = autorun.push_sell_alert(cfg)
                if sr.get('pushed'):
                    extra = '｜卖出预警已推 %d 只（红%d/黄%d）' % (
                        sr.get('n_alerts', 0), sr.get('n_red', 0), sr.get('n_yellow', 0))
                else:
                    extra = '｜卖出预警：%s' % (sr.get('reason') or sr.get('error') or '未推')
            flash('手动运行完成：%s%s' % (r.get('summary', r.get('data_date')), extra))
        except Exception as e:
            flash('手动运行失败: %s: %s' % (type(e).__name__, e))
    return redirect(request.form.get('next') or url_for('dashboard'))


# ---------------- 卖出通知工具（独立页群：预警总览 / 因子配置 / 监控清单 / 历史回测） ----------------

def _sell_cfg_from_form(form, cfg):
    """只有带 sell_present 标记时才改 sell_notify 段；空/非法值不动原配置（与三态/提示层同一约定）"""
    sn = _form_section(form, SELL_NUM_KEYS, SELL_INT_KEYS, cfg.get('sell_notify'))
    sn.update(_form_section(form, SELL_INT_KEYS, SELL_INT_KEYS, sn))
    sn['enabled'] = form.get('sell_enabled') == 'on'
    sn['d_on'] = form.get('sell_d_on') == 'on'
    out = dict(cfg)
    out['sell_notify'] = sn
    return out


@app.route('/sell')
def sell_page():
    """卖出信号监控（单页）：四路信号逐票卡片 + 可折叠配置（因子阈值/通知） + 回测/清单入口
    首次拉数据较慢，当日缓存后秒开；?refresh=1 强制重拉"""
    rep = sellscan.scan_sell(refresh=request.args.get('refresh') == '1')
    preview = format_sell_message(rep.get('date'), rep)
    return render_template('sell.html', rep=rep, preview=preview,
                           logs=sellscan.read_log(5), sn=rep['cfg'].get('sell_notify') or {})


@app.route('/sell/send', methods=['POST'])
def sell_send():
    """把当前预警推飞书：dry=1 只生成预览；两者都写入发送记录供网页展示"""
    dry = request.form.get('dry') == '1'
    rep = sellscan.scan_sell(refresh=False)
    lines = format_sell_message(rep.get('date'), rep)
    result = notify_push(SELL_TITLE, lines, rep['cfg'], dry_run=dry)
    text = SELL_TITLE + '\n' + '\n'.join(lines)
    ch = describe_channel(rep['cfg'])
    ok = result.get('code') == 0
    sellscan.append_log({'ts': sellscan.datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                         'dry': dry, 'ok': ok, 'code': result.get('code'),
                         'msg': str(result.get('msg', ''))[:80],
                         'channel': result.get('channel', ch), 'text': text})
    if dry:
        flash('dry-run 预览已生成并记入发送历史（通道:%s，未真推送）' % ch)
    elif ok:
        flash('已发送卖出预警到飞书（通道:%s），预警 %d 只' % (
            result.get('channel', ch), len(rep.get('alerts') or [])))
    else:
        flash('发送失败 code=%s msg=%s（通道:%s）' % (result.get('code'), result.get('msg'), ch))
    return redirect(url_for('sell_page'))


@app.route('/sell/factors', methods=['GET', 'POST'])
def sell_factors_page():
    """因子配置已并入 /sell 的可折叠区；本路由只负责保存后跳回总览页（GET 直接回总览）"""
    if request.method == 'POST' and 'sell_present' in request.form:
        cfg = config.load_config()
        cfg = _sell_cfg_from_form(request.form, cfg)
        config.save_config(cfg)
        flash('因子配置已保存，重扫后生效')
    return redirect(url_for('sell_page'))


@app.route('/sell/backtest', methods=['GET', 'POST'])
def sell_backtest_page():
    """历史回测：复盘四路信号在过去何时进入预警，附事件后 5/10/20 个交易日涨跌作有效性参考"""
    form, bt = {}, None
    if request.method == 'POST':
        form = request.form.to_dict()
        codes = request.form.getlist('codes') or None
        try:
            years = int(form.get('years') or 0) or None
        except ValueError:
            years = None
        try:
            bt = sellscan.backtest(codes=codes, years=years)
        except Exception as e:
            flash('回测失败：%s: %s' % (type(e).__name__, e))
    cfg = config.load_config()
    sn = cfg.get('sell_notify') or {}
    return render_template('sell_backtest.html', bt=bt, form=form,
                           stocks=config.load_stocks(), sn=sn)


if __name__ == '__main__':
    autorun.start(config.load_config())     # 开关为开时拉起盘后定时线程
    app.run(host='0.0.0.0', port=8899, debug=False)
