# -*- coding: utf-8 -*-
"""
微信驱动（wxauto4 免费版，主窗口轮询模式）。

已在微信 4.1.8.107 + wxauto4 41.x 真机验证：
  - GetSession() 读会话列表，info.isnew / new_count 为未读信号
  - 有未读（或首轮基线）才 ChatWith 切换 + GetAllMessage 读取
  - 免费版无独立子窗口能力，回复也通过主窗口 ChatWith 后 SendMsg
"""
import logging
import re
import time
from datetime import datetime

log = logging.getLogger('kefu')


class NormalizedMsg:
    def __init__(self, chat, chat_type, sender, attr, mtype, content, is_history=False):
        self.chat = chat
        self.chat_type = chat_type
        self.sender = sender or ''
        self.attr = attr            # self / friend / system
        self.mtype = mtype          # text / quote / image / system ...
        self.content = content or ''
        self.is_history = is_history
        self.fp = '|'.join([chat, attr, mtype, self.sender, self.content.strip()])
        self.ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    def as_dict(self):
        return {'ts': self.ts, 'chat': self.chat, 'chat_type': self.chat_type,
                'sender': self.sender, 'attr': self.attr, 'mtype': self.mtype,
                'content': self.content, 'fp': self.fp}


_WELCOME_PATTERNS = [
    re.compile(r'^(?P<names>.+?)(?:加入了?群聊|加入群聊|通过扫描.*?二维码加入群聊)'),
]


def parse_welcome_names(content):
    """从系统消息解析新进群成员昵称，如 '<己方客服E>, <己方客服H>  加入群聊' -> ['<己方客服E>','<己方客服H>']。"""
    text = (content or '').strip()
    for pat in _WELCOME_PATTERNS:
        m = pat.match(text)
        if m:
            raw = m.group('names')
            # 分隔符可能是逗号、顿号、空格
            parts = re.split(r'[,，、\s]+', raw)
            return [p.strip() for p in parts if p.strip()]
    return []


class WxDriver:
    def __init__(self, general_cfg):
        self.general = general_cfg
        self.wx = None
        self.ready = False
        self.account = None
        self._baseline = set()
        self._switch_wait = float(general_cfg.get('switch_wait_sec', 1.5))

    # ---- 生命周期 ----

    def start(self, retries=999):
        """连接微信；微信未就绪时持续重试（面板可看到状态）。"""
        from wxauto4 import WeChat
        attempt = 0
        while True:
            attempt += 1
            try:
                try:
                    wx = WeChat(ads=False)
                except TypeError:
                    wx = WeChat()
                self.wx = wx
                self.account = wx.GetMyInfo()
                self.ready = True
                self._baseline.clear()
                log.info('微信已连接: %s', self.account)
                return True
            except Exception as e:
                self.ready = False
                if attempt >= retries:
                    raise
                log.warning('等待微信客户端就绪（%s），请确认微信 4.1.8.107 已登录且主窗口已打开… 第%d次重试',
                            str(e)[:80], attempt)
                time.sleep(5)

    def online(self):
        if not self.ready or self.wx is None:
            return False
        try:
            return bool(self.wx.IsOnline())
        except Exception:
            return True  # 免费版可能无此方法，保守视为在线

    # ---- 读取 ----

    def scan_sessions(self):
        """面板用：返回当前微信会话列表 [{name, content, time, isnew, new_count, ismute}]。"""
        if not self.ready:
            return []
        out = []
        try:
            for s in self.wx.GetSession():
                info = getattr(s, 'info', {}) or {}
                out.append({
                    'name': getattr(s, 'name', '') or info.get('name', ''),
                    'content': info.get('content', ''),
                    'time': info.get('time', ''),
                    'isnew': bool(info.get('isnew')),
                    'new_count': int(info.get('new_count') or 0),
                    'ismute': bool(info.get('ismute')),
                })
        except Exception as e:
            log.warning('scan_sessions 失败: %s', e)
        return out

    def _unread_map(self):
        result = {}
        try:
            for s in self.wx.GetSession():
                info = getattr(s, 'info', {}) or {}
                name = getattr(s, 'name', '') or info.get('name', '')
                if name:
                    result[name] = bool(info.get('isnew')) or int(info.get('new_count') or 0) > 0
        except Exception as e:
            log.warning('GetSession 失败: %s', e)
        return result

    def poll(self, chats):
        """chats: [{name,type,enabled}]；返回 NormalizedMsg 列表。"""
        out = []
        unread = self._unread_map()
        for chat in chats:
            name, ctype = chat['name'], chat.get('type', 'group')
            first_pass = name not in self._baseline
            if not first_pass and not unread.get(name, False):
                continue
            try:
                self.wx.ChatWith(name)
                time.sleep(self._switch_wait)
                raw_msgs = self.wx.GetAllMessage()
            except Exception as e:
                log.warning('读取 %s 失败: %s', name, e)
                continue
            self._baseline.add(name)
            for m in raw_msgs:
                norm = self._normalize(name, ctype, m, is_history=first_pass)
                if norm:
                    out.append(norm)
        return out

    def _normalize(self, name, ctype, m, is_history=False):
        attr = getattr(m, 'attr', '') or ''
        mtype = getattr(m, 'type', '') or ''
        if mtype == 'time':
            return None
        sender = getattr(m, 'sender', '') or ''
        content = str(getattr(m, 'content', '') or '')
        if attr == 'system':
            return NormalizedMsg(name, ctype, 'system', 'system', 'system', content, is_history)
        if ctype == 'dm' and attr == 'friend':
            sender = name
        return NormalizedMsg(name, ctype, sender, attr, mtype, content, is_history)

    # ---- 发送 ----

    def send(self, name, text, at=None):
        """发送文本；at: None / 'asker' 已由上层解析为昵称列表 / list[str]。"""
        try:
            self.wx.ChatWith(name)
            time.sleep(0.8)
            kwargs = {}
            if at:
                kwargs['at'] = at
            resp = self.wx.SendMsg(text, **kwargs)
            if resp is not None and not bool(resp):
                log.warning('SendMsg 返回失败: %s', resp)
                return False
            return True
        except Exception as e:
            log.error('发送到 %s 失败: %s', name, e)
            return False
