# WeChat OpenClaw 适配器

基于 iLink Bot API 的微信适配器，通过 OpenClaw 代理服务与微信对接。

## 功能特性

- 私聊消息收发（iLink Bot API 仅支持私聊）
- AES-128-ECB 加密的 CDN 媒体上传/下载（图片、语音、视频、文件）
- 扫码登录 + Token 自动持久化
- 打字指示器（"正在输入"）
- 会话过期自动暂停与恢复
- 语音转文字（使用微信自带 ASR 结果）
- SILK 语音转 WAV 转码（需安装可选依赖 `graiax-silkcoder`）
- Markdown → 纯文本自动转换

## 快速开始

### 第一步：启用适配器

在 WebUI **适配器管理 → WeChat OpenClaw → 配置** 页面中，将 **启用适配器** 设为开启，然后**重启应用**。

> 首次启用时无需填写 BOT_TOKEN 和 ACCOUNT_ID，这些会在扫码登录后自动获取。

### 第二步：扫码登录

适配器启用后，通过 API 完成扫码登录：

**1. 获取二维码**

```bash
curl -X POST http://your-host/api/adapters/wechat_openclaw/login/qr/start \
  -H "Authorization: Bearer <your_jwt_token>"
```

返回：

```json
{
  "success": true,
  "qrcode_url": "https://...",
  "session_key": "abc123"
}
```

用微信扫描 `qrcode_url` 对应的二维码。

**2. 等待扫码确认**

```bash
curl -X POST "http://your-host/api/adapters/wechat_openclaw/login/qr/wait?session_key=abc123" \
  -H "Authorization: Bearer <your_jwt_token>"
```

这是长轮询接口，扫码确认后返回：

```json
{
  "success": true,
  "status": 2,
  "message": "登录成功",
  "account_id": "...",
  "bot_token": "..."
}
```

登录成功后，凭证自动保存到配置，消息监控自动启动。

### 手动配置（替代方案）

如果已有凭证，可在 WebUI 配置页面直接填写：

| 配置项 | 说明 |
|--------|------|
| **Bot Token** | iLink Bot API 认证 Token |
| **账号 ID** | iLink Bot 的 account_id |
| **关联用户 ID** | 微信登录账号自身的 user_id（可选） |

填写后重启应用。

## API 端点

所有端点前缀：`/api/adapters/wechat_openclaw`

| 端点 | 方法 | 说明 |
|------|------|------|
| `/login/qr/start` | POST | 获取登录二维码 |
| `/login/qr/wait?session_key=xxx` | POST | 等待扫码确认（长轮询，最长 480s） |
| `/logout` | POST | 登出并清除凭证 |
| `/status` | GET | 查询连接状态 |

## 配置项

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| ENABLED | false | 启用适配器 |
| BASE_URL | `https://ilinkai.weixin.qq.com` | iLink API 地址 |
| CDN_BASE_URL | `https://cdn.weixin.qq.com` | CDN 媒体地址 |
| ACCOUNT_ID | | 账号 ID（登录后自动获取） |
| BOT_TOKEN | | Bot Token（登录后自动获取） |
| LINKED_USER_ID | | 微信登录账号的 user_id |
| POLL_TIMEOUT | 35 | 长轮询超时(秒) |
| TYPING_INDICATOR_ENABLED | true | 启用打字指示器 |
| TYPING_INTERVAL | 5 | 打字指示器间隔(秒) |

## 聊天标识规则

```
私聊: wechat_openclaw-{user_id}
```

其中 `user_id` 为微信用户的 iLink ID（通常是 `xxx@im.wechat` 格式）。

## 技术细节

### context_token

每条来自用户的消息都携带 `context_token`，回复该用户时必须附带此 token。token 仅缓存在内存中，应用重启后需等待用户再次发消息才能回复。

### CDN 媒体加密

所有通过 CDN 传输的媒体文件都使用 AES-128-ECB 加密。上传时随机生成密钥加密后上传，下载时使用消息中携带的密钥解密。

### 会话暂停

当 iLink 返回 `errcode=-14`（会话过期）时，适配器会自动暂停该账号 1 小时，避免频繁无效请求。

### 错误重试

- 消息轮询：连续失败 3 次后退避 30 秒，否则 2 秒重试
- CDN 上传：最多重试 3 次，4xx 错误立即失败
- typing_ticket 获取：指数退避（2s → 1h）

## 前置依赖

- OpenClaw iLink Bot API 服务（需自行部署或获取访问权限）
- `cryptography`（已是项目依赖）
- `httpx`（已是项目依赖）
- `graiax-silkcoder`（可选，用于 SILK 语音转 WAV）
