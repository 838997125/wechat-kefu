# -*- coding: utf-8 -*-
"""用全部历史消息 + 当前路由规则，重新生成影子转发日志。
清空旧 route_logs，按消息时间顺序逐条回放路由预判（不发送任何消息）。"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding='utf-8')

from app.config import Config
from app.storage import Storage
from app import router as R

BASE = os.path.dirname(os.path.abspath(__file__))
cfg = Config(os.path.join(BASE, 'config.json'))
st = Storage(os.path.join(BASE, 'data', 'messages.db'))

rcfg = cfg.data.get('routing', {})
routes = rcfg.get('routes', []) or []
own = rcfg.get('own_staff', ['<公司简称>'])
shadow = True  # 始终以影子模式回放

# 1) 清空旧路由日志
with st._lock:
    st.conn.execute('DELETE FROM route_logs')
    st.conn.commit()
print('已清空旧转发日志')

# 2) 读取全部消息（按时间正序），模拟实时路由
with st._lock:
    rows = st.conn.execute(
        "SELECT ts,chat,sender,attr,mtype,content FROM messages WHERE attr='friend' ORDER BY id ASC"
    ).fetchall()

generated = 0
matched = []
matched_keys = []
for ts, chat, sender, attr, mtype, content in rows:
    if mtype not in ('text', 'quote'):
        continue
    # 己方客服消息不触发转发（<回写机器人名>除外）
    if R.is_own_staff(sender, own) and '<回写机器人名>' not in (sender or ''):
        continue
    rt = R.match_route(routes, chat, sender, content or '', mtype)
    if not rt:
        continue
    tracking = R.extract_tracking_no(content or '')
    all_numbers = R.extract_all_tracking(content or '')
    dedup_key = R.route_dedup_key(rt, content, all_numbers=all_numbers)
    # 按 dedup_key 去重（同一批单号只记一次）
    if any(prev['key'] == dedup_key and prev['rid'] == rt.get('id') for prev in matched_keys):
        continue
    requester = ''
    if rt.get('at_requester') and tracking:
        requester = st.find_sender_by_tracking(rt.get('target'), tracking, own_staff=own)
    forward = R.build_forward(rt, content, sender, tracking, requester=requester, all_numbers=all_numbers)
    if forward is None:
        continue
    rid = rt.get('id', rt.get('name', ''))
    st.route_log({
        'ts': ts, 'route_name': rid, 'source_chat': chat,
        'target_chat': rt.get('target'), 'sender': sender, 'tracking_no': tracking,
        'mode': rt.get('mode'), 'shadow': shadow,
        'original': dedup_key, 'forward': forward, 'status': 'shadow'})
    generated += 1
    matched_keys.append({'rid': rid, 'key': dedup_key})
    matched.append((ts, rt.get('name'), chat, rt.get('target'), sender, tracking, forward[:50]))

print(f'\n回放完成：共扫描 {len(rows)} 条消息，命中路由 {generated} 条\n')
for ts, name, src, tgt, sender, no, fwd in matched:
    print(f'[{ts[11:16]}] {name}')
    print(f'   {sender} | 单号 {no or "无"}')
    print(f'   {src}  →  {tgt}')
    print(f'   转发: {fwd}')
    print()
