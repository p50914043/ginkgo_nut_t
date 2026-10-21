# -*- coding: utf-8 -*-
"""切换分级统计：核对“震荡→趋势 / 趋势→震荡”这些切换到底值多少钱

口径（与需求文档一致）：
  · 用 core/regime.py 的生效三态（含滞回 + 毛刺过滤）逐日推演；
  · 切换次日收盘买入，看未来 horizon 个交易日的收盘收益；
  · “方向胜率”= 该次切换后收益方向与 trend 是否一致（震荡→多头算涨、震荡→空头算跌）。

用法：PYTHONPATH=./libs: python3 scripts_flip_stats.py [回溯交易日数=501] [horizon=10]
"""
import sys
sys.path.insert(0, 'libs')
sys.path.insert(0, '.')

import collections

from core import config, data, regime
from core.indicators import compute_factors


def collect(bars=501, horizon=10, cfg=None):
    cfg = cfg or config.load_config()
    r = regime.rcfg(cfg)
    stat = collections.defaultdict(lambda: {'n': 0, 'ret_sum': 0.0, 'win': 0, 'ret_sq': 0.0})
    per_stock = []
    for st in config.load_stocks():
        try:
            df = compute_factors(data.get_kline(st['code'], cfg['data_start']))
        except Exception as e:
            print('  跳过 %s：%s' % (st['code'], e))
            continue
        codes, _raw, flags = regime.regime_full(df, cfg)
        # 只统计最近 bars 个交易日：均线/状态机用全量历史算，但只取窗口内的切片
        n0 = max(0, len(df) - bars)
        win = df.iloc[n0:]
        segs = regime.segments(list(win['date']), list(codes[n0:]), win['close'].values,
                               win['high'].values, win['low'].values, flags[n0:],
                               r['spike_days'])
        close = df['close'].values
        # 波段起点即“生效切换日”：从起点次日收盘买入（i0 是窗口内下标，要补回全量偏移）
        n_flip = 0
        for k, sg in enumerate(segs):
            if k == 0:
                continue                      # 第一段没有“前态”，不算切换
            prev_kind = segs[k - 1]['kind']
            i = n0 + sg['i0']
            entry, out = i + 1, i + 1 + horizon
            if out >= len(close) or entry >= len(close):
                continue
            ret = (close[out] / close[entry] - 1.0) * 100
            key = (prev_kind, sg['kind'])
            a = stat[key]
            a['n'] += 1
            a['ret_sum'] += ret
            a['ret_sq'] += ret * ret
            want = 1 if sg['kind'] == regime.BULL else (-1 if sg['kind'] == regime.BEAR else 0)
            if want:
                a['win'] += int(ret * want > 0)
            else:
                a['win'] += int(ret < 0)       # 进入震荡：算“别指望有方向”，下跌比例做参考
            n_flip += 1
        days = collections.Counter(c for c in codes[n0:] if c)
        per_stock.append((st['name'], len(df), n_flip, days))
    return stat, per_stock, len(config.load_stocks())


def main():
    bars = int(sys.argv[1]) if len(sys.argv) > 1 else 501
    horizon = int(sys.argv[2]) if len(sys.argv) > 2 else 10
    cfg = config.load_config()
    r = regime.rcfg(cfg)
    print('参数：MA20×MA%d slope>%.2f%% ADX>%.1f 滞回%d日 毛刺%d日' % (
        r['ma_slow'], r['slope_min'], r['adx_min'], r['confirm_days'], r['spike_days']))
    stat, per_stock, n_stocks = collect(bars, horizon, cfg)
    print('样本：%d 只标的 · 最近 %d 个交易日 · 持有 %d 个交易日\n' % (n_stocks, bars, horizon))
    for st_name, n_all, n_flip, days in per_stock:
        print('  %-8s 数据%d行 切换%d次  多头%d 空头%d 震荡%d' % (
            st_name, n_all, n_flip, days[regime.BULL], days[regime.BEAR], days[regime.RANGE]))
    print('\n切换分级（买入次日收盘，看未来 %d 日）' % horizon)
    order = [(regime.RANGE, regime.BULL), (regime.RANGE, regime.BEAR),
             (regime.BULL, regime.RANGE), (regime.BEAR, regime.RANGE),
             (regime.BULL, regime.BEAR), (regime.BEAR, regime.BULL)]
    for key in order:
        a = stat.get(key)
        if not a or not a['n']:
            continue
        avg = a['ret_sum'] / a['n']
        sd = (max(a['ret_sq'] / a['n'] - avg * avg, 0)) ** 0.5
        g = regime.flip_grade(key[0], key[1])
        print('  %-3s %s → %s：%2d 次  均净 %+5.2f%%  中位附近σ %.2f%%  方向胜率 %.0f%%  [%s]' % (
            g['mark'], regime.LABEL_CN[key[0]], regime.LABEL_CN[key[1]], a['n'], avg, sd,
            a['win'] / a['n'] * 100, g['grade']))
    tot = sum(a['n'] for a in stat.values())
    print('  ——合计切换 %d 次' % tot)


if __name__ == '__main__':
    main()
