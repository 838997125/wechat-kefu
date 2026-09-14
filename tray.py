# -*- coding: utf-8 -*-
"""
客服微信助手 · 系统托盘程序（无黑色控制台窗口）
- 后台以隐藏窗口方式拉起/守护 run.py（机器人 + Web 面板）
- 托盘图标颜色表示状态：绿=运行中  黄=已暂停  红=微信未连接/需人工  灰=启动中
- 右键菜单：打开面板 / 暂停 / 恢复 / 重启服务 / 停止服务 / 开机自启 / 退出
用 pythonw.exe 运行（启动托盘.vbs 已隐藏窗口）。
"""
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
import webbrowser

try:
    import pystray
    from PIL import Image, ImageDraw
except Exception:
    # 缺依赖时给出可诊断的提示（写日志，不静默崩溃）
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs', 'tray-error.txt'), 'a', encoding='utf-8') as f:
        import traceback
        f.write('缺少 pystray/Pillow:\n' + traceback.format_exc())
    raise

ROOT = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get('KEFU_PORT', '43991'))
BASE = f'http://127.0.0.1:{PORT}'
CREATE_NO_WINDOW = 0x08000000

# 颜色
_GREEN = (7, 193, 96)
_YELLOW = (250, 173, 20)
_RED = (245, 63, 63)
_GRAY = (150, 150, 150)
_BLUE = (22, 119, 255)


def python_exe():
    """优先用项目自带 venv 的 pythonw（无控制台）。"""
    cand = [
        os.path.join(ROOT, '.venv', 'Scripts', 'pythonw.exe'),
        os.path.join(ROOT, '.venv', 'Scripts', 'python.exe'),
    ]
    for p in cand:
        if os.path.exists(p):
            return p
    return sys.executable


