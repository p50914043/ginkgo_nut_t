# -*- coding: utf-8 -*-
"""飞书通知：支持两种通道
1) 自定义群机器人 Webhook（可选加签）
2) 企业自建应用机器人（tenant_access_token + im/v1/messages，可私聊直达个人）
"""
import base64
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request

OPEN_BASE = 'https://open.feishu.cn/open-apis'
_token_cache = {}   # app_id -> (token, expire_at)


def _post(url, payload, headers=None, timeout=15):
    body = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, data=body,
                                 headers=dict({'Content-Type': 'application/json'}, **(headers or {})))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode('utf-8'))
        except Exception:
            return {'code': e.code, 'msg': str(e)}
    except Exception as e:
        return {'code': -1, 'msg': '%s: %s' % (type(e).__name__, e)}


def _get(url, headers=None, timeout=15):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode('utf-8'))
        except Exception:
            return {'code': e.code, 'msg': str(e)}
    except Exception as e:
        return {'code': -1, 'msg': '%s: %s' % (type(e).__name__, e)}


# ---------------- 群机器人 Webhook ----------------

def gen_sign(secret, timestamp):
    """飞书自定义机器人"加签"算法：timestamp+"\n"+secret 作 key，空消息，SHA256 后 base64"""
    key = ('%d\n%s' % (timestamp, secret)).encode('utf-8')
    digest = hmac.new(key, b'', digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode('utf-8')


def send_webhook(webhook_url, title, lines, secret='', dry_run=False):
    """发送飞书文本消息；dry_run 仅打印，未配置地址则报错不静默"""
    text = title + '\n' + '\n'.join(lines)
    if dry_run:
        print('[通知-dry run]\n' + text)
        return {'code': 0, 'msg': 'dry_run', 'text': text}
    if not webhook_url:
        return {'code': -1, 'msg': '未配置飞书群机器人 Webhook', 'channel': 'webhook'}
    payload = {'msg_type': 'text', 'content': {'text': text}}
    if secret:
        ts = int(time.time())
        payload.update(timestamp=ts, sign=gen_sign(secret, ts))
    out = _post(webhook_url, payload)
    out.setdefault('channel', 'webhook')
    return out


# ---------------- 企业自建应用机器人 ----------------

def get_tenant_access_token(app_id, app_secret):
    """获取 tenant_access_token（进程内缓存，提前 5 分钟过期）"""
    if not app_id or not app_secret:
        return None, {'code': -1, 'msg': 'app_id/app_secret 未配置'}
    cached, exp = _token_cache.get(app_id, (None, 0))
    if cached and time.time() < exp:
        return cached, {'code': 0, 'msg': 'cached'}
    out = _post(OPEN_BASE + '/auth/v3/tenant_access_token/internal',
                {'app_id': app_id, 'app_secret': app_secret})
    if out.get('code') != 0:
        return None, out
    _token_cache[app_id] = (out['tenant_access_token'], time.time() + int(out.get('expire', 7200)) - 300)
    return out['tenant_access_token'], out


def app_send_text(app_id, app_secret, receive_id, title, lines,
                  id_type='open_id', dry_run=False):
    """应用机器人发消息（receive_id 可为 open_id / user_id / union_id / chat_id）"""
    text = title + '\n' + '\n'.join(lines)
    if dry_run:
        print('[通知-dry run]\n' + text)
        return {'code': 0, 'msg': 'dry_run', 'text': text}
    token, err = get_tenant_access_token(app_id, app_secret)
    if not token:
        err.setdefault('channel', 'app_bot')
        return err
    url = '%s/im/v1/messages?receive_id_type=%s' % (OPEN_BASE, id_type)
    out = _post(url, {'receive_id': receive_id, 'msg_type': 'text',
                      'content': json.dumps({'text': text}, ensure_ascii=False)},
                {'Authorization': 'Bearer ' + token})
    out.setdefault('channel', 'app_bot')
    return out


def app_list_chats(app_id, app_secret):
    """列出机器人已加入的群聊，便于获取 chat_id"""
    token, err = get_tenant_access_token(app_id, app_secret)
    if not token:
        return err
    out = _get(OPEN_BASE + '/im/v1/chats?page_size=100', {'Authorization': 'Bearer ' + token})
    if out.get('code') != 0:
        return out
    return {'code': 0, 'chats': [{'chat_id': c.get('chat_id'), 'name': c.get('name')}
                                 for c in (out.get('data') or {}).get('items', [])]}


def app_resolve_user_id(app_id, app_secret, mobiles=None, emails=None, id_type='open_id'):
    """手机号/邮箱 -> 用户 id（需应用开通"通过手机号或邮箱获取用户ID"权限）"""
    token, err = get_tenant_access_token(app_id, app_secret)
    if not token:
        return err
    url = '%s/contact/v3/users/batch_get_id?user_id_type=%s' % (OPEN_BASE, id_type)
    out = _post(url, {'mobiles': mobiles or [], 'emails': emails or []},
                {'Authorization': 'Bearer ' + token})
    if out.get('code') != 0:
        return out
    users = [{'input': u.get('mobile') or u.get('email'),
              'user_id': u.get('user_id'), 'name': u.get('name')}
             for u in ((out.get('data') or {}).get('user_list') or [])]
    return {'code': 0, 'users': users}


# ---------------- 统一入口 ----------------

def push(title, lines, cfg, dry_run=False, force_channel=None):
    """按配置自动选择通道：优先应用机器人，退回群机器人 Webhook"""
    channel = force_channel or cfg.get('notify_channel', 'auto')
    app_id = (cfg.get('feishu_app_id') or '').strip()
    app_secret = (cfg.get('feishu_app_secret') or '').strip()
    receive_id = (cfg.get('feishu_receive_id') or '').strip()
    wh = (cfg.get('feishu_webhook') or '').strip()

    want_app = channel in ('auto', 'app_bot') and app_id and receive_id
    if want_app or channel == 'app_bot':
        if not (app_id and app_secret and receive_id):
            return {'code': -1, 'msg': '应用机器人配置不全(app_id/app_secret/receive_id)', 'channel': 'app_bot'}
        return app_send_text(app_id, app_secret, receive_id, title, lines,
                             cfg.get('feishu_receive_id_type', 'open_id'), dry_run)
    if channel in ('auto', 'webhook'):
        return send_webhook(wh, title, lines, (cfg.get('feishu_webhook_secret') or '').strip(), dry_run)
    return {'code': -1, 'msg': '未知通道 %s' % channel}


def describe_channel(cfg):
    """返回当前生效通道的中文描述，供页面展示"""
    app_ok = bool(cfg.get('feishu_app_id') and cfg.get('feishu_app_secret') and cfg.get('feishu_receive_id'))
    wh_ok = bool(cfg.get('feishu_webhook'))
    ch = cfg.get('notify_channel', 'auto')
    if ch == 'app_bot' or (ch == 'auto' and app_ok):
        return '应用机器人(私聊/群聊)' if app_ok else '应用机器人(配置不全)'
    if ch == 'webhook' or (ch == 'auto' and wh_ok):
        return '群机器人Webhook' + ('+加签' if cfg.get('feishu_webhook_secret') else '')
    return '未配置(仅dry-run)'


def _chg(v):
    """涨跌幅统一写法：+1.20% / -0.80% / 无数据时空串"""
    if v is None:
        return ''
    return ('+%.2f%%' % v) if v >= 0 else ('%.2f%%' % v)


def _tip_line(x):
    """提示行：标记 + 一句话纪律 + 关键价位 + 为何这么提示（全程不出现仓位与金额）"""
    t = x.get('tip') or {}
    seg = []
    if t.get('pctb') is not None:
        seg.append('%%B %.2f' % t['pctb'])
    if t.get('stop_price') is not None:
        seg.append('硬止损位 %.2f' % t['stop_price'])
    if t.get('hold_days'):
        seg.append('第 %d/%d 日' % (t['hold_days'], t.get('max_hold') or 0))
    if t.get('action') in ('watch', 'bull_wait') and t.get('ma20') is not None:
        seg.append('MA20 %s · 量比 %s×' % (
            _px(t.get('ma20')), ('%.2f' % t['vol_ratio']) if t.get('vol_ratio') is not None else '-'))
    tail = (' ⚠️ ' + t['risk']) if t.get('risk') else ''
    return '   %s %s｜%s %s｜%s｜%s%s' % (
        x['name'], x['code'], t.get('mark', ''), t.get('action_cn', ''),
        ' · '.join(seg) or '-', t.get('reason', ''), tail)


def format_trend_message(date_str, trend, title_tag='趋势播报'):
    """盘后飞书播报：趋势概览 → 逐只行情+三态(强/弱) → 分级切换提醒 → 区间做T提示 → 酝酿中。

    trend 是 scan.build_trend_report 的结果。描述行情与分级提示，不给仓位与金额。
    """
    trend = trend or {}
    lines = ['%s %s｜盘后生成 · 只做行情识别与区间纪律提示，不接管下单' % (date_str, title_tag)]
    rows = trend.get('rows') or []
    lines.append('📊 趋势：%d 多头 · %d 空头 · %d 震荡（共 %d 只已判定，其中强趋势 %d 只）' % (
        trend.get('n_bull', 0), trend.get('n_bear', 0), trend.get('n_range', 0), len(rows),
        trend.get('n_strong_rows', 0)))
    if trend.get('n_tip'):
        lines.append('🎯 提示：%d 只到动作（🟢 买入 %d · 🔺 卖出 %d · 🛑 止损/回避 %d · 🟠 多头回踩 %d）' % (
            trend.get('n_tip', 0), trend.get('n_tip_buy', 0), trend.get('n_tip_sell', 0),
            trend.get('n_tip_stop', 0), trend.get('n_tip_watch', 0)))
    for x in rows:
        sg = x.get('segment') or {}
        span = ('本波段自 %s，第 %d 日' % (sg['start'], x['days'])) if sg.get('start') \
            else ('已持续 %d 日' % x['days'])
        ma = x.get('ma') or {}
        ind = x.get('ind') or {}
        extra = ''
        if x.get('kind') == 'range' and ind.get('pctb') is not None:
            extra = ' · 区间位置%%B %.2f' % ind['pctb']
        adx = x.get('adx')
        lines.append('   %s %s 收盘 %.2f%s ｜ %s ｜ %s ｜ MA20 %s / 慢线 %s · 斜率 %s%s%s' % (
            x['name'], x['code'], x['close'] or 0.0, _chg(x['chg_pct']),
            x.get('level_cn') or x['label_cn'], span,
            _px(ma.get('mid')), _px(ma.get('slow')),
            ('%+.2f%%' % ma['slope5']) if ma.get('slope5') is not None else '-',
            (' · ADX %.1f' % adx) if adx is not None else '', extra))
    strong = trend.get('strong_changed') or []
    momentum = trend.get('momentum_changed') or []
    changed = trend.get('changed') or []
    if strong:
        lines.append('🔴🔴 强信号｜震荡 → 趋势（历史未来 10 日有效性最高，已确认切换）')
        for x in strong:
            lines.append('   %s %s：%s → %s（%s）%s' % (
                x['name'], x['code'], x['prev_cn'], x['label_cn'], x['desc'],
                '' if x.get('strong') else '·当前偏弱'))
    if momentum:
        lines.append('🟡 动能变化｜趋势 → 震荡（方向性弱，只是减速信号）')
        for x in momentum:
            lines.append('   %s %s：%s → %s（%s）' % (
                x['name'], x['code'], x['prev_cn'], x['label_cn'], x['desc']))
    other = [x for x in changed
             if (x.get('grade') or {}).get('grade') not in ('strong', 'momentum')]
    if other:
        lines.append('⚪ 其它切换')
        for x in other:
            lines.append('   %s %s：%s → %s（%s）' % (
                x['name'], x['code'], x['prev_cn'], x['label_cn'], x['desc']))
    if not changed and rows:
        lines.append('今日无趋势变化：%d 只标的延续各自波段' % len(rows))
    tip_rows = trend.get('tips') or []
    if tip_rows:
        lines.append('🎯 提示｜第一纪律：趋势段停手（只给纪律提示，不接管下单）')
        for x in tip_rows:
            lines.append(_tip_line(x))
    hold_rows = trend.get('holds') or []
    if hold_rows:
        lines.append('🔵 区间持仓中（按上下沿与止损纪律走）')
        for x in hold_rows:
            lines.append(_tip_line(x))
    pending = trend.get('pending') or []
    if pending:
        lines.append('⏳ 酝酿中（新状态还没凑满确认天数，今日口径不变）')
        for x in pending:
            lines.append('   %s %s：当前%s，原始判定已转%s（连续 %d/%d 日）' % (
                x['name'], x['code'], x['label_cn'], x['pending_cn'],
                x['pending_days'], x['confirm_days']))
    quiet = trend.get('quiet') or []
    if quiet:
        lines.append('--- 未纳入判定：%s ---' % '、'.join(
            '%s(%s)' % (q['name'], q.get('status') or '-') for q in quiet))
    return lines


def _px(v):
    return ('%.2f' % v) if v else '-'
