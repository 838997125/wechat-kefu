# -*- coding: utf-8 -*-
"""
客服微信助手 · 一键升级（保留历史数据）
把新版本整包解压到任意目录后，在【新版目录】运行本脚本，或双击同目录的
「2.升级程序-保留数据.bat」。

升级逻辑：
  1. 停止正在运行的机器人/托盘（释放文件占用）
  2. 找到旧安装目录（自动扫描常见位置，或让用户确认）
  3. 备份旧目录的 data/、config.json、logs/（在旧目录留 backup_时间戳）
  4. 仅覆盖程序文件（app 代码、run.py、tray.py、requirements、启动脚本、bin/runtime/vendor），
     绝不删除旧目录的 data/ 与 config.json
  5. 依赖若有新增则离线/在线补装（复用旧 .venv，不重建环境，秒级完成）
  6. 完成后提示启动；历史消息、转发记录、人工规则、AI 学习库全部保留。
"""
import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))  # 新版 app 目录的上级调用见 main
# 兼容：脚本可在 app/ 内（ROOT=app）或包根
def find_app_dir(base):
    if os.path.exists(os.path.join(base, 'run.py')):
        return base
    if os.path.exists(os.path.join(base, 'app', 'run.py')):
        return os.path.join(base, 'app')
    return None

# 受保护（绝不覆盖/删除）的运行期数据
KEEP = ['data', 'config.json', 'logs']
# 要更新的程序文件/目录（位于 app 目录）
PROGRAM_ITEMS = ['app_pkg', 'run.py', 'tray.py', 'requirements.txt',
                 '启动客服助手.bat', '启动客服助手.vbs']


def log(msg):
    print(msg, flush=True)


def stop_running():
    log('[1/6] 停止正在运行的服务 ...')
    # 面板优雅停止
    try:
        import urllib.request
        urllib.request.urlopen('http://127.0.0.1:43991/api/bot/shutdown', data=b'', timeout=4)
        time.sleep(2)
    except Exception:
        pass
    # 兜底：结束本项目相关 python/pythonw（按命令行路径匹配）
    try:
        import psutil
        for p in psutil.process_iter(['name', 'cmdline']):
            try:
                cmd = ' '.join(p.info.get('cmdline') or [])
                if ('run.py' in cmd or 'tray.py' in cmd) and 'upgrade' not in cmd:
                    p.terminate()
            except Exception:
                pass
        time.sleep(1.5)
    except Exception:
        pass


def _is_old_app(path):
    """一个目录是旧版安装 app：含 run.py + data(历史) + .venv(已安装的独立环境)。
    要求 .venv 是为了排除源码开发目录（开发目录通常没有打包好的 .venv 部署环境）。"""
    try:
        return (os.path.isfile(os.path.join(path, 'run.py'))
                and os.path.isdir(os.path.join(path, 'data'))
                and os.path.isdir(os.path.join(path, '.venv', 'Scripts')))
    except Exception:
        return False


# 明确排除：源码开发目录与打包工作目录（按绝对路径/特征），绝不参与升级
_EXCLUDE_SUBSTR = (
    r'deepseekharness',          # 源码工程
    r'\kefu-deploy',             # 打包工作目录（精确，不含 kefu-wechat-deploy-vX 安装目录）
    r'\kefu-remote',
    r'\升级测试', r'\部署验证', r'\v16验证', r'\v17验证', r'\v18验证', r'\v19验证',
)


def _excluded(path):
    p = path.lower()
    # 含 .git 的是源码工程
    if os.path.isdir(os.path.join(path, '.git')):
        return True
    return any(k in p for k in _EXCLUDE_SUBSTR)


def candidate_old_dirs():
    cands = []
    skip_names = {'windows', 'program files', 'program files (x86)', '$recycle.bin',
                  'system volume information', 'node_modules', '.git', '__pycache__',
                  'appdata', 'documents and settings', 'perflogs', 'recovery',
                  '$windows.~bt', '$windows.~ws', 'deepseekharness'}
    for drive in ['C:\\', 'D:\\', 'E:\\']:
        if not os.path.exists(drive):
            continue
        for dirpath, dirnames, _ in os.walk(drive):
            if dirpath[len(drive):].count(os.sep) >= 4:
                dirnames[:] = []
                continue
            if _excluded(dirpath):
                dirnames[:] = []
                continue
            dirnames[:] = [d for d in dirnames if d.lower() not in skip_names and not d.startswith('.')]
            if _is_old_app(dirpath) and not _excluded(dirpath):
                cands.append(os.path.abspath(dirpath))
    seen, out = set(), []
    for c in cands:
        if c not in seen:
            seen.add(c); out.append(c)
    return out


