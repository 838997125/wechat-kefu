# -*- coding: utf-8 -*-
"""
客服微信助手 · 启动入口
- 启动微信机器人后台线程
- 启动 Web 管理面板（默认 http://127.0.0.1:43991）
"""
import logging
import os
import sys
import threading
import time
import webbrowser
from logging.handlers import RotatingFileHandler


def base_dir():
    """打包后数据放在 exe 同级目录，开发时放在项目目录。"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def setup_logging(log_path):
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    logger = logging.getLogger('kefu')
    logger.setLevel(logging.INFO)
    logger.propagate = False
    fmt = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', '%Y-%m-%d %H:%M:%S')
    fh = RotatingFileHandler(log_path, maxBytes=2_000_000, backupCount=5, encoding='utf-8')
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


def main():
    root = base_dir()
    log_path = os.path.join(root, 'logs', 'bot.log')
    db_path = os.path.join(root, 'data', 'messages.db')
    config_path = os.path.join(root, 'config.json')
    os.makedirs(os.path.join(root, 'data'), exist_ok=True)
    os.makedirs(os.path.join(root, 'logs'), exist_ok=True)

    log = setup_logging(log_path)
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

    from app.config import Config
    from app.storage import Storage
    from app.bot import Bot
    from app.web.server import PanelServer

    cfg = Config(config_path)
    storage = Storage(db_path)
    bot = Bot(cfg, storage)
    panel = PanelServer(cfg, storage, bot, log_path, port=int(os.environ.get('KEFU_PORT', 43991)))

    bot.start()

    if cfg.general().get('auto_open_panel', True):
        def _open():
            time.sleep(1.5)
            try:
                webbrowser.open(f'http://127.0.0.1:{panel.port}')
            except Exception:
                pass
        threading.Thread(target=_open, daemon=True).start()

    log.info('━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━')
    log.info('客服微信助手已启动，管理面板：http://127.0.0.1:%s', panel.port)
    log.info('使用要求：PC 微信 4.1.8.107 已登录、主窗口保持打开、关闭微信自动更新')
    log.info('━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━')
    try:
        panel.run()
    except KeyboardInterrupt:
        pass
    finally:
        bot.stop()


if __name__ == '__main__':
    main()
