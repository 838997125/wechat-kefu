# -*- coding: utf-8 -*-
"""
微信驱动（wxauto4 免费版，主窗口轮询模式）。

已在微信 4.1.8.107 + wxauto4 41.x 真机验证：
  - GetSession() 读会话列表，info.isnew / new_count 为未读信号
  - 有未读（或首轮基线）才 ChatWith 切换 + GetAllMessage 读取
  - 免费版无独立子窗口能力，回复也通过主窗口 ChatWith 后 SendMsg
"""
import logging
import os
import re
import subprocess
import time
from datetime import datetime

log = logging.getLogger('kefu')


def _wechat_exe_path():
    """查找微信可执行文件路径（注册表/常见安装目录）。"""
    candidates = [
        r'D:\APPS\Weixin\Weixin.exe',
        r'C:\Program Files\Tencent\Weixin\Weixin.exe',
        r'C:\Program Files (x86)\Tencent\Weixin\Weixin.exe',
        os.path.expandvars(r'%LOCALAPPDATA%\Programs\Tencent\Weixin\Weixin.exe'),
    ]
    for p in candidates:
        if p and os.path.exists(p):
            return p
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                             r'SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall',
                             0, winreg.KEY_READ)
        i = 0
        while True:
            try:
                sub = winreg.EnumKey(key, i)
                i += 1
                try:
                    sk = winreg.OpenKey(key, sub)
                    name = winreg.QueryValueEx(sk, 'DisplayName')[0]
                    if isinstance(name, str) and name.strip() == '微信':
                        loc = winreg.QueryValueEx(sk, 'InstallLocation')[0]
                        exe = os.path.join(loc.strip('"'), 'Weixin.exe')
                        if os.path.exists(exe):
                            return exe
                except OSError:
                    pass
            except OSError:
                break
    except Exception:
        pass
    return None


