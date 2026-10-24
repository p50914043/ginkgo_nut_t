# -*- coding: utf-8 -*-
"""配置与股票清单读写

需求：判定层只做行情识别（三态 + 强/弱档），参数留三态判定 + 提示层 + 数据 + 盘后自动运行 + 飞书通道；
更早一轮的仓位/成本/网格/寻优类交易参数仍列为废弃键（OBSOLETE），读写两侧都会丢弃；
但提示层的 %B 上下沿、硬止损、最长持有日数现在是“区间纪律提示”的参数，不再属于交易记账。
"""
import os

import yaml

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(BASE_DIR, 'config.yaml')
STOCKLIST_PATH = os.path.join(BASE_DIR, 'stocklist.yaml')

# 卖出通知工具：标的类型 → 估值口径说明（判定在 core/sellscan.py，PEV 数据源缺失，保险暂用 PB 代理）
SELL_TYPES = ('value', 'bank', 'insurance', 'cycle')

DEFAULT_CONFIG = {
    # 三态行情识别（唯一判定层，只用均线）：MA20 与慢线的高低定结构，MA20 五日斜率定方向
    'regime': {
        'ma_fast': 5,           # 快线：界面展示与辅助用（判据以 MA20/慢线为准）
        'ma_slow': 60,          # 慢线：中期结构分界
        'slope_min': 0.3,       # MA20 的 5 日斜率阈值%(绝对值)：不到它就不算“强”趋势
        'confirm_days': 3,      # 滞回：新状态连续成立 N 个交易日才切换
        'adx_min': 25.0,        # ADX(14) 动能门槛：结构+方向都对但 ADX 不够 → 弱多头/弱空头
        'spike_days': 3,        # 毛刺过滤：持续不足 N 日的片段并入相邻主状态，不成波段
    },
    # 提示层（不改变三态判定）：只在震荡段给做T纪律提示，多头段给回踩介入提示
    't_trade': {
        'enabled': True,
        'buy_pctb': 0.10,       # 区间下沿：%B ≤ 此值给买入提示
        'sell_pctb': 0.90,      # 区间上沿：持仓中 %B ≥ 此值给卖出提示
        'stop_pct': 5.0,        # 硬止损：自提示买入当日收盘跌这么多就走
        'max_hold': 15,         # 最长持有交易日数，超了提示离场
        'pb_touch': 1.5,        # 多头回踩容差%：最低价距 MA20 这么近算“踩到”
        'vol_shrink': 0.8,      # 缩量：当日量比(20) 低于此倍数才算缩
    },
    # 卖出通知工具（信号 A 估值 / B 基本面 / C 行业与宏观 / D 社保保险减持）：只出预警，不接管下单
    'sell_notify': {
        'enabled': True,
        # 信号 A：估值分位预警（非金融 PE-TTM 5 年 / 银行 PB 10 年 / 保险 PB 代理 PEV 5 年 / 周期 PB 10 年）
        'a_yellow': 70.0,       # 历史分位 ≥ 此值 → 黄色预警（%）
        'a_red': 80.0,          # 历史分位 ≥ 此值 → 红色预警（%）
        'a_years_pe': 5,        # PE / PEV（代理）分位回看年数
        'a_years_pb': 10,       # PB 分位回看年数
        # 信号 B：基本面恶化（同源性分组，跨组才计数；任一组命中即黄色）
        'b_ocf_ratio': 0.5,     # 经营现金流/净利润 低于此值算现金流恶化（连续 2 期触发）
        'b_debt_up': 10.0,      # 资产负债率同比升超此值（百分点）
        'b_growth_gap': 30.0,   # 应收/存货平均增速 − 营收增速 超过此值（百分点）算显著超前
        'b_roe_years': 3,       # ROE 连降判定的年报份数（3 = 最近三个年度逐级下降）
        'b_div_drop': 30.0,     # 年度分红总额同比降超此 % 或取消 → 命中
        'b_red_groups': 3,      # 命中组数 ≥ 此值 → 升红色（默认要三组全中）
        # 信号 C：行业与宏观（行业指数回撤 × 同行业绩预警，双条件同时成立才预警）
        'c_dd_pct': 20.0,       # 行业指数自近期高点回撤阈值（%）
        'c_dd_days': 250,       # 近期高点回看交易日数
        'c_warn_days': 30,      # 业绩预警公告回看自然日数
        'c_warn_peers': 2,      # 同行发预警的公司数 ≥ 此值算“多家”
        # 信号 D：社保/保险减持
        'd_on': True,
        # 历史回测
        'bt_years': 3,          # 默认回测年数
    },
    # 盘后自动运行：工作日到点自动「更新数据→扫描→飞书播报」一次，当日只跑一次
    'auto_run_enabled': False,
    'auto_run_time': '15:35',
    'auto_run_retry': False,     # 判定为非交易日后，本轮是否继续重试
    # 通知通道: auto(优先应用机器人) / app_bot / webhook
    'notify_channel': 'auto',
    'feishu_webhook': '',
    'feishu_webhook_secret': '',     # 群机器人"加签"密钥，未启用则留空
    'feishu_app_id': '',             # 企业自建应用 App ID
    'feishu_app_secret': '',         # 企业自建应用 App Secret
    'feishu_receive_id': '',         # open_id(ou_xxx) / user_id / union_id / chat_id(oc_xxx)
    'feishu_receive_id_type': 'open_id',
    'data_start': '2015-01-01',
}

