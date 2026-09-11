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
        "poll_interval_sec": 3.0,        # 轮询间隔（保守值，降低机械感）
        "switch_wait_sec": 1.5,          # 切换聊天后等待 UI 渲染秒数
        "reply_delay_min": 3.0,          # 回复前随机延时（拟人，社区安全建议≥5秒档）
        "reply_delay_max": 8.0,
        "max_replies_per_minute": 8,     # 单会话每分钟回复上限
        "split_length": 1500,            # 长消息按此长度拆条
        "record_self": True,             # 是否记录机器人自己发的消息
        "at_sender": True,               # AI 回复群消息时自动 @ 提问人
        "auto_open_panel": True,         # 启动时自动打开管理面板
        # 真实UI心跳间隔（秒）：越短检测越快，但会周期性切换微信窗口可能干扰本机操作；
        # 本机调试用大值（120秒，基本不抢焦点），部署到无人操作的专用主机时可调小（如15~30秒）
        "heartbeat_ui_interval_sec": 120,
        # 微信连接/重连最多尝试次数，超过即停止自动重试、等人工重启（防止反复拉起微信拖垮电脑）
        "max_connect_attempts": 3,
        "panel_password": "kefu2026"     # Web 管理面板登录密码（客服远程访问时使用，请部署后修改）
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
            "reply_mode": "random",      # random=多条话术随机回一条（防风控）/ all=全部连发
            "replies": [
                "亲~查快递请把【订单号】发在群里，客服马上帮您核实物流情况😊",
                "您好，麻烦提供一下订单号哦，这边马上为您查询物流~",
                "收到~请把订单号发给我们，客服会尽快核实并回复您"
            ],
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
            "reply_mode": "random",
            "replies": [
                "客服在线时间：周一至周日 9:00–21:00，节假日照常值班~",
                "我们每天 9 点到 21 点都有客服在线哦，节假日不休息~"
            ],
            "at": ["none"]
        }
    ],
    "welcome": {
        "enabled": False,
        "chats": [],                     # 空=所有监听群
        "message": "欢迎新成员进群🎉 有问题直接在群里留言，客服会尽快回复~",
        "at_newcomer": True
    },
    # 多群消息路由转发
    "routing": {
        "shadow_mode": True,             # 影子模式：只记录将转发什么，不实际发送（核对准确率后改 False 正式转发）
        # 己方客服名单（这些人发的消息视为己方操作，转发/回流时排除或特殊处理）
        # 命中规则：昵称在名单内，或昵称含“<公司简称>”
        "own_staff": ["<公司简称>", "<己方客服D>", "<己方客服E>", "。。", "<己方客服B>", "<己方客服C>"],
        # 客服源群列表：外部平台售后在这些群发需求，共用同一套路由规则（->中通/催发货/仓库）；
        # <回写机器人名>失败结果按单号实际来源回流到对应客服源群。新增同类客服群只需在此追加群名并在 chats 勾选监听。
        "customer_groups": [
            "<品牌A>客服对接群",
            "聚好麦【西帕医药旗舰店-物流"
        ],
        "routes": [
            {
                "id": "rt_a2b",
                "name": "客服对接群 → 中通快递群（拦截/召回/签收未收到/催件/送错地址/短少）",
                "enabled": True,
                "source": "<品牌A>客服对接群",
                "target": "药商通c端中通快递沟通群",
                "mode": "raw",           # raw=纯原文转发 / process=加工话术后转发
                # 以群里实际转发为准：拦截/召回/签收未收到/催件/送错地址/短少 客服都会转到中通群
                "keywords": ["拦截", "召回", "签收未收到", "签收未收", "没收到", "催件", "催快递",
                             "送错地址", "没送到正确地址", "没送到", "短少", "少件"],
                "regex": "",
                # 排除：拒收咨询、顺丰(SF)售后退货、特殊退/效果问题等非中通拦截指令
                "exclude_keywords": ["拒收么", "可以拒收", "能拒收", "特殊退", "效果问题", "验收"],
                "require_sender_contains": [],   # 空=不限定发送者；填关键词则只处理昵称含该词的消息
                "exclude_sender_contains": ["<公司简称>"],  # 排除己方客服（他们已手工搬运）
                "template": ""           # process 模式的话术模板，可用 {单号}{text}{sender}{来源摘要}
            },
            {
                "id": "rt_a2c",
                "name": "客服对接群 → <物流方>&<公司简称>（催发货/催单，提取快递单号去重）",
                "enabled": True,
                "source": "<品牌A>客服对接群",
                "target": "<物流方>&<公司简称>",
                "mode": "process",
                "keywords": ["催单", "提前揽收", "催揽收", "麻烦揽收", "安排揽收", "揽收一下", "催发货", "催派"],
                "regex": "",
                "exclude_keywords": ["揽收重量", "已揽收"],   # 排除含重量数据/已揽收的非催办消息
                "require_sender_contains": [],
                "exclude_sender_contains": ["<公司简称>"],
                # 只发提取出的快递单号，按单号去重，不转发平台订单号
                "extract_numbers": True,
                "dedup_by_number": True,
                # 京东快递单号不进中通催发群（京东走自有揽收，需客服人工处理京东单）
                "exclude_number_prefix": ["JDV", "JD"],
                "template": "【催发货】{全部单号}"
            },
            {
                "id": "rt_b2a_blocked",
                "name": "中通快递群 → 客服对接群（仅无法拦截：已签收/在代收点，@发单人）",
                "enabled": True,
                "source": "药商通c端中通快递沟通群",
                "target": "<品牌A>客服对接群",
                "mode": "process",
                # 只有“快件已录签收/已在代收点/无法退回”才回传客服群并@发单人；
                # 正常“已通知网点/已受理/已处理完成-退回扫描”是拦截成功，不回传。
                # 只有“确认拦截失败”的结果才回传：快件已签收/已在代收点/已取件；
                # 注意“已受理/已入柜入库/若收件人已收到将无法退回”是条件句（拦截进行中），不算失败。
                # 注意：不要用“无法退回”这种词——拦截受理成功的条件句里也有“若收件人已收到，将无法退回”。
                "keywords": ["已录签收", "已被签收", "已完成签收", "快件已签收", "已在代收点", "已在驿站",
                            "已被取件", "已取件", "拦截失败", "未到达指定退改地址", "无法受理",
                            "进村件", "投递至村站", "已完成转运派送"],
                "regex": "",
                "require_sender_contains": ["<回写机器人名>"],   # 只处理中通<回写机器人名>的结果消息
                "exclude_sender_contains": [],
                "at_requester": True,                  # 回传时 @A群里该单号的发单人
                "template": "【中通拦截失败】单号{单号}：{来源摘要}，请发单客服知悉并联系客户处理。"
            },
            {
                "id": "rt_a2d_shorthand",
                "name": "客服对接群 → C端审单发货售后（订单特殊备注）",
                "enabled": True,
                "source": "<品牌A>客服对接群",
                "target": "C端审单发货售后",
                "mode": "raw",
                "keywords": ["特殊备注", "备注", "改地址", "换货", "补发", "指定快递", "不要发"],
                "regex": "",
                "require_sender_contains": [],
                "exclude_sender_contains": ["<公司简称>"],
                "template": ""
            },
            {
                "id": "rt_a2d_shortage",
                "name": "客服对接群 → C端审单发货售后（少件/少发核实、提供发货视频截图）",
                "enabled": True,
                "source": "<品牌A>客服对接群",
                "target": "C端审单发货售后",
                "mode": "raw",
                # 仅当明确是仓库发货少件/要打包视频才转D；"核实结果"太宽泛（可能是中通核实走B群），不放关键词交给大模型区分
                "keywords": ["少件", "少发", "漏发", "缺货", "少货", "数量不对", "没收到货", "包裹里没有",
                             "提供发货视频", "发货视频", "打包视频", "称重图", "称重", "反馈少", "少一盒", "少一瓶"],
                "regex": "",
                # 排除：平台退货验收、出库数量截图等非仓库少件核实
                "exclude_keywords": ["验收", "出库", "退货验收", "验收图片"],
                "require_sender_contains": [],
                "exclude_sender_contains": ["<公司简称>"],
                "template": ""
            }
        ],
        # 大模型意图识别（混合方案）：未在学习库命中的新消息才调用模型，结果自动学习记忆
        # api_key 不在代码里写死：请在本地 config.json 的 routing.llm_intent.api_key 配置，
        # 或设置环境变量 KEFU_ARK_API_KEY（避免密钥提交到代码仓库）
        "llm_intent": {
            "enabled": True,
            "base_url": "https://ark.cn-beijing.volces.com/api/plan/v3",
            "api_key": os.environ.get("KEFU_ARK_API_KEY", ""),
            "model": "ark-code-latest"
        }
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
