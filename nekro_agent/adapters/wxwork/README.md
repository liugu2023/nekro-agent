# 企业微信自建应用适配器

企业微信自建应用适配器用于连接企业微信平台，支持消息接收和主动发送功能。

## 适配器功能

- **消息接收**: 接收企业微信用户的文本、图片、语音、视频、文件等多种消息类型
- **主动发送**: 支持主动向用户或群聊发送文本、Markdown、图片、文件等消息
- **群聊支持**: 支持群聊和私聊场景，可区分不同的聊天频道
- **用户信息**: 获取企业微信成员的姓名、头像等信息

## 配置说明

### 前置条件

1. 拥有企业微信账号和企业认证信息
2. 有权限在企业微信管理后台创建自建应用

### 获取配置参数

#### 1. 获取 Corp ID

1. 登录 [企业微信管理后台](https://work.weixin.qq.com/wework_admin/frame)
2. 点击『我的企业』
3. 在『企业信息』页面查看『企业ID』

#### 2. 创建自建应用并获取 Secret 和 Agent ID

1. 登录企业微信管理后台
2. 点击『应用与集成』→『应用』
3. 点击『创建应用』，选择『自建』
4. 填写应用信息：
   - 应用名称：例如 "nekro-agent"
   - 应用描述：可选
   - 应用分类：选择合适的分类
5. 创建成功后，在应用详情页面可看到：
   - AgentID：应用编号
   - Secret：应用密钥

#### 3. 配置回调 URL

1. 在应用详情页面，找到『接收消息』部分
2. 点击『设置』
3. 填写回调配置：
   - **URL**: `http://your-domain/adapters/wxwork/callback`（需替换为实际域名）
   - **Token**: 任意自定义字符串，用于验证请求来源（例如 `your_custom_token`）
   - **EncodingAESKey**: 点击『随机生成』获取，用于消息加解密（43位字符）
4. 点击『保存』，企业微信会发送 GET 请求到你的 URL 验证有效性

### nekro-agent 配置

在 nekro-agent 的配置文件中（通常为 `.env` 或 `config.yaml`），添加以下环境变量：

```yaml
# 企业微信自建应用配置
WXWORK_CORP_ID=your_corp_id
WXWORK_CORP_SECRET=your_corp_secret
WXWORK_AGENT_ID=your_agent_id
WXWORK_TOKEN=your_custom_token
WXWORK_ENCODING_AES_KEY=your_43_char_encoding_aes_key
```

或者使用环境变量方式：

```bash
export WXWORK_CORP_ID=your_corp_id
export WXWORK_CORP_SECRET=your_corp_secret
export WXWORK_AGENT_ID=your_agent_id
export WXWORK_TOKEN=your_custom_token
export WXWORK_ENCODING_AES_KEY=your_43_char_encoding_aes_key
```

## 配置项说明

| 配置项 | 环境变量 | 说明 | 示例 |
|------|--------|------|------|
| Corp ID | `WXWORK_CORP_ID` | 企业微信的企业 ID | `wwd12345678` |
| Corp Secret | `WXWORK_CORP_SECRET` | 自建应用的应用密钥 | `jDXXdGRvvQ...` |
| Agent ID | `WXWORK_AGENT_ID` | 自建应用的应用编号 | `1000001` |
| Token | `WXWORK_TOKEN` | 回调 URL 验证的 Token | `my_secure_token` |
| Encoding AES Key | `WXWORK_ENCODING_AES_KEY` | 消息加解密的密钥 | `1234567890abcdef1234567890abcdef1234567890a` |

## Chat Key 格式

本适配器使用以下格式标识不同的聊天频道：

- **私聊**: `wxwork-private_{userid}`
  - 例如：`wxwork-private_zhangsan`
  - 用于与企业成员的一对一聊天

- **群聊**: `wxwork-group_{chatid}`
  - 例如：`wxwork-group_wrkSFfCgAAxxxxxxxxx`
  - 用于群聊消息

## 消息类型支持

### 接收消息

- ✅ 文本消息
- ✅ 图片消息
- ✅ 语音消息
- ✅ 视频消息
- ✅ 文件消息
- ✅ 链接消息

### 发送消息

- ✅ 文本消息
- ✅ Markdown 消息
- ✅ 图片消息（自动上传）
- ✅ 文件消息（自动上传）
- ✅ @用户（文本形式）

## 故障排除

### URL 验证失败

**症状**: 配置回调 URL 时显示验证失败

**排查步骤**:
1. 确保 Token 和 EncodingAESKey 与配置文件中的值完全一致
2. 确保 URL 可以从公网访问（不在防火墙后面）
3. 检查 nekro-agent 是否正常运行
4. 查看 nekro-agent 日志，搜索 "wxwork" 相关错误信息

### 无法获取 Access Token

**症状**: 日志中显示 "获取企业微信 token 失败"

**排查步骤**:
1. 确认 Corp ID、Corp Secret 和 Agent ID 正确
2. 确保企业微信后台该应用已激活
3. 检查网络连接（可能需要配置代理）
4. 检查 API 调用频率是否超过限制

### 收不到消息

**症状**: 在企业微信发送消息给应用，但 nekro-agent 未收到

**排查步骤**:
1. 确认应用已在『接收消息』中配置了回调 URL
2. 确认企业微信后台应用的『接收消息』功能已启用
3. 查看 nekro-agent 日志中是否有消息接收记录
4. 检查回调 URL 是否正确（包括协议、域名、路径）

## 常见问题

**Q: 如何只发送给特定用户?**

A: 在发送消息时指定 `chat_key` 为 `wxwork-private_{userid}`，其中 userid 是用户在企业微信中的账号。

**Q: 如何在群聊中 @ 某个用户?**

A: 目前适配器会将 @ 信息转换为文本形式发送（例如 `@zhangsan`）。企业微信原生的 @ 功能需要通过应用权限实现，暂未支持。

**Q: 可以上传多大的文件?**

A: 企业微信的临时素材限制为 20MB，您可以上传此大小以内的文件。

**Q: 消息加密时会出现什么问题?**

A: 若 EncodingAESKey 配置错误，会导致消息解密失败。确保从企业微信后台复制的密钥完全准确（43位字符）。

## 相关链接

- [企业微信官方文档](https://developer.work.weixin.qq.com/)
- [接收消息官方文档](https://developer.work.weixin.qq.com/document/path/101033)
- [发送消息官方文档](https://developer.work.weixin.qq.com/document/path/90236)

## 更新日志

### v2.0.0

- 从"智能机器人"改造为"自建应用"模式
- 新增主动发送消息功能
- 支持群聊场景
- 支持多种消息类型的接收和发送
- 新增 Token 缓存机制，提升性能
