# -*- coding: utf-8 -*-
"""Windows 锁屏/会话状态检测（仅 Windows，ctypes 调 WTSAPI32，无第三方依赖）。

锁屏后微信 4.x 的 UI Automation 控件树停止渲染，wxauto 会出现“控件失效/读到空/只有系统撤回通知”，
机器人因此假活着却收不到消息。这里直接检测当前会话是否处于锁屏，供面板/托盘告警。
"""
import ctypes
from ctypes import wintypes


def is_session_locked():
    """返回 True 表示当前 Windows 会话处于锁屏状态。无法判断时返回 False（保守）。"""
    try:
        WTS_SESSIONSTATE_LOCK = 0
        WTS_CURRENT_SERVER_HANDLE = 0
        WTS_CURRENT_SESSION = -1

        class WTS_INFO_CHUNK(ctypes.Structure):
            _fields_ = [('State', wintypes.INT), ('Flag', wintypes.INT)]

        WTSAPI32 = ctypes.WinDLL('Wtsapi32.dll')
        WTSAPI32.WTSQuerySessionInformationW.restype = wintypes.BOOL
        WTSAPI32.WTSQuerySessionInformationW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, ctypes.c_int,
            ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD)]
        WTSAPI32.WTSFreeMemory.restype = None
        WTSAPI32.WTSFreeMemory.argtypes = [ctypes.c_void_p]

        WTS_SessionState = 24  # WTSSessionInfoClass -> WTS_SESSIONSTATE
        buf = ctypes.c_void_p()
        size = wintypes.DWORD()
        ok = WTSAPI32.WTSQuerySessionInformationW(
            WTS_CURRENT_SERVER_HANDLE, WTS_CURRENT_SESSION, WTS_SessionState,
            ctypes.byref(buf), ctypes.byref(size))
        if not ok or not buf.value:
            return False
        try:
            state = ctypes.cast(buf, ctypes.POINTER(WTS_INFO_CHUNK)).contents.State
            return int(state) == WTS_SESSIONSTATE_LOCK
        finally:
            WTSAPI32.WTSFreeMemory(buf)
    except Exception:
        return False