def choose_old_app(new_app):
    auto = candidate_old_dirs()
    auto = [a for a in auto if os.path.abspath(a) != os.path.abspath(new_app)]
    if not auto:
        log('  未自动找到旧安装目录。')
        p = input('  请手动粘贴旧版本 app 目录的完整路径（含 run.py），回车取消：').strip().strip('"')
        return p if p and find_app_dir(p) else None
    log('  找到以下旧安装：')
    for i, a in enumerate(auto, 1):
        has = os.path.exists(os.path.join(a, 'data'))
        log(f'   [{i}] {a}  (data: {"有" if has else "无"})')
    sel = input('  选择要升级的旧目录序号（默认1，n 手动输入）：').strip() or '1'
    if sel.isdigit() and 1 <= int(sel) <= len(auto):
        return auto[int(sel) - 1]
    if sel.lower() == 'n':
        p = input('  旧 app 目录完整路径：').strip().strip('"')
        return p if find_app_dir(p) else None
    return auto[0]


def backup_old(old_app):
    ts = time.strftime('%Y%m%d_%H%M%S')
    bdir = os.path.join(old_app, f'backup_{ts}')
    os.makedirs(bdir, exist_ok=True)
    for item in KEEP:
        src = os.path.join(old_app, item)
        if os.path.exists(src):
            dst = os.path.join(bdir, item)
            if os.path.isdir(src):
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)
    log(f'[3/6] 已备份历史数据到：{bdir}')
    return bdir


def copy_program(new_app, old_app):
    log('[4/6] 更新程序文件（保留 data/config.json/logs）...')
    # new_app 是新版 app 目录；把其中程序文件复制到 old_app，跳过受保护数据
    skip = set(KEEP) | {'.venv', '__pycache__', 'backup_ignore'}
    for name in os.listdir(new_app):
        if name in skip:
            continue
        s = os.path.join(new_app, name)
        d = os.path.join(old_app, name)
        try:
            if os.path.isdir(s):
                # 复制代码目录（app 包），保留旧目录里的 data
                if name == 'app':
                    _merge_app_package(s, d)
                else:
                    shutil.copytree(s, d, dirs_exist_ok=True)
            else:
                shutil.copy2(s, d)
        except Exception as e:
            log(f'   跳过 {name}: {e}')


def _merge_app_package(new_pkg, old_pkg):
    """复制 app Python 包，但不碰任何 data。"""
    os.makedirs(old_pkg, exist_ok=True)
    for name in os.listdir(new_pkg):
        if name in ('__pycache__',):
            continue
        s = os.path.join(new_pkg, name)
        d = os.path.join(old_pkg, name)
        if os.path.isdir(s):
            shutil.copytree(s, d, dirs_exist_ok=True)
        else:
            shutil.copy2(s, d)


def ensure_deps(old_app):
    log('[5/6] 检查依赖（复用原 .venv，通常很快）...')
    py = os.path.join(old_app, '.venv', 'Scripts', 'python.exe')
    if not os.path.exists(py):
        log('  旧目录无 .venv，将由首次启动的安装脚本重建；本次仅更新代码。')
        return
    wheels = None
    # 新版包内 vendor/wheels
    pkg_root = os.path.dirname(os.path.dirname(old_app))  # app 上两级=包根（best effort）
    cand = os.path.join(pkg_root, 'vendor', 'wheels')
    if os.path.isdir(cand):
        wheels = cand
    reqs = os.path.join(old_app, 'requirements.txt')
    cmd = [py, '-m', 'pip', 'install']
    if wheels:
        cmd += ['--no-index', '--find-links', wheels]
    cmd += ['-r', reqs]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 and wheels:
        log('  离线补装有缺失，尝试在线 ...')
        subprocess.run([py, '-m', 'pip', 'install', '-r', reqs])
    log('  依赖检查完成')


def main():
    new_app = find_app_dir(ROOT)
    if not new_app:
        log('未在当前目录找到 run.py，请把本脚本放在新版 app 目录或包根后重试。')
        input('回车退出'); sys.exit(1)
    log('================ 客服微信助手 · 升级（保留历史） ================')
    log(f'新版本目录：{new_app}')
    stop_running()
    log('[2/6] 查找旧安装 ...')
    old_app = choose_old_app(new_app)
    if not old_app:
        log('未选择旧目录，已取消。历史数据未做任何改动。')
        input('回车退出'); sys.exit(0)
    old_app = find_app_dir(old_app) or old_app
    log(f'  目标旧目录：{old_app}')
    if os.path.abspath(old_app) == os.path.abspath(new_app):
        log('新旧目录相同，无需升级（已在最新目录）。')
        input('回车退出'); sys.exit(0)
    backup_old(old_app)
    copy_program(new_app, old_app)
    ensure_deps(old_app)
    log('[6/6] 升级完成！历史消息/转发记录/人工规则/AI学习库均已保留。')
    log('请从旧目录的桌面快捷方式或「启动客服助手.vbs」启动；建议先保持影子模式观察。')
    input('回车退出')


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        import traceback
        traceback.print_exc()
        input('升级出错（如上），回车退出')
        sys.exit(1)
