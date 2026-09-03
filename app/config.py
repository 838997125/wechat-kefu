# -*- coding: utf-8 -*-
"""配置管理：config.json 读写、默认值、热加载（按文件 mtime）。"""
import json
import os
import threading
import time
import uuid

DEFAULT_CONFIG = {
    "general": {
        "bot_names": ["zyq"],            # 机器人在群里的昵称（@ 识别用，可多个）
        "poll_interval_sec": 1.5,        # 轮询间隔
        "switch_wait_sec": 1.5,          # 切换聊天后等待 UI 渲染秒数
        "reply_delay_min": 1.0,          # 回复前随机延时（拟人）
        "reply_delay_max": 3.0,
        "max_replies_per_minute": 12,    # 单会话每分钟回复上限
        "split_length": 1500,            # 长消息按此长度拆条
        "record_self": True,             # 是否记录机器人自己发的消息
        "at_sender": True,               # AI 回复群消息时自动 @ 提问人
        "auto_open_panel": True          # 启动时自动打开管理面板
    },
    "chats": [
        {"name": "客服测试群", "type": "group", "enabled": True}
    ],
    "rules": [
        {
            "id": "r_demo_1",
            "name": "示例-查快递",
            "enabled": True,
            "scope": "all",              # all=全部会话 / group=仅群聊 / dm=仅私聊
            "chats": [],                 # 限定会话名列表，空=不限定
            "keywords": ["快递", "单号", "物流", "查件"],
            "match": "any",              # any=包含任一 / all=全部包含 / exact=完全相等 / regex=正则
            "require_at": False,         # 是否必须 @机器人
            "replies": ["亲~查快递请把【订单号】发在群里，客服马上帮您核实物流情况😊"],
            "at": ["asker"]              # asker=@提问人 / none=不@ / 也可填具体群昵称
        },
        {
            "id": "r_demo_2",
            "name": "示例-营业时间",
            "enabled": True,
            "scope": "all",
            "chats": [],
            "keywords": ["营业时间", "几点上班", "几点下班"],
            "match": "any",
            "require_at": False,
            "replies": ["客服在线时间：周一至周日 9:00–21:00，节假日照常值班~"],
            "at": ["none"]
        }
    ],
    "welcome": {
        "enabled": False,
        "chats": [],                     # 空=所有监听群
        "message": "欢迎新成员进群🎉 有问题直接在群里留言，客服会尽快回复~",
        "at_newcomer": True
    },
    "ai": {
        "enabled": False,
        "trigger": "at",                 # at=仅@机器人且规则未命中 / all=所有未命中消息 / off=关闭
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "",
        "model": "deepseek-chat",
        "temperature": 0.7,
        "timeout_sec": 60,
        "memory_turns": 10,              # 每个会话保留的对话轮数（0=无记忆）
        "system_prompt": "你是一名专业、耐心的微信客服助手。回答简洁口语化，一次只说一件事；不确定的信息不要编造，引导客户留下订单号或联系方式。",
        "user_prompt": "当前群聊：{chat}\n发送人：{sender}\n客户消息：{text}"
    }
}


def new_rule_id():
    return 'r_' + uuid.uuid4().hex[:10]


class Config:
    """线程安全的配置容器，支持按文件 mtime 热加载。"""

    def __init__(self, path):
        self.path = path
        self._lock = threading.RLock()
        self._data = None
        self._mtime = 0
        self.load()

    def load(self, force=False):
        """从磁盘加载；文件未变化时跳过。返回是否实际加载。"""
        with self._lock:
            try:
                mtime = os.path.getmtime(self.path)
            except OSError:
                mtime = 0
            if not force and mtime and mtime == self._mtime and self._data is not None:
                return False
            if not os.path.exists(self.path):
                self._data = json.loads(json.dumps(DEFAULT_CONFIG))
                self.save()
            else:
                with open(self.path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                self._data = self._merge_defaults(data)
            self._mtime = self._file_mtime()
            return True

    def _file_mtime(self):
        try:
            return os.path.getmtime(self.path)
        except OSError:
            return 0

    def _merge_defaults(self, data):
        """用默认值补齐缺失字段（兼容旧配置）。"""
        def merge(base, override):
            out = json.loads(json.dumps(base))
            for k, v in (override or {}).items():
                if k in out and isinstance(out[k], dict) and isinstance(v, dict):
                    out[k] = merge(out[k], v)
                else:
                    out[k] = v
            return out
        merged = merge(DEFAULT_CONFIG, data)
        # 规则补 id
        for r in merged.get('rules', []):
            if not r.get('id'):
                r['id'] = new_rule_id()
        return merged

    def save(self, data=None):
        """保存配置到磁盘。data 为 None 时写回当前内存配置。"""
        with self._lock:
            if data is not None:
                self._data = self._merge_defaults(data)
            tmp = self.path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
            self._mtime = self._file_mtime()

    @property
    def data(self):
        """配置快照（dict）。使用方读取后不应直接修改，修改走 save。"""
        with self._lock:
            return json.loads(json.dumps(self._data))

    # 便捷访问
    def general(self):
        return self.data['general']

    def chats(self, enabled_only=True):
        cs = self.data.get('chats', [])
        return [c for c in cs if c.get('enabled', True)] if enabled_only else cs

    def rules(self, enabled_only=True):
        rs = self.data.get('rules', [])
        return [r for r in rs if r.get('enabled', True)] if enabled_only else rs

    def welcome(self):
        return self.data.get('welcome', {})

    def ai(self):
        return self.data.get('ai', {})
