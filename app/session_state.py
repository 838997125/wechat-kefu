# -*- coding: utf-8 -*-
"""Windows 锁屏检测（仅 Windows，ctypes 调 WTSAPI32，无第三方依赖）。

判定口径（关键，避免在“无交互会话/服务方式运行”的专用主机上误报）：
  WTSConnectState==Active(0) 且 WTSSessionState==Locked(0) 才认定为“用户已锁屏”。
  当会话是 Disconnected(4，如 RDP 断开后仍以服务/无人值守方式运行) 时，WTSSessionState
  也可能返回 0，但这不是“锁屏阻断 UI”，不应报警——此时是否真读不到消息由 stale 检测兜底。

锁屏后微信 4.x 的 UI Automation 控件树停止渲染，wxauto 会“控件失效/读空”，需要人工解锁。
"""
import ctypes
from ctypes import wintypes

WTS_CURRENT_SERVER_HANDLE = 0
WTS_CURRENT_SESSION = -1
WTSSessionId = 12          # WTS_INFO_CLASS
WTSConnectClass = 8        # -> WTS_CONNECTSTATE_CLASS
WTS_SESSIONSTATE = 24      # -> WTS_SESSIONSTATE (0=Locked, 1=Unlocked)

WTSActive = 0
WTSConnected = 1
WTSDisconnected = 4
WTS_SESSIONSTATE_LOCK = 0


class _SessionState(ctypes.Structure):
    _fields_ = [('State', wintypes.INT), ('Flag', wintypes.INT)]


def _api():
    wts = ctypes.WinDLL('Wtsapi32.dll')
    wts.WTSQuerySessionInformationW.restype = wintypes.BOOL
    wts.WTSQuerySessionInformationW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.c_int,
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD)]
    wts.WTSFreeMemory.restype = None
    wts.WTSFreeMemory.argtypes = [ctypes.c_void_p]
    return wts


def _query_uint(wts, session_id, info_class):
    buf = ctypes.c_void_p()
    size = wintypes.DWORD()
    ok = wts.WTSQuerySessionInformationW(
        WTS_CURRENT_SERVER_HANDLE, session_id, info_class,
        ctypes.byref(buf), ctypes.byref(size))
    if not ok or not buf.value:
        return None
    try:
        return int(ctypes.cast(buf, ctypes.POINTER(wintypes.INT)).contents.value)
    finally:
        wts.WTSFreeMemory(buf)


def session_info():
    """返回 (connect_state, session_state, session_id)；无法获取返回 None。"""
    try:
        wts = _api()
        sid_buf = ctypes.c_void_p()
        sid_size = wintypes.DWORD()
        if not wts.WTSQuerySessionInformationW(
                WTS_CURRENT_SERVER_HANDLE, WTS_CURRENT_SESSION, WTSSessionId,
                ctypes.byref(sid_buf), ctypes.byref(sid_size)) or not sid_buf.value:
            return None
        try:
            sid = int(ctypes.cast(sid_buf, ctypes.POINTER(wintypes.DWORD)).contents.value)
        finally:
            wts.WTSFreeMemory(sid_buf)
        connect_state = _query_uint(wts, sid, WTSConnectClass)
        # 查锁定状态
        lock_buf = ctypes.c_void_p()
        lock_size = wintypes.DWORD()
        session_state = None
        if wts.WTSQuerySessionInformationW(
                WTS_CURRENT_SERVER_HANDLE, sid, WTS_SESSIONSTATE,
                ctypes.byref(lock_buf), ctypes.byref(lock_size)) and lock_buf.value:
            try:
                session_state = int(
                    ctypes.cast(lock_buf, ctypes.POINTER(_SessionState)).contents.State)
            finally:
                wts.WTSFreeMemory(lock_buf)
        return connect_state, session_state, sid
    except Exception:
        return None


def is_session_locked():
    """仅当“活跃交互会话被锁定”才返回 True。

    会话断开（无人值守/服务方式，ConnectState=Disconnected）不算锁屏，避免在
    本来就无桌面登录的专用主机上持续误报红色；这种场景由 stale（长时间无有效消息）兜底。
    """
    info = session_info()
    if not info:
        return False
    connect_state, session_state, _sid = info
    # 必须是当前活跃（或已连接）的交互会话且处于锁定态；断开的会话不计锁屏
    if connect_state not in (WTSActive, WTSConnected):
        return False
    return session_state == WTS_SESSIONSTATE_LOCK
