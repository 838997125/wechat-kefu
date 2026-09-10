# -*- coding: utf-8 -*-
"""客服微信助手一键安装（Windows 新主机）。
由引导 bat 调用；也可直接 python install.py。
流程：检测/安装 Python -> 建 venv -> 离线装依赖 -> 自检 -> 注册开机自启。
"""
import os
import sys
import shutil
import subprocess
import glob

# Windows 控制台中文输出兼容（bat 引导时 chcp 936）
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

ROOT = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(ROOT)                      # 部署包根目录（app 的上一级）
VENV = os.path.join(ROOT, '.venv')
PYEXE = os.path.join(VENV, 'Scripts', 'python.exe')
WHEELS = os.path.join(PKG, 'vendor', 'wheels')
PY_INST = os.path.join(PKG, 'runtime', 'python-3.12-amd64.exe')
REQS = os.path.join(ROOT, 'requirements.txt')


def run(cmd, **kw):
    print('  >', ' '.join(cmd) if isinstance(cmd, list) else cmd)
    return subprocess.run(cmd, **kw)


def venv_python(base_python):
    if os.path.exists(PYEXE):
        return PYEXE
    print('[2/6] 创建独立运行环境 .venv ...')
    run([base_python, '-m', 'venv', VENV], check=True)
    if not os.path.exists(PYEXE):
        raise RuntimeError('虚拟环境创建失败: ' + PYEXE)
    return PYEXE


def install_python():
    """静默安装内置 Python 3.12（先全局，失败则用户级），返回 python.exe 路径。"""
    if not os.path.exists(PY_INST):
        raise RuntimeError('找不到内置 Python 安装器: ' + PY_INST)
    print('  正在静默安装 Python 3.12（约1分钟）...')
    common_args = ['/quiet', 'PrependPath=1', 'Include_test=0',
                   'Include_launcher=1', 'Include_pip=1']
    r = subprocess.run([PY_INST] + common_args + ['InstallAllUsers=1'])
    cands = [r'C:\Program Files\Python312\python.exe',
             os.path.expandvars(r'%LOCALAPPDATA%\Programs\Python\Python312\python.exe')]
    found = next((p for p in cands if os.path.exists(p)), None)
    if not found and r.returncode != 0:
        # 全局安装失败（可能权限不足），退到当前用户安装
        print('  全局安装未成功，尝试当前用户安装 ...')
        subprocess.run([PY_INST] + common_args + ['InstallAllUsers=0'], check=False)
        found = next((p for p in cands if os.path.exists(p)), None)
    if not found:
        raise RuntimeError('Python 安装结束但未找到 python.exe，请右键以管理员身份运行安装脚本')
    return found


def pick_python():
    """挑选 3.10-3.12 的 Python（离线 wheel 是按 cp312 构建的，版本必须匹配）。
    找不到合适版本时，静默安装包内置的 Python 3.12 并返回其路径。"""
    vi = sys.version_info
    if (3, 10) <= (vi.major, vi.minor) <= (3, 12):
        return sys.executable
    # 通过 py 启动器找 3.12/3.11/3.10
    if shutil.which('py'):
        for ver in ('3.12', '3.11', '3.10'):
            r = subprocess.run(['py', '-%s' % ver, '-c', 'import sys;print(sys.executable)'],
                               capture_output=True, text=True)
            if r.returncode == 0 and r.stdout.strip():
                p = r.stdout.strip().splitlines()[-1].strip()
                if os.path.exists(p):
                    return p
    # 版本不符且无合适解释器：安装内置 3.12（离线 wheel 强依赖）
    print('  当前 Python 为 %d.%d，离线依赖要求 3.10-3.12，准备安装内置 Python 3.12 ...'
          % (vi.major, vi.minor))
    return install_python()


def main():
    print('=' * 56)
    print('客服微信助手 一键安装部署')
    print('=' * 56)
    print('部署目录:', PKG)

    # 1. Python
    print('\n[1/6] 检测 Python ...')
    base = pick_python()
    print('  使用 Python:', base)
    subprocess.run([base, '--version'])

    # 2. venv
    print('\n[2/6] 创建独立运行环境 ...')
    py = venv_python(base)
    print('  venv Python:', py)

    # 3. 离线依赖
    print('\n[3/6] 安装依赖（优先离线包）...')
    r = subprocess.run([py, '-m', 'pip', 'install', '--no-index',
                        '--find-links', WHEELS, '-r', REQS])
    if r.returncode != 0:
        print('  离线安装有缺失，尝试在线补齐 ...')
        subprocess.run([py, '-m', 'pip', 'install', '-r', REQS], check=True)

    # 4. 自检
    print('\n[4/6] 自检依赖 ...')
    check = "import flask,wxauto4,psutil,openpyxl;print('  核心依赖导入成功')"
    subprocess.run([py, '-c', check], check=True)
    if not os.path.exists(os.path.join(ROOT, 'config.json')):
        ex = os.path.join(ROOT, 'config.example.json')
        if os.path.exists(ex):
            shutil.copy(ex, os.path.join(ROOT, 'config.json'))
    print('  配置文件就绪')

    # 5. 微信提示
    print('\n[5/6] 微信检查')
    print('  请确认已安装微信 4.1.8.107（安装包在 runtime 目录），并登录、主窗口保持打开。')

    # 6. 开机自启
    print('\n[6/6] 注册开机自启 ...')
    guard = os.path.join(ROOT, '客服助手开机自启.bat')
    sch = ['schtasks', '/create', '/tn', 'KefuWechatBot', '/tr',
           '"%s"' % guard, '/sc', 'onlogon', '/rl', 'highest', '/f']
    rr = subprocess.run(sch, capture_output=True, text=True)
    if rr.returncode == 0:
        print('  开机自启已注册')
    else:
        print('  计划任务注册失败（可右键以管理员身份重跑，或手动双击 app 目录开机自启脚本）')

    print('\n' + '=' * 56)
    print('全部完成！下一步：双击 app\\启动客服助手.bat')
    print('面板 http://127.0.0.1:43991  密码默认 kefu2026（请尽快修改）')
    print('=' * 56)


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print('\n[安装失败]', e)
        sys.exit(1)
