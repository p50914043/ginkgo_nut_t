# -*- coding: utf-8 -*-
"""盘后调度（crontab 入口）：更新数据 -> 三态扫描 -> 飞书趋势播报
用法: python3 scheduler.py [--dry] [--test]
  --dry  只打印不推送;  --test  跳过扫描，只发一条连接测试消息
  30 15 * * 1-5 cd /home/chas/workspace/ginkgo-nut_t && PYTHONPATH=./libs: python3 scheduler.py >> cron.log 2>&1
"""
import sys
sys.path.insert(0, 'libs')
sys.path.insert(0, '.')

from core import config, scan as scan_mod
from core.notifier import push, format_trend_message, describe_channel
from core.autorun import TITLE, push_sell_alert


def _report(res, dry):
    if dry:
        print('通道=%s (dry-run 未推送)' % describe_channel(config.load_config()))
        return
    if res.get('code') == 0:
        print('✅ 已推送飞书, 通道=%s' % res.get('channel', '-'))
    else:
        print('❌ 未推送: code=%s msg=%s（请到网页“设置→飞书通知”配置通道）'
              % (res.get('code'), res.get('msg')))


def main():
    dry = '--dry' in sys.argv
    cfg = config.load_config()
    if '--test' in sys.argv:
        _report(push(TITLE, ['这是一条来自趋势识别工具的通道测试消息'], cfg, dry_run=dry), dry)
        return
    out = scan_mod.scan(cfg, refresh=True)
    trend = scan_mod.build_trend_report(out)
    print('扫描完成 %s：%d 只标的｜多头 %d · 空头 %d · 震荡 %d｜今日变化 %d 只 · 酝酿中 %d 只' % (
        trend['date'], len(out['results']), trend['n_bull'], trend['n_bear'],
        trend['n_range'], len(trend['changed']), len(trend['pending'])))
    for r in out['results']:
        snap = r.get('regime') or {}
        print('  %-6s %-6s %-4s 收盘 %-8s 本波段第 %s 日' % (
            r['code'], r['name'], snap.get('label_cn', r['status']),
            ('%.2f' % r['close']) if r.get('close') else '-', snap.get('days', '-')))
    _report(push(TITLE, format_trend_message(trend['date'], trend), cfg, dry_run=dry), dry)
    # 趋势播报后追发卖出预警（仅当出现红/黄信号才推）
    sell = push_sell_alert(cfg, dry_run=dry)
    if sell.get('pushed'):
        print('✅ 卖出预警已推送：红%d/黄%d 共%d只' % (
            sell.get('n_red', 0), sell.get('n_yellow', 0), sell.get('n_alerts', 0)))
    elif sell.get('skipped'):
        print('卖出预警：%s（未推送）' % sell.get('reason'))
    elif sell.get('error'):
        print('卖出预警：扫描失败 %s' % sell.get('error'))
    else:
        print('卖出预警：推送未成功 code=%s' % (sell.get('result') or {}).get('code'))


if __name__ == '__main__':
    main()