def ensure_wechat_window():
    """确保微信主窗口存在：进程在就激活，进程不在/无主窗口就启动微信 exe。
    返回 True 表示已尝试确保。"""
    try:
        import psutil
        exe = _wechat_exe_path()
        wx_procs = [p for p in psutil.process_iter(['name']) if p.info['name'] and p.info['name'].lower().startswith('weixin')]
        if not wx_procs:
            # 微信进程不存在：启动
            if exe:
                log.info('微信进程不存在，自动启动微信…')
                subprocess.Popen([exe], close_fds=True)
                time.sleep(8)
                return True
            log.warning('未找到微信可执行文件，无法自动拉起')
            return False
        # 进程在：检查主窗口是否存在，无窗口则重新激活
        has_window = False
        for p in wx_procs:
            try:
                if p.info.get('num_handles', 0):
                    pass
            except Exception:
                pass
        # 简单策略：进程在但可能主窗口关闭（托盘），重新执行 exe 会唤起主窗口
        if exe:
            subprocess.Popen([exe], close_fds=True)
            time.sleep(4)
        return True
    except Exception as e:
        log.warning('自动拉起微信失败: %s', e)
        return False


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
        self._fail_streak = 0   # 连续读取失败计数（用于自愈）
        # 真实 UI 心跳间隔（秒）：低频切换窗口，避免干扰用户打字；部署专用主机可调小
        self._hb_ui_interval = float(general_cfg.get('heartbeat_ui_interval_sec', 120))
        self._last_ui_hb = 0.0
        # 连接/重连失败上限：累计失败超过此次数即放弃自动重试，等人工处理（防止反复拉起微信拖垮机器）
        self.max_connect_attempts = int(general_cfg.get('max_connect_attempts', 3))
        self.connect_attempts = 0   # 本轮连接尝试累计（成功后清零）
        self.give_up = False        # 是否已放弃自动重连，需人工重启服务

    def reset_give_up(self):
        """人工恢复后可调用以重置放弃状态（重启服务即全新实例，通常无需调用）。"""
        self.give_up = False
        self.connect_attempts = 0
        self._fail_streak = 0

    # ---- 生命周期 ----

    def start(self, retries=None):
        """连接微信。
        retries 参数已弃用（保留兼容旧调用），失败次数统一受 max_connect_attempts 控制：
        最多尝试 max_connect_attempts 次，仍连不上就置 give_up=True 并抛异常，交由人工重启，
        不再无限重试、不再反复拉起微信窗口（避免把电脑拖垮）。"""
        from wxauto4 import WeChat
        while self.connect_attempts < self.max_connect_attempts:
            self.connect_attempts += 1
            n = self.connect_attempts
            try:
                try:
                    wx = WeChat(ads=False)
                except TypeError:
                    wx = WeChat()
                self.wx = wx
                self.account = wx.GetMyInfo()
                self.ready = True
                self.give_up = False
                self.connect_attempts = 0
                self._fail_streak = 0
                self._baseline.clear()
                log.info('微信已连接: %s', self.account)
                return True
            except Exception as e:
                self.ready = False
                log.warning('微信连接失败（第%d/%d次）: %s', n, self.max_connect_attempts, str(e)[:80])
                if n >= self.max_connect_attempts:
                    break
                # 仅在第一次尝试时拉起一次微信窗口，之后不再反复操作（避免抢焦点/拖垮机器）
                if n == 1:
                    log.warning('尝试拉起微信主窗口，请确认微信 4.1.8.107 已登录、主窗口已打开…')
                    ensure_wechat_window()
                time.sleep(5)
        # 达到上限：放弃自动重试，等待人工处理
        self.ready = False
        self.give_up = True
        raise RuntimeError(
            '微信连续 %d 次连接失败，已停止自动重试，请人工确认微信已登录后重启服务。'
            % self.max_connect_attempts)

    def online(self):
        if not self.ready or self.wx is None:
            return False
        try:
            return bool(self.wx.IsOnline())
        except Exception:
            return True  # 免费版可能无此方法，保守视为在线

    def heartbeat(self, force_ui=False):
        """探测 wxauto 连接是否可用。
        平时轻量探测（不抢焦点）；失效时走 start() 重连，重连同样受 3 次上限约束，
        超限即 give_up，由 bot 主循环停止服务、等待人工重启，绝不无限重连。"""
        if self.give_up:
            return False
        if self.wx is None:
            self.ready = False
            return self._reconnect()
        now = time.time()
        do_ui = force_ui or (now - getattr(self, '_last_ui_hb', 0)) >= self._hb_ui_interval
        if not do_ui:
            if not self.ready:
                return self._reconnect()
            return True
        self._last_ui_hb = now
        try:
            self.wx.ChatWith('文件传输助手')
            time.sleep(0.3)
            info = self.wx.ChatInfo()
            if isinstance(info, dict) and info.get('chat_name'):
                self._fail_streak = 0
                self.ready = True
                return True
            raise RuntimeError('ChatInfo 无有效会话')
        except Exception as e:
            self._fail_streak += 1
            log.warning('微信心跳探测失败（连续%d次）: %s', self._fail_streak, str(e)[:60])
            self.ready = False
            return self._reconnect()

    def _reconnect(self):
        """失效后重连：复用 start() 的次数上限（不额外无限重试）。
        成功返回 True；达到上限则 give_up=True 返回 False，交由人工重启。"""
        if self.give_up:
            return False
        # 旧对象先置空；不再每轮都 ensure 窗口，避免反复抢焦点
        try:
            self.wx = None
        except Exception:
            pass
        try:
            self.start()
            return self.ready
        except Exception as ce:
            log.error('%s', str(ce)[:120])
            self.ready = False
            return False



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
            self._fail_streak = 0  # 读取成功，重置失败计数
        except Exception as e:
            self._fail_streak += 1
            log.warning('GetSession 失败（连续%d次）: %s', self._fail_streak, str(e)[:60])
            # 连续失败说明微信主窗口可能被关：标记未就绪，触发自愈
            if self._fail_streak >= 3:
                self.ready = False
        return result

    def poll(self, chats):
        """chats: [{name,type,enabled}]；返回 NormalizedMsg 列表。"""
        out = []
        if not self.ready or self.wx is None:
            return out
        unread = self._unread_map()
        for chat in chats:
            name, ctype = chat['name'], chat.get('type', 'group')
            first_pass = name not in self._baseline
            if not first_pass and not unread.get(name, False):
                continue
            raw_msgs = None
            for attempt in range(2):  # 最多切换两次，防止前台窗口没切到目标群导致串读
                try:
                    self.wx.ChatWith(name)
                    time.sleep(self._switch_wait)
                    # 校验当前激活会话确实是目标群，避免把私聊/别的群消息读串
                    if self._current_chat_matches(name):
                        raw_msgs = self.wx.GetAllMessage()
                        break
                    log.warning('切换到 %s 后当前会话不匹配，重试（第%d次）', name, attempt + 1)
                    time.sleep(self._switch_wait)
                except Exception as e:
                    log.warning('读取 %s 失败: %s', name, e)
                    self._fail_streak += 1
                    # 连续多个群读取都报 COM/UI 错误，说明连接已失效（如重新登录后监听线程崩溃）
                    if self._fail_streak >= 3:
                        log.warning('多次读取失败，判定微信连接失效，置 ready=False 等待自愈重连')
                        self.ready = False
                    break
            if raw_msgs is None:
                continue
            self._fail_streak = 0  # 成功读到消息，重置失败计数
            self._baseline.add(name)
            for m in raw_msgs:
                norm = self._normalize(name, ctype, m, is_history=first_pass)
                if norm:
                    out.append(norm)
        return out

    def _current_chat_matches(self, name):
        """校验微信当前激活的聊天窗口是否就是 name（防止窗口焦点没切过去导致读串群）。"""
        try:
            info = self.wx.ChatInfo()
            if isinstance(info, dict) and info.get('chat_name'):
                return str(info['chat_name']).strip() == name
        except Exception:
            pass
        # ChatInfo 不可用时保守放行
        return True

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