# 已从工具中移除的废弃键（含更早一轮删掉的仓位/日内参数）：读入丢弃、写出不落盘
OBSOLETE = (
    'capital', 'cost_rate', 'fixed_t_amount', 'max_hold_days', 'sl_atr', 'tp_atr',
    'tp_part', 'trail_atr', 'entry_box_max', 'entry_pullback', 'adx_min', 'adx_max',
    'min_votes', 'redline_loss_pct', 'enable_base_filter', 'tier_thresholds', 'grid',
    'pos_frac', 'sizing_mode', 'exit_on_flip', 'close_stop_atr', 'intraday',
    'tier_max_positions',
)


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding='utf-8') as f:
            user_cfg = yaml.safe_load(f) or {}
        for k, v in user_cfg.items():
            if k in OBSOLETE:
                continue                          # 已废弃的旧键不再读入，免得界面不显示却影响结果
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                merged = dict(cfg[k])
                merged.update({kk: vv for kk, vv in v.items() if kk in cfg[k]})
                cfg[k] = merged
            else:
                cfg[k] = v
    return cfg


def save_config(cfg):
    out = {k: v for k, v in cfg.items() if k not in OBSOLETE}
    for k, v in list(out.items()):
        if isinstance(v, dict):
            out[k] = {kk: vv for kk, vv in v.items() if kk in DEFAULT_CONFIG.get(k, {})}
    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
        yaml.safe_dump(out, f, allow_unicode=True, sort_keys=False)


def load_stocks():
    """股票清单：趋势识别只关心代码/名称；卖出预警额外用 type(估值口径)/index(行业指数)/enabled(纳入预警)
    三个字段（早期“单独维护一份 sellwatchlist”已合并到这份主清单，两功能共用）"""
    if not os.path.exists(STOCKLIST_PATH):
        return []
    with open(STOCKLIST_PATH, encoding='utf-8') as f:
        data = yaml.safe_load(f) or []
    out = []
    for item in data:
        code = str(item.get('code', '')).strip()
        if not code:
            continue
        stype = item.get('type') or 'value'
        out.append({
            'code': code,
            'name': item.get('name', '') or '',
            'type': stype if stype in SELL_TYPES else 'value',
            'index': str(item.get('index', '') or '').strip(),
            'enabled': bool(item.get('enabled', True)),
        })
    return out


def save_stocks(stocks):
    out = [{'code': str(s.get('code', '')).strip(),
            'name': s.get('name', '') or '',
            'type': (s.get('type') or 'value') if (s.get('type') or 'value') in SELL_TYPES else 'value',
            'index': str(s.get('index', '') or '').strip(),
            'enabled': bool(s.get('enabled', True))}
           for s in stocks if str(s.get('code', '')).strip()]
    with open(STOCKLIST_PATH, 'w', encoding='utf-8') as f:
        yaml.safe_dump(out, f, allow_unicode=True, sort_keys=False)


# 卖出预警不再单独维护清单：早期 load_sell_stocks/save_sell_stocks 一律映射到主清单（兼容旧引用）
load_sell_stocks = load_stocks
save_sell_stocks = save_stocks