class TrayApp:
    def __init__(self):
        self.proc = None
        self.tunnel_proc = None
        self.tunnel_url = ''
        self.tunnel_log = os.path.join(ROOT, 'logs', 'tunnel.log')
        self.state = {'key': 'gray', 'text': '启动中…', 'paused': False, 'give_up': False}
        self.quitting = False
        self.startup_vbs = os.path.join(
            os.environ.get('APPDATA', ''), r'Microsoft\Windows\Start Menu\Programs\Startup',
            '客服微信助手托盘.vbs')

    # ---------- Cloudflare 隧道 ----------
    def _cloudflared(self):
        # 包根/bin/cloudflared.exe（部署结构）或开发目录上级
        cands = [
            os.path.join(os.path.dirname(ROOT), 'bin', 'cloudflared.exe'),
            os.path.join(ROOT, 'bin', 'cloudflared.exe'),
        ]
        for p in cands:
            if os.path.exists(p):
                return p
        return None

    def tunnel_running(self, item=None):
        return bool(self.tunnel_proc and self.tunnel_proc.poll() is None)

    def toggle_tunnel(self, icon=None, item=None):
        if self.tunnel_running():
            self.stop_tunnel()
        else:
            self.start_tunnel()

    def start_tunnel(self):
        exe = self._cloudflared()
        if not exe:
            self.state['text'] = '未找到 cloudflared.exe'
            return
        os.makedirs(os.path.dirname(self.tunnel_log), exist_ok=True)
        logf = open(self.tunnel_log, 'ab')
        self.tunnel_proc = subprocess.Popen(
            [exe, 'tunnel', '--url', 'http://127.0.0.1:%d' % PORT, '--no-autoupdate'],
            cwd=os.path.dirname(exe), stdout=logf, stderr=subprocess.STDOUT,
            creationflags=CREATE_NO_WINDOW, close_fds=True)
        threading.Thread(target=self._read_tunnel_url, daemon=True).start()

    def _read_tunnel_url(self):
        """从 cloudflared 输出解析 https://xxxx.trycloudflare.com。"""
        import re as _re
        deadline = time.time() + 40
        while time.time() < deadline and self.tunnel_running():
            try:
                if os.path.exists(self.tunnel_log):
                    with open(self.tunnel_log, 'rb') as f:
                        data = f.read().decode('utf-8', 'ignore')
                    m = _re.search(r'https://[a-z0-9-]+\.trycloudflare\.com', data)
                    if m and not self.tunnel_url:
                        self.tunnel_url = m.group(0)
                        try:
                            with open(os.path.join(os.path.dirname(self.tunnel_log), 'tunnel-url.txt'),
                                      'w', encoding='utf-8') as f:
                                f.write(self.tunnel_url)
                        except Exception:
                            pass
                        return
            except Exception:
                pass
            time.sleep(1.5)

    def stop_tunnel(self):
        if self.tunnel_proc and self.tunnel_proc.poll() is None:
            try:
                self.tunnel_proc.terminate()
                for _ in range(15):
                    if self.tunnel_proc.poll() is not None:
                        break
                    time.sleep(0.2)
                if self.tunnel_proc.poll() is None:
                    self.tunnel_proc.kill()
            except Exception:
                pass
        self.tunnel_url = ''

    def copy_tunnel_url(self, icon=None, item=None):
        if not self.tunnel_url:
            return
        try:
            import subprocess as _sp
            _sp.Popen(['clip'], stdin=_sp.PIPE).communicate(self.tunnel_url.encode('utf-8'))
        except Exception:
            pass

    def tunnel_label(self, item):
        if self.tunnel_running():
            return '公网隧道：停止' + ('（地址已复制就绪）' if self.tunnel_url else '（启动中…）')
        return '公网隧道：启动'

    def tunnel_url_label(self, item):
        return '复制公网地址' if self.tunnel_url else '公网地址（启动后可用）'

    # ---------- 子进程管理 ----------
    def start_service(self):
        if self.proc and self.proc.poll() is None:
            return
        logs = os.path.join(ROOT, 'logs')
        os.makedirs(logs, exist_ok=True)
        logf = open(os.path.join(logs, 'service.out.log'), 'ab')
        flags = CREATE_NO_WINDOW
        self.proc = subprocess.Popen(
            [python_exe(), os.path.join(ROOT, 'run.py')],
            cwd=ROOT, stdout=logf, stderr=subprocess.STDOUT,
            creationflags=flags, close_fds=True)

    def stop_service(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                for _ in range(20):
                    if self.proc.poll() is not None:
                        break
                    time.sleep(0.2)
                if self.proc.poll() is None:
                    self.proc.kill()
            except Exception:
                pass

    def restart_service(self, icon=None, item=None):
        self.stop_service()
        time.sleep(0.8)
        self.start_service()

    # ---------- HTTP ----------
    def _api(self, path, method='GET'):
        req = urllib.request.Request(BASE + path, method=method)
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read().decode('utf-8'))

    def poll_status(self):
        while not self.quitting:
            try:
                if self.proc and self.proc.poll() is None:
                    s = self._api('/api/status')
                    if s.get('give_up'):
                        self.state = {'key': 'red', 'text': '微信连接失败，需人工登录后重启服务',
                                      'paused': s.get('paused'), 'give_up': True}
                    elif s.get('locked'):
                        self.state = {'key': 'red', 'text': '电脑已锁屏，锁屏期间无法读消息，请保持解锁',
                                      'paused': s.get('paused'), 'give_up': False}
                    elif s.get('stale'):
                        self.state = {'key': 'red', 'text': '长时间未收到消息（窗口异常/锁屏？请检查）',
                                      'paused': s.get('paused'), 'give_up': False}
                    elif s.get('paused'):
                        self.state = {'key': 'yellow', 'text': '已暂停（不监听不转发）', 'paused': True, 'give_up': False}
                    elif s.get('wechat_ready'):
                        n = s.get('chats') or []
                        self.state = {'key': 'green', 'text': f'运行中（监听 {len(n)} 个群）', 'paused': False, 'give_up': False}
                    else:
                        self.state = {'key': 'blue', 'text': '正在连接微信…', 'paused': False, 'give_up': False}
                else:
                    self.state = {'key': 'red', 'text': '服务未运行', 'paused': False, 'give_up': False}
            except (urllib.error.URLError, Exception):
                if self.proc and self.proc.poll() is None:
                    self.state = {'key': 'blue', 'text': '服务启动中…', 'paused': False, 'give_up': False}
                else:
                    self.state = {'key': 'red', 'text': '服务未运行', 'paused': False, 'give_up': False}
            self._refresh_icon()
            time.sleep(4)

    # ---------- 图标 ----------
    def _make_image(self, color):
        img = Image.new('RGBA', (64, 64), (255, 255, 255, 0))
        d = ImageDraw.Draw(img)
        d.ellipse((6, 6, 58, 58), fill=color)
        d.ellipse((6, 6, 58, 58), outline=(255, 255, 255, 230), width=3)
        return img

    def _refresh_icon(self):
        try:
            c = {'green': _GREEN, 'yellow': _YELLOW, 'red': _RED, 'blue': _BLUE}.get(self.state['key'], _GRAY)
            self.icon.icon = self._make_image(c)
            self.icon.title = '客服微信助手 · ' + self.state['text'][:50]
            self.icon.update_menu()
        except Exception:
            pass

    # ---------- 菜单动作 ----------
    def open_panel(self, icon=None, item=None):
        webbrowser.open(BASE)

    def toggle_pause(self, icon=None, item=None):
        try:
            if self.state.get('paused'):
                self._api('/api/bot/resume', 'POST')
            else:
                self._api('/api/bot/pause', 'POST')
        except Exception:
            pass
        time.sleep(0.5)

    def shutdown_service(self, icon=None, item=None):
        try:
            self._api('/api/bot/shutdown', 'POST')
        except Exception:
            self.stop_service()
        time.sleep(1.5)

    def autostart_enabled(self, item):
        return os.path.exists(self.startup_vbs)

    def toggle_autostart(self, icon=None, item=None):
        try:
            if os.path.exists(self.startup_vbs):
                os.remove(self.startup_vbs)
            else:
                pyw = python_exe()
                vbs = (
                    'Set ws = CreateObject("WScript.Shell")\r\n'
                    f'ws.Run """{pyw}"" ""{os.path.join(ROOT, "tray.py")}""", 0, False\r\n'
                )
                os.makedirs(os.path.dirname(self.startup_vbs), exist_ok=True)
                with open(self.startup_vbs, 'w', encoding='gbk') as f:
                    f.write(vbs)
        except Exception:
            pass

    def quit_app(self, icon=None, item=None):
        self.quitting = True
        self.stop_tunnel()
        self.stop_service()
        try:
            icon.stop()
        except Exception:
            pass

    def _pause_label(self, item):
        return '恢复服务' if self.state.get('paused') else '暂停服务'

    def run(self):
        self.start_service()
        menu = pystray.Menu(
            pystray.MenuItem('打开管理面板', self.open_panel, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(lambda i: self._pause_label(i), self.toggle_pause),
            pystray.MenuItem('重启服务', self.restart_service),
            pystray.MenuItem('停止服务', self.shutdown_service),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(lambda i: self.tunnel_label(i), self.toggle_tunnel),
            pystray.MenuItem(lambda i: self.tunnel_url_label(i), self.copy_tunnel_url,
                             enabled=lambda i: bool(self.tunnel_url)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem('开机自动启动', self.toggle_autostart, checked=self.autostart_enabled),
            pystray.MenuItem('退出', self.quit_app),
        )
        self.icon = pystray.Icon('kefu-wechat', self._make_image(_GRAY), '客服微信助手', menu)
        threading.Thread(target=self.poll_status, daemon=True).start()
        self.icon.run()


if __name__ == '__main__':
    TrayApp().run()
