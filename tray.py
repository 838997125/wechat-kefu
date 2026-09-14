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
import re
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
        self.tunnel_fixed = False
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
        # cloudflared 已作为 Windows 系统服务运行时，托盘不再另起临时隧道，避免双开
        if self.cloudflared_service_state() == 'RUNNING':
            self.state['text'] = '公网隧道由系统服务管理（固定地址）'
            return
        exe = self._cloudflared()
        if not exe:
            self.state['text'] = '未找到 cloudflared.exe'
            return
        os.makedirs(os.path.dirname(self.tunnel_log), exist_ok=True)
        logf = open(self.tunnel_log, 'ab')
        token = self._tunnel_token()
        if token:
            # 固定命名隧道（Zero Trust 网页创建，DNS/公网域名在云端配置，地址永久不变）
            cmd = [exe, 'tunnel', '--no-autoupdate', 'run', '--token', token]
            self.tunnel_fixed = True
            self.tunnel_url = self._fixed_hostname() or '固定域名（见配置）'
        else:
            # 回退：临时 quick tunnel（地址每次变）
            cmd = [exe, 'tunnel', '--url', 'http://127.0.0.1:%d' % PORT, '--no-autoupdate']
            self.tunnel_fixed = False
        self.tunnel_proc = subprocess.Popen(
            cmd, cwd=os.path.dirname(exe), stdout=logf, stderr=subprocess.STDOUT,
            creationflags=CREATE_NO_WINDOW, close_fds=True)
        if not token:
            threading.Thread(target=self._read_tunnel_url, daemon=True).start()

    def _config_path(self):
        return os.path.join(ROOT, 'config.json')

    def _load_config(self):
        try:
            with open(self._config_path(), 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}

    def _tunnel_token(self):
        """config.general.cloudflared_token 或环境变量；配置了即用固定隧道。"""
        t = os.environ.get('CLOUDFLARED_TUNNEL_TOKEN', '')
        if t:
            return t.strip()
        g = self._load_config().get('general', {}) or {}
        return str(g.get('cloudflared_token', '') or '').strip()

    def _fixed_hostname(self):
        g = self._load_config().get('general', {}) or {}
        return str(g.get('cloudflared_hostname', '') or '').strip()

    def cloudflared_service_state(self):
        """查询 Windows 上 Cloudflared 系统服务状态。
        返回 'RUNNING'/'STOPPED'/None(未安装或非Windows)。服务在跑时禁用托盘临时隧道，避免双开。"""
        try:
            if not sys.platform.startswith('win'):
                return None
            r = subprocess.run(['sc', 'query', 'Cloudflared'], capture_output=True,
                               text=True, timeout=8, creationflags=CREATE_NO_WINDOW)
            out = (r.stdout or '') + (r.stderr or '')
            m = re.search(r'STATE\s*:\s*\d+\s+(\w+)', out)
            return m.group(1) if m else None
        except Exception:
            return None

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

    def open_tunnel_url(self, icon=None, item=None):
        """系统服务方式=打开固定域名；托盘临时隧道=复制临时地址。"""
        if self.tunnel_managed_by_service():
            host = self._fixed_hostname()
            if host:
                url = host if host.startswith('http') else 'https://' + host
                webbrowser.open(url)
            return
        if self.tunnel_url:
            self.copy_tunnel_url()
            self.state['text'] = '公网地址已复制：%s' % self.tunnel_url

    def tunnel_managed_by_service(self, item=None):
        """cloudflared 系统服务 RUNNING 时返回 True（托盘不再管理隧道，防双开）。"""
        return self.cloudflared_service_state() == 'RUNNING'

    def tunnel_label(self, item):
        if self.tunnel_managed_by_service():
            return '公网隧道：系统服务管理中'
        kind = '固定' if (self.tunnel_running() and self.tunnel_fixed) else '临时'
        if self.tunnel_running():
            return f'{kind}公网隧道：停止'
        return '公网隧道：启动' + ('（已配置固定域名）' if self._tunnel_token() else '（临时地址）')

    def tunnel_url_label(self, item):
        host = self._fixed_hostname()
        if self.tunnel_managed_by_service():
            return host or '公网固定地址（系统服务）'
        if self.tunnel_running() and self.tunnel_fixed:
            return host or '复制公网地址'
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
            pystray.MenuItem(lambda i: self.tunnel_label(i), self.toggle_tunnel,
                             enabled=lambda i: not self.tunnel_managed_by_service(i)),
            pystray.MenuItem(lambda i: self.tunnel_url_label(i), self.open_tunnel_url,
                             enabled=lambda i: bool(self.tunnel_url) or self.tunnel_managed_by_service(i)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem('开机自动启动', self.toggle_autostart, checked=self.autostart_enabled),
            pystray.MenuItem('退出', self.quit_app),
        )
        self.icon = pystray.Icon('kefu-wechat', self._make_image(_GRAY), '客服微信助手', menu)
        threading.Thread(target=self.poll_status, daemon=True).start()
        # 配置了固定隧道 token 或开启了 auto_start_tunnel 时，开机随托盘自动拉起隧道
        try:
            g = self._load_config().get('general', {}) or {}
            if self._tunnel_token() or g.get('cloudflared_autostart'):
                threading.Thread(target=self.start_tunnel, daemon=True).start()
        except Exception:
            pass
        self.icon.run()


if __name__ == '__main__':
    TrayApp().run()
