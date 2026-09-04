/**
 * 企业微信「智能机器人」长连接模式最小验证脚本（内部群场景）
 *
 * 前置条件（在企业微信管理后台操作）：
 *   1. 应用管理 → 智能机器人 → 创建机器人（需管理员），记录 aibotid
 *   2. 获取 corpid（我的企业 → 企业ID）和该机器人的 secret
 *   3. 在一个【内部群】里：群设置 → 群机器人 → 添加刚创建的机器人
 *   4. 运行本脚本，在群里 @机器人 发消息
 *
 * 运行：
 *   $env:WECOM_CORPID="ww..."; $env:WECOM_BOT_SECRET="..."
 *   node index.js
 *
 * 注意：企微长连接协议为 WebSocket + 消息加解密（AES/CBC + SM4 国密），
 * 官方推荐直接使用其 SDK（Java/Go/C++，见文档「专区程序SDK和示例下载」）。
 * 本脚本先用「接收消息 HTTP 回调 + access_token 主动回复」模式做验证，
 * 只需一台能接收企微回调的机器（或用内网穿透），无需长连接 SDK。
 */

import http from 'node:http';
import crypto from 'node:crypto';

const CORPID = process.env.WECOM_CORPID || '';
const BOT_SECRET = process.env.WECOM_BOT_SECRET || '';
const PORT = Number(process.env.PORT || 43995);

// access_token 缓存
let tokenCache = { token: '', expire: 0 };

async function getAccessToken() {
  if (tokenCache.token && Date.now() < tokenCache.expire - 60000) return tokenCache.token;
  const url = `https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid=${CORPID}&corpsecret=${BOT_SECRET}`;
  const r = await fetch(url).then((r) => r.json());
  if (r.errcode !== 0) throw new Error(`gettoken 失败: ${r.errcode} ${r.errmsg}`);
  tokenCache = { token: r.access_token, expire: Date.now() + r.expires_in * 1000 };
  return r.access_token;
}

/**
 * 被动回复：企微回调校验与消息加解密。
 * 回调 URL 验证（GET）：解密 echostr 后原样返回
 * 接收消息（POST）：解密消息体
 * 完整 AES-256-CBC 加解密见官方文档「回调和回复的加解密方案」。
 * 这里给出框架与待补的解密函数签名；实际落地建议用官方/社区 SDK（如 wecom-crypt）。
 */
class WeComCrypto {
  constructor(token, encodingAESKey, receiveId) {
    this.token = token;
    this.aesKey = Buffer.from(encodingAESKey + '=', 'base64');
    this.receiveId = receiveId; // 机器人场景为 corpid
    this.iv = this.aesKey.subarray(0, 16);
  }

  // GET 回调 URL 验证
  verifyURL(msgSignature, timestamp, nonce, echostr) {
    // 1) sha1(sort(token, timestamp, nonce, echostr)) === msgSignature
    // 2) AES-256-CBC 解密 echostr → 去掉16字节随机串+4字节长度 → 得到明文 echostr
    // 3) 返回明文 echostr
    throw new Error('verifyURL 待按官方加解密文档实现（建议引入 wecom-crypt 等 SDK）');
  }

  // POST 消息解密
  decrypt(postXml) {
    // 返回 { msgtype, content, fromUserid, chatid, chattype, ... }
    throw new Error('decrypt 待按官方加解密文档实现（建议引入 wecom-crypt 等 SDK）');
  }
}

let crypt = null; // 配置回调后在下面初始化

const server = http.createServer(async (req, res) => {
  try {
    const u = new URL(req.url, `http://127.0.0.1:${PORT}`);

    // 回调 URL 验证（企微后台点「保存」时发 GET）
    if (req.method === 'GET' && u.pathname === '/wecom/callback') {
      const q = u.searchParams;
      const plain = crypt.verifyURL(q.get('msg_signature'), q.get('timestamp'), q.get('nonce'), q.get('echostr'));
      res.end(plain);
      return;
    }

    // 接收消息
    if (req.method === 'POST' && u.pathname === '/wecom/callback') {
      const body = await new Promise((r) => { const c = []; req.on('data', (x) => c.push(x)); req.on('end', () => r(Buffer.concat(c).toString())); });
      const msg = crypt.decrypt(body);
      console.log(`[收到] chattype=${msg.chattype} from=${msg.fromUserid} content=${JSON.stringify(msg.content)}`);

      // 被动回复：加密后返回 XML（5 秒内）。也可改用「主动回复消息」API
      // 这里仅演示收到后通过主动回复发一条文本（需要 chatid/userid）
      if (msg.msgtype === 'text' && msg.content.includes('测试')) {
        await proactiveReply(msg, '✅ 企业微信智能机器人验证成功！我能收到你的 @ 消息并回复。');
      }
      res.end('success');
      return;
    }

    // 健康检查
    if (u.pathname === '/health') {
      res.setHeader('Content-Type', 'application/json');
      res.end(JSON.stringify({ ok: true, configured: !!(CORPID && BOT_SECRET) }));
      return;
    }

    res.statusCode = 404;
    res.end('not found');
  } catch (e) {
    console.error('请求处理失败:', e);
    res.statusCode = 500;
    res.end('error: ' + e.message);
  }
});

async function proactiveReply(msg, text) {
  const token = await getAccessToken();
  // 智能机器人主动回复：调用 aibot 发消息接口（官方文档「主动回复消息」）
  // 群聊用 chatid，单聊用 userid。此处给出请求体框架，实际字段以官方文档为准。
  const url = `https://qyapi.weixin.qq.com/cgi-bin/aibot/send_msg?access_token=${token}`;
  const payload = {
    chatid: msg.chattype === 'group' ? msg.chatid : undefined,
    userid: msg.chattype === 'single' ? msg.fromUserid : undefined,
    msgtype: 'text',
    text: { content: text },
  };
  const r = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  }).then((r) => r.json());
  console.log('[回复结果]', r);
  if (r.errcode !== 0) console.error('回复失败，请核对 aibotid/接口字段（官方文档 101138）');
}

server.listen(PORT, '0.0.0.0', () => {
  console.log(`企业微信机器人验证服务已启动：http://0.0.0.0:${PORT}/wecom/callback`);
  console.log('请在企微后台「智能机器人 → 接收消息」配置此回调 URL（需公网可达，可用内网穿透）');
  console.log('凭证配置：', CORPID ? `corpid=${CORPID.slice(0, 6)}...` : '❌ 未设置 WECOM_CORPID/WECOM_BOT_SECRET');
  console.log('提示：消息加解密建议引入官方/社区 SDK（wecom-crypt），本脚本已留好接口');
});
