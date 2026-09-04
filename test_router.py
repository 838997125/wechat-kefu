# -*- coding: utf-8 -*-
"""路由引擎 + 影子模式离线自测（不连微信）。"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding='utf-8')

from app.config import Config
from app.storage import Storage
from app.bot import Bot
from app.driver import NormalizedMsg
from app import router as R

PASS = FAIL = 0
def check(name, cond, detail=''):
    global PASS, FAIL
    if cond: PASS += 1; print(f'  ✅ {name}')
    else: FAIL += 1; print(f'  ❌ {name} {detail}')


print('1) 单号提取:')
check('中通14位', R.extract_tracking_no('<TRACKING_NO_A> 拦截') == '<TRACKING_NO_A>')
check('京东JDV', R.extract_tracking_no('JDV029472116353 已发货').upper().startswith('JDV'))
check('无单号返回空', R.extract_tracking_no('收到') == '')

print('2) 路由匹配（A群拦截→B群，排除己方客服）:')
routes = Config(os.path.join(tempfile.mkdtemp(), 'c.json')).data['routing']['routes']
rt = R.match_route(routes, '<品牌A>客服对接群', '<合作方客服A>-拼多多售前', '<TRACKING_NO_A> 拦截', 'text')
check('合作方拦截消息命中A→B路由', rt is not None and rt['id'] == 'rt_a2b', str(rt and rt['id']))
rt2 = R.match_route(routes, '<品牌A>客服对接群', '<己方客服B>', '7903 拦截', 'text')
check('己方客服(<公司简称>)搬运的消息被排除', rt2 is None, str(rt2 and rt2['id']))
rt3 = R.match_route(routes, '<品牌A>客服对接群', '<合作方客服A>', '7913 催单', 'text')
check('催单命中A→C路由', rt3 is not None and rt3['id'] == 'rt_a2c', str(rt3 and rt3['id']))
rt4 = R.match_route(routes, '<品牌A>客服对接群', '<合作方客服A>', '客户说少件了', 'text')
check('少件命中A→D(审单发货)路由', rt4 is not None and rt4['id'] == 'rt_a2d_shortage', str(rt4 and rt4['id']))

print('3) B群结果回流（仅“无法拦截”才回传，正常拦截成功不回传）:')
# 拦截成功（已通知网点）-> 不回传
rt_ok = R.match_route(routes, '药商通c端中通快递沟通群', '<回写机器人名>',
                    '@<己方客服E> <TRACKING_NO_C>退回寄件网点，已通知网点。', 'text')
check('拦截成功(已通知网点)不回传A群', rt_ok is None, str(rt_ok and rt_ok['id']))
rt_ok2 = R.match_route(routes, '药商通c端中通快递沟通群', '<回写机器人名>',
                    '@x 7903已处理完成，快件已经操作退回扫描', 'text')
check('拦截成功(已退回扫描)不回传A群', rt_ok2 is None, str(rt_ok2 and rt_ok2['id']))
# 无法拦截（已录签收）-> 回传并@发单人
rt5 = R.match_route(routes, '药商通c端中通快递沟通群', '<回写机器人名>',
                    '@AKA 79028178278170退回寄件网点：快件当前已录签收，若收件人已收到，将无法退回', 'text')
check('已录签收(无法拦截)命中B→A路由', rt5 is not None and rt5['id'] == 'rt_b2a_blocked', str(rt5 and rt5['id']))
fwd = R.build_forward(rt5, '@AKA 79028178278170退回寄件网点：快件当前已录签收，若收件人已收到，将无法退回',
                      '<回写机器人名>', '79028178278170', requester='<合作方客服A>')
check('加工话术为拦截失败提示', fwd.startswith('【中通拦截失败】') and '无法拦截退回' in fwd and '已签收' in fwd, fwd)
check('不含内部@<己方客服E>/操作建议长句', '@<己方客服E>' not in fwd and '未收到证明' not in fwd and '若收件人' not in fwd, fwd)
rt6 = R.match_route(routes, '药商通c端中通快递沟通群', '晴子', '1', 'text')
check('水消息"1"不命中任何路由', rt6 is None, str(rt6))

print('4) raw 模式纯原文:')
fwd_raw = R.build_forward(rt, '<TRACKING_NO_A> 拦截', '<合作方客服A>', '<TRACKING_NO_A>')
check('原文不变', fwd_raw == '<TRACKING_NO_A> 拦截', fwd_raw)

print('5) 影子模式端到端（命中路由但不发送，记录日志）:')
tmp = tempfile.mkdtemp(prefix='kefu_route_')
cfg = Config(os.path.join(tmp, 'config.json'))
d = cfg.data
d['routing']['shadow_mode'] = True
cfg.save(d)
storage = Storage(os.path.join(tmp, 'm.db'))
bot = Bot(cfg, storage)
sent = []
bot.driver.send = lambda chat, text, at=None: sent.append((chat, text)) or True
bot._route_message(NormalizedMsg('<品牌A>客服对接群', 'group', '<合作方客服A>-拼多多售前', 'friend', 'text', '<TRACKING_NO_A> 拦截'))
check('影子模式不实际发送', len(sent) == 0, f'实际发送了{len(sent)}条')
logs = storage.route_recent()
check('影子日志已记录', len(logs) == 1 and logs[0]['shadow'] == 1, str(len(logs)))
check('日志含目标群和转发内容', logs and logs[0]['target_chat'] == '药商通c端中通快递沟通群' and '拦截' in logs[0]['forward'])

print('6) 防重复转发（影子模式同一原文只记一条）:')
bot._route_message(NormalizedMsg('<品牌A>客服对接群', 'group', '<合作方客服A>-拼多多售前', 'friend', 'text', '<TRACKING_NO_A> 拦截'))
check('同一原文不重复记录影子日志', len(storage.route_recent(shadow=True)) == 1)

print('7) 正式模式端到端（实际发送到目标群）:')
d2 = cfg.data; d2['routing']['shadow_mode'] = False; cfg.save(d2)
sent2 = []
bot.driver.send = lambda chat, text, at=None: sent2.append((chat, text)) or True
bot._route_message(NormalizedMsg('<品牌A>客服对接群', 'group', '<合作方客服A>-拼多多售前', 'friend', 'text', '<TRACKING_NO_D> 召回'))
check('正式模式实际发送1条', len(sent2) == 1, f'{len(sent2)}条')
check('发送目标是B群', sent2 and sent2[0][0] == '药商通c端中通快递沟通群', str(sent2 and sent2[0][0]))
check('发送内容为原文', sent2 and '召回' in sent2[0][1])
logs2 = storage.route_recent(shadow=False)
check('正式转发日志标记为sent', logs2 and logs2[0]['status'] == 'sent', str(logs2 and logs2[0]['status']))

print(f'\n结果：{PASS} 通过 / {FAIL} 失败')
sys.exit(1 if FAIL else 0)
