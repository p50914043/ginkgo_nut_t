# -*- coding: utf-8 -*-
"""盘后自动运行开关：内置定时线程，到点自动「更新数据 → 三态扫描 → 飞书趋势播报」一次

与 crontab 二选一（都开也不会重复：同一自然日只跑一次）。
交易日保护：拉到的最新数据日期不是今天（节假日/数据源滞后）时不推送，避免空跑打扰。
"""
import datetime as dt
import json
import os
import threading

from . import config
from . import scan as scan_mod
from .notifier import push, format_trend_message

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_PATH = os.path.join(BASE_DIR, 'data', 'autorun_state.json')
LOG_PATH = os.path.join(BASE_DIR, 'autorun.log')

_lock = threading.Lock()
_thread_lock = threading.Lock()
_thread = None
_stop = threading.Event()
TITLE = '趋势播报｜多头/空头/震荡 每日识别 + 趋势变化提醒（只做行情识别，不产生交易指令）'


# ---------- 状态读写 ----------

def load_state():
    try:
        with open(STATE_PATH, encoding='utf-8') as f:
            return json.load(f) or {}
    except Exception:
        return {}


def save_state(state):
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        with open(STATE_PATH, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def _now():
    """单一时钟入口：测试里 monkeypatch 它，避免“跨零点跑用例”时假失败"""
    return dt.datetime.now()


def _log(line):
    try:
        with open(LOG_PATH, 'a', encoding='utf-8') as f:
            f.write('%s %s\n' % (_now().strftime('%Y-%m-%d %H:%M:%S'), line))
    except Exception:
        pass


def parse_hhmm(text):
    """'15:35' -> (15, 35)；非法返回 None"""
    try:
        h, m = str(text).strip().split(':')
        h, m = int(h), int(m)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    except Exception:
        pass
    return None


# ---------- 触发判断 ----------

def should_run(now=None, cfg=None, state=None):
    """返回 (是否该跑, 原因说明)"""
    now = now or _now()
    cfg = cfg or config.load_config()
    if not cfg.get('auto_run_enabled'):
        return False, '开关未启用'
    if now.weekday() >= 5:
        return False, '周末非交易日'
    hm = parse_hhmm(cfg.get('auto_run_time', '15:35'))
    if hm is None:
        return False, '触发时间格式非法(应为 HH:MM)'
    if (now.hour, now.minute) < hm:
        return False, '未到触发时间 %s' % cfg.get('auto_run_time')
    state = state if state is not None else load_state()
    today = now.strftime('%Y-%m-%d')
    if state.get('last_run_date') == today:
        return False, '今日已运行过'
    if state.get('last_skip_date') == today and not cfg.get('auto_run_retry'):
        return False, '今日已判定非交易日(不重试)'
    return True, '满足条件'


def latest_data_date(results):
    dates = [r.get('date') for r in results if r.get('date')]
    return max(dates) if dates else None


# ---------- 执行 ----------

def run_once(cfg=None, force=False, manual=False):
    """跑一轮完整流程；force/manual=True 时忽略交易日保护并照常推送"""
    cfg = cfg or config.load_config()
    started = _now()
    with _lock:
        out = scan_mod.scan(cfg, refresh=True)
        trend = scan_mod.build_trend_report(out)
        date_str = trend['date']
        data_date = latest_data_date(out['results']) or date_str
        today = started.strftime('%Y-%m-%d')
        trading_day = data_date == today
        state = load_state()

        if not trading_day and not force:
            state.update(last_skip_date=today, last_result='跳过',
                         last_detail='最新数据日期 %s != 今日 %s，判定非交易日' % (data_date, today),
                         last_run_at=started.strftime('%Y-%m-%d %H:%M:%S'))
            save_state(state)
            _log('跳过推送: 非交易日 (数据日期 %s)' % data_date)
            return {'pushed': False, 'skipped': True, 'data_date': data_date, 'rows': 0}

        lines = format_trend_message(data_date, trend)
        res = push(TITLE, lines, cfg, dry_run=False)
        ok = res.get('code') == 0
        pushed = ok and res.get('msg') != 'dry_run'
        # 摘要里带上分级计数：页面/日志一眼能看出今天有没有强信号
        summary = ('趋势 多头%d/空头%d/震荡%d（今日变化 %d 只：🔴🔴 %d / 🟡 %d；酝酿 %d 只）'
                   '/ 未判定 %d 只 / 数据日期 %s / 通道 %s / code=%s %s' % (
            trend['n_bull'], trend['n_bear'], trend['n_range'],
            len(trend['changed']), len(trend['strong_changed']),
            len(trend['momentum_changed']), len(trend['pending']),
            len(trend['quiet']), data_date,
            res.get('channel', '-'), res.get('code'), res.get('msg')))
        state.update(last_run_date=today, last_result='成功' if ok else '失败',
                     last_detail=summary, last_rows=len(trend['rows']),
                     last_trend='%d/%d/%d' % (trend['n_bull'], trend['n_bear'], trend['n_range']),
                     last_trend_changed=[x['name'] for x in trend['changed']],
                     last_strong_flips=len(trend['strong_changed']),
                     last_momentum_flips=len(trend['momentum_changed']),
                     last_data_date=data_date,
                     last_pushed=pushed,
                     last_run_at=started.strftime('%Y-%m-%d %H:%M:%S'))
        if ok:
            state.pop('last_skip_date', None)
        save_state(state)
        _log(('手动' if manual else '自动') + '运行: ' + summary)
        return {'pushed': pushed, 'skipped': False, 'data_date': data_date,
                'rows': len(trend['rows']), 'summary': summary, 'trend': trend,
                'result': res}


# ---------- 线程控制 ----------

def loop(interval=30):
    while not _stop.is_set():
        try:
            cfg = config.load_config()          # 每轮重读，页面改开关立即生效
            due, why = should_run(cfg=cfg)
            if due:
                run_once(cfg)
        except Exception as e:
            _log('自动运行异常: %s: %s' % (type(e).__name__, e))
        _stop.wait(interval)


def start(cfg=None):
    """按当前配置启动守护线程（幂等）"""
    global _thread
    cfg = cfg or config.load_config()
    with _thread_lock:
        if not cfg.get('auto_run_enabled'):
            _stop.set()
            _thread = None
            return False
        if _thread and _thread.is_alive():
            return True
        _stop.clear()
        _thread = threading.Thread(target=loop, name='autorun', daemon=True)
        _thread.start()
        _log('定时线程已启动 (每日 %s)' % cfg.get('auto_run_time', '15:35'))
        return True


def stop():
    global _thread
    _stop.set()
    t, _thread = _thread, None
    if t is not None and t.is_alive():
        t.join(timeout=2)
    return True


def status(cfg=None):
    """供页面展示的运行状态"""
    cfg = cfg or config.load_config()
    st = load_state()
    now = dt.datetime.now()
    due, why = should_run(now=now, cfg=cfg, state=st)
    return {'enabled': bool(cfg.get('auto_run_enabled')),
            'time': cfg.get('auto_run_time', '15:35'),
            'alive': bool(_thread and _thread.is_alive()),
            'last_run_date': st.get('last_run_date', '-'),
            'last_run_at': st.get('last_run_at', '-'),
            'last_result': st.get('last_result', '尚未运行'),
            'last_detail': st.get('last_detail', '-'),
            'last_rows': st.get('last_rows', 0),
            'next': ('今日 %s' % cfg.get('auto_run_time')) if due or why.startswith('未到') else '-',
            'why': why}
