# -*- coding: utf-8 -*-
"""
一次性历史回补：对指定客服群，读取当前微信窗口已加载的消息，
去重入库后按最新路由规则补算“影子转发日志”（不发送任何消息）。
用法：python backfill_group.py [群名]
注意：会短暂把微信前台切到目标群。建议机器人暂停时运行，避免抢焦点。
"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

from app.config import Config
from app.storage import Storage
from app import intention
from app import router as route_engine

ROOT = os.path.dirname(os.path.abspath(__file__))
GROUP = sys.argv[1] if len(sys.argv) > 1 else '聚好麦【西帕医药旗舰店-物流'

cfg = Config(os.path.join(ROOT, 'config.json'))
storage = Storage(os.path.join(ROOT, 'data', 'messages.db'))
rcfg = cfg.data.get('routing', {})
own_staff = rcfg.get('own_staff', ['<公司简称>'])
routes = rcfg.get('routes', [])
llm_cfg = rcfg.get('llm_intent', {})

ZHONGTONG = '药商通c端中通快递沟通群'

# ---- 1. 连接微信读取目标群当前窗口消息 ----
from wxauto4 import WeChat
wx = WeChat(ads=False)
print('已连接微信，切换到群:', GROUP)
wx.ChatWith(GROUP)
time.sleep(2.0)
info = wx.ChatInfo()
if not (isinstance(info, dict) and info.get('chat_name') == GROUP):
    print('!! 切换后当前会话不是目标群:', info, '，中止避免串群')
    sys.exit(1)
raw = wx.GetAllMessage()
print('窗口读到消息条数:', len(raw))

# ---- 2. 去重入库（用 content+sender+ts 粗去重）----
from app.driver import WxDriver  # noqa 仅借用 _normalize 不方便，这里直接构造
# 直接用 driver 实例的 _normalize
drv = WxDriver(cfg.general())
drv.wx = wx
added = 0
for m in raw:
    norm = drv._normalize(GROUP, 'group', m, is_history=True)
    if not norm or norm.mtype in ('time', 'system'):
        continue
    # 粗去重：同群同发送人同内容已存在则跳过
    exists = storage.conn.execute(
        "SELECT 1 FROM messages WHERE chat=? AND sender=? AND content=? LIMIT 1",
        (GROUP, norm.sender, norm.content)).fetchone()
    if exists:
        continue
    md = {'ts': norm.ts, 'chat': norm.chat, 'chat_type': norm.chat_type,
          'sender': norm.sender, 'attr': norm.attr, 'mtype': norm.mtype,
          'content': norm.content, 'fp': norm.fp}
    if storage.record(md):
        added += 1
print('新增入库消息:', added)

# ---- 3. 对该群所有“外部客服”历史消息补算影子路由 ----
def templates():
    tmpl = {}
    for rt in routes:
        t = rt.get('target', '')
        if ZHONGTONG in t: tmpl.setdefault('B', rt)
        elif '<物流方>中通&<公司简称><公司简称>' in t: tmpl.setdefault('C', rt)
        elif 'C端审单发货售后' in t: tmpl.setdefault('D', rt)
    return tmpl
tmpl = templates()

rows = storage.conn.execute(
    "SELECT ts,sender,mtype,content FROM messages WHERE chat=? AND attr='friend' AND mtype IN ('text','quote') ORDER BY id",
    (GROUP,)).fetchall()

logged = 0
for ts, sender, mtype, content in rows:
    # 跳过己方
    if route_engine.is_own_staff(sender, own_staff):
        continue
    res, src = intention.classify(sender, content, own_staff, llm_cfg,
                                  intention.IntentStore(storage), is_robot=False,
                                  source_role='customer_src')
    for letter in [t for t in (res or []) if t in ('B', 'C', 'D')]:
        rt = tmpl.get(letter)
        if not rt:
            continue
        tracking = route_engine.extract_tracking_no(content)
        allnums = route_engine.extract_all_tracking(content)
        fwd = route_engine.build_forward(rt, content, sender, tracking,
                                         requester='', all_numbers=allnums)
        if fwd is None:
            continue
        dedup = route_engine.route_dedup_key(rt, content, all_numbers=allnums)
        if storage.route_log_exists(rt.get('id'), dedup, True):
            continue
        target = rt.get('target')
        storage.route_log({
            'ts': ts, 'route_name': rt.get('id'), 'source_chat': GROUP,
            'target_chat': target, 'sender': sender, 'tracking_no': tracking,
            'mode': rt.get('mode'), 'shadow': True,
            'original': dedup, 'forward': fwd, 'status': 'shadow'})
        print(f'  [影子·{src}] {ts[11:16]} -> {target[:12]} | {fwd[:40]}')
        logged += 1
print('补算影子路由日志:', logged, '条')
print('完成。')
