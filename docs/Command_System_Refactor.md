# 命令系统独立化重构设计文档

**文档版本:** 1.0
**创建日期:** 2026-02-26
**状态:** 规划中

---

## 目录

1. [现状分析](#现状分析)
2. [问题诊断](#问题诊断)
3. [理想架构](#理想架构)
4. [详细设计](#详细设计)
5. [实施路线图](#实施路线图)
6. [代码示例](#代码示例)
7. [风险评估](#风险评估)

---

## 现状分析

### 当前命令系统架构

```
现状结构：
┌─────────────────────────────────────────┐
│         OneBot V11 适配器              │
│  (nekro_agent/adapters/onebot_v11)     │
├─────────────────────────────────────────┤
│    命令处理 (matchers/command.py)       │
│  • @on_command 装饰器                   │
│  • 依赖 NoneBot 框架                    │
│  • 紧耦合到 OneBot 协议                 │
│                                         │
│  包含的命令：                           │
│  • /reset - 重置对话                    │
│  • /stop-stream - 停止流回复             │
│  • /inspect - 查询频道信息              │
│  • /exec - 执行代码                     │
│  • /code_log - 查看执行日志             │
│  • /system - 添加系统消息               │
│  • /debug_on - 调试模式                 │
│  • /na_help - 帮助信息                  │
│  • ... 约20+个命令                      │
└─────────────────────────────────────────┘
        ↓
   ❌ 其他适配器无法使用这些命令
   Discord / Telegram / Email / WeChat
```

### 适配器对命令的需求

| 适配器 | 支持状态 | 问题 |
|--------|--------|------|
| OneBot V11 | ✅ 完全支持 | 基础设施完整 |
| Discord | ❌ 无法使用 | 需要在 Discord 适配器中重新实现 |
| Telegram | ❌ 无法使用 | 需要在 Telegram 适配器中重新实现 |
| Email | ❌ 无法使用 | 通过邮件命令复杂 |
| WeChat/wxwork | ❌ 无法使用 | 微信特定处理 |
| SSE | ⚠️ 部分支持 | 有独立命令系统，但未完整 |

---

## 问题诊断

### 核心问题

#### 问题1：紧耦合到 OneBot 框架

```python
# ❌ 现状 - onebot_v11/matchers/command.py
@on_command("reset", priority=5, block=True).handle()
async def _(matcher: Matcher, event: MessageEvent, bot: Bot, arg: Message = CommandArg()):
    # 依赖项：
    # - matcher (NoneBot Matcher)
    # - event (OneBot MessageEvent)
    # - bot (NoneBot Bot)
    # - arg (NoneBot Message)

    # 业务逻辑
    db_chat_channel = await DBChatChannel.get_channel(chat_key=target_chat_key)
    await db_chat_channel.reset_channel()
    await finish_with(matcher, message=f"已重置...")
```

**紧耦合的危害：**
- 命令逻辑无法跨平台复用
- 新增平台必须复制命令代码
- 命令逻辑修改需要改动适配器
- 单元测试困难

#### 问题2：命令发现困难

- 命令定义分散在各适配器
- 无统一的命令注册表
- 文档与代码不同步
- 难以维护命令帮助信息

#### 问题3：命令权限管理复杂

```python
# ❌ 权限检查分散在各命令
async def command_guard(event, bot, arg, matcher, require_advanced_command=False):
    # 每个命令都需要调用这个函数
    # 权限逻辑难以统一管理
    ...
```

#### 问题4：扩展性差

- 添加新命令需要修改适配器
- 命令配置无法动态修改
- 命令别名管理复杂
- 无法实现条件性启用/禁用

---

## 理想架构

### 整体设计

```
理想结构：
┌──────────────────────────────────────────────────┐
│         独立的命令系统                            │
│  (nekro_agent/services/command/)                 │
├──────────────────────────────────────────────────┤
│                                                   │
│  ┌─ CommandRegistry          平台无关            │
│  │  • 命令注册表                                  │
│  │  • 命令发现                                    │
│  │                                                │
│  ├─ CommandExecutor                             │
│  │  • 命令路由                                    │
│  │  • 权限验证                                    │
│  │  • 参数解析                                    │
│  │                                                │
│  ├─ Built-in Commands                           │
│  │  • reset.py                                    │
│  │  • stop_stream.py                             │
│  │  • exec.py                                    │
│  │  • inspect.py                                 │
│  │  ... (纯业务逻辑)                              │
│  │                                                │
│  └─ CommandPermission                           │
│     • 权限检查                                    │
│     • 角色管理                                    │
│                                                   │
└──────────────────────────────────────────────────┘
         ↑         ↑         ↑         ↑
         │         │         │         │
    ┌────────┬─────────┬─────────┬──────────┐
    │ OneBot │ Discord │Telegram │   Email  │
    │ 适配器 │ 适配器  │ 适配器  │  适配器  │
    └────────┴─────────┴─────────┴──────────┘
```

### 关键设计原则

#### 1. **平台无关性**
- 命令接口不依赖任何平台特定的类或库
- 使用通用的数据结构（Pydantic BaseModel）
- 所有平台调用统一的命令接口

#### 2. **职责分离**
- **命令系统** - 负责命令定义和执行逻辑
- **适配器** - 负责消息解析和格式转换
- **权限系统** - 负责访问控制

#### 3. **可扩展性**
- 插件化的命令注册
- 支持动态命令加载
- 命令可以启用/禁用

#### 4. **可维护性**
- 统一的命令文档生成
- 自动化的命令帮助
- 便于调试和日志记录

---

## 详细设计

### 1. 命令请求/响应模型

```python
# services/command/schemas.py

from pydantic import BaseModel, Field
from typing import Any, Dict, Optional
from enum import Enum

class CommandExecutionContext(BaseModel):
    """命令执行上下文 - 平台无关"""

    # 基本信息
    user_id: str = Field(..., description="用户ID")
    chat_key: str = Field(..., description="聊天标识(如: onebot_v11-group_123456)")
    username: str = Field(..., description="用户名")

    # 权限信息
    is_super_user: bool = Field(default=False, description="是否为超级用户")
    is_advanced_user: bool = Field(default=False, description="是否为高级用户")

    # 平台特定信息（可选）
    adapter_key: str = Field(..., description="适配器标识")
    platform_event: Optional[Dict[str, Any]] = Field(
        default=None,
        description="平台特定的事件数据(例如OneBot MessageEvent)"
    )


class CommandRequest(BaseModel):
    """命令请求 - 平台无关"""

    context: CommandExecutionContext = Field(..., description="执行上下文")
    command_name: str = Field(..., description="命令名称(不含前缀)")
    args: str = Field(default="", description="命令参数(原始字符串)")
    parsed_args: Optional[Dict[str, Any]] = Field(
        default=None,
        description="解析后的参数"
    )


class CommandResponseStatus(str, Enum):
    """命令响应状态"""
    SUCCESS = "success"
    ERROR = "error"
    UNAUTHORIZED = "unauthorized"
    NOT_FOUND = "not_found"
    INVALID_ARGS = "invalid_args"


class CommandResponse(BaseModel):
    """命令响应 - 平台无关"""

    status: CommandResponseStatus = Field(..., description="响应状态")
    message: str = Field(..., description="响应消息")
    data: Optional[Dict[str, Any]] = Field(default=None, description="响应数据")
    raw_message: Optional[str] = Field(default=None, description="原始输出(如代码执行)")
```

### 2. 命令基类设计

```python
# services/command/base.py

from abc import ABC, abstractmethod
from typing import List, Optional, Dict, Any
from enum import Enum

class CommandPermission(Enum):
    """命令权限等级"""
    PUBLIC = "public"              # 公开命令
    USER = "user"                  # 普通用户
    ADVANCED = "advanced"          # 高级用户
    SUPER_USER = "super_user"      # 超级用户


class CommandMetadata(BaseModel):
    """命令元数据"""

    name: str = Field(..., description="命令名称")
    aliases: List[str] = Field(default_factory=list, description="命令别名")
    description: str = Field(..., description="命令描述")
    usage: str = Field(default="", description="使用说明")
    permission: CommandPermission = Field(
        default=CommandPermission.PUBLIC,
        description="权限要求"
    )
    enabled: bool = Field(default=True, description="是否启用")
    category: str = Field(default="general", description="命令分类")


class BaseCommand(ABC):
    """命令基类"""

    @property
    @abstractmethod
    def metadata(self) -> CommandMetadata:
        """返回命令元数据"""
        pass

    async def check_permission(
        self,
        context: CommandExecutionContext,
        metadata: Optional[CommandMetadata] = None
    ) -> tuple[bool, Optional[str]]:
        """检查权限

        Returns:
            (是否有权限, 错误信息)
        """
        meta = metadata or self.metadata

        if meta.permission == CommandPermission.PUBLIC:
            return True, None
        elif meta.permission == CommandPermission.SUPER_USER:
            if context.is_super_user:
                return True, None
            return False, "此命令仅限超级用户使用"
        elif meta.permission == CommandPermission.ADVANCED:
            if context.is_advanced_user or context.is_super_user:
                return True, None
            return False, "此命令仅限高级用户使用"
        else:  # USER
            return True, None

    async def parse_args(self, raw_args: str) -> Dict[str, Any]:
        """解析命令参数(可选覆盖)"""
        return {}

    @abstractmethod
    async def execute(
        self,
        context: CommandExecutionContext,
        args: str
    ) -> CommandResponse:
        """执行命令

        Args:
            context: 执行上下文
            args: 原始参数字符串

        Returns:
            CommandResponse: 命令响应
        """
        pass

    async def handle(
        self,
        request: CommandRequest
    ) -> CommandResponse:
        """完整的命令处理流程"""
        try:
            # 1. 检查权限
            has_perm, error_msg = await self.check_permission(request.context)
            if not has_perm:
                return CommandResponse(
                    status=CommandResponseStatus.UNAUTHORIZED,
                    message=error_msg or "权限不足"
                )

            # 2. 执行命令
            response = await self.execute(request.context, request.args)
            return response

        except ValueError as e:
            return CommandResponse(
                status=CommandResponseStatus.INVALID_ARGS,
                message=f"参数错误: {str(e)}"
            )
        except Exception as e:
            return CommandResponse(
                status=CommandResponseStatus.ERROR,
                message=f"命令执行出错: {str(e)}"
            )
```

### 3. 命令注册表

```python
# services/command/registry.py

from typing import Dict, Type, Optional, List
from functools import lru_cache

class CommandRegistry:
    """命令注册表"""

    def __init__(self):
        self._commands: Dict[str, BaseCommand] = {}
        self._command_classes: Dict[str, Type[BaseCommand]] = {}
        self._aliases: Dict[str, str] = {}  # alias -> command_name

    def register(self, command_class: Type[BaseCommand]) -> None:
        """注册命令类"""
        instance = command_class()
        metadata = instance.metadata

        # 注册主命令
        self._commands[metadata.name] = instance
        self._command_classes[metadata.name] = command_class

        # 注册别名
        for alias in metadata.aliases:
            if alias in self._aliases and self._aliases[alias] != metadata.name:
                logger.warning(f"别名冲突: {alias} 已映射到 {self._aliases[alias]}")
            self._aliases[alias] = metadata.name

    def get_command(self, name: str) -> Optional[BaseCommand]:
        """获取命令(支持别名)"""
        # 检查是否是别名
        if name in self._aliases:
            name = self._aliases[name]

        return self._commands.get(name)

    def list_commands(
        self,
        category: Optional[str] = None,
        permission: Optional[CommandPermission] = None,
        enabled_only: bool = True
    ) -> List[CommandMetadata]:
        """列出命令"""
        results = []
        for cmd in self._commands.values():
            meta = cmd.metadata

            # 过滤
            if enabled_only and not meta.enabled:
                continue
            if category and meta.category != category:
                continue
            if permission and meta.permission != permission:
                continue

            results.append(meta)

        return sorted(results, key=lambda m: m.name)

    async def execute(
        self,
        request: CommandRequest
    ) -> CommandResponse:
        """执行命令"""
        command = self.get_command(request.command_name)

        if not command:
            return CommandResponse(
                status=CommandResponseStatus.NOT_FOUND,
                message=f"命令不存在: {request.command_name}"
            )

        if not command.metadata.enabled:
            return CommandResponse(
                status=CommandResponseStatus.NOT_FOUND,
                message=f"命令已禁用: {request.command_name}"
            )

        return await command.handle(request)


# 全局单例
command_registry = CommandRegistry()
```

### 4. 命令实现示例

```python
# services/command/built_in/reset.py

from services.command.base import BaseCommand, CommandMetadata, CommandPermission, CommandResponse, CommandResponseStatus, CommandExecutionContext
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage

class ResetCommand(BaseCommand):
    """重置对话命令"""

    @property
    def metadata(self) -> CommandMetadata:
        return CommandMetadata(
            name="reset",
            aliases=["重置"],
            description="重置指定频道的对话上下文和历史消息",
            usage="reset [chat_key]\n\n例: reset onebot_v11-group_123456",
            permission=CommandPermission.USER,
            category="chat"
        )

    async def execute(
        self,
        context: CommandExecutionContext,
        args: str
    ) -> CommandResponse:
        """执行reset命令"""

        # 如果参数为空，使用当前频道
        target_chat_key = args.strip() if args.strip() else context.chat_key

        # 超级用户可以重置其他频道，普通用户只能重置当前频道
        if target_chat_key != context.chat_key:
            if not context.is_super_user:
                return CommandResponse(
                    status=CommandResponseStatus.UNAUTHORIZED,
                    message="普通用户只能重置当前频道的对话"
                )

        try:
            # 获取重置前的消息统计
            db_chat_channel = await DBChatChannel.get_channel(chat_key=target_chat_key)
            msg_cnt = await DBChatMessage.filter(
                chat_key=target_chat_key,
                send_timestamp__gte=int(db_chat_channel.conversation_start_time.timestamp()),
            ).count()

            # 执行重置
            await db_chat_channel.reset_channel()

            return CommandResponse(
                status=CommandResponseStatus.SUCCESS,
                message=f"已重置 {target_chat_key} 的对话上下文（当前会话 {msg_cnt} 条消息已归档）"
            )

        except Exception as e:
            return CommandResponse(
                status=CommandResponseStatus.ERROR,
                message=f"重置对话失败: {str(e)}"
            )


# 在模块初始化时注册
command_registry.register(ResetCommand)
```

### 5. 适配器集成

```python
# adapters/interface/base.py - 在 BaseAdapter 中添加

class BaseAdapter(ABC, Generic[TConfig]):

    async def handle_command(
        self,
        chat_key: str,
        user_id: str,
        username: str,
        command_name: str,
        args: str,
        is_super_user: bool = False,
        is_advanced_user: bool = False,
        platform_event: Optional[Dict] = None
    ) -> CommandResponse:
        """处理命令请求 - 由适配器调用"""
        from nekro_agent.services.command.schemas import CommandRequest, CommandExecutionContext
        from nekro_agent.services.command.registry import command_registry

        context = CommandExecutionContext(
            user_id=user_id,
            chat_key=chat_key,
            username=username,
            is_super_user=is_super_user,
            is_advanced_user=is_advanced_user,
            adapter_key=self.key,
            platform_event=platform_event
        )

        request = CommandRequest(
            context=context,
            command_name=command_name,
            args=args
        )

        return await command_registry.execute(request)
```

### 6. OneBot 适配器的迁移示例

```python
# adapters/onebot_v11/matchers/command.py - 重构后

from nekro_agent.adapters.onebot_v11.adapter import OnebotV11Adapter

adapter_instance = OnebotV11Adapter()

@on_command("reset", priority=5, block=True).handle()
async def _(matcher: Matcher, event: MessageEvent, bot: Bot, arg: Message = CommandArg()):
    """重置对话"""

    # 从OneBot事件提取信息
    chat_key = f"onebot_v11-{...}"  # 构建chat_key
    user_id = event.get_user_id()
    username = await get_username(user_id, bot)
    command_args = str(arg).strip()

    # 调用适配器的统一命令处理
    response = await adapter_instance.handle_command(
        chat_key=chat_key,
        user_id=user_id,
        username=username,
        command_name="reset",
        args=command_args,
        is_super_user=user_id in config.SUPER_USERS,
        platform_event={"event": event, "bot": bot}  # 可选的平台特定数据
    )

    # 将响应转换为OneBot消息
    await matcher.send(response.message)
```

---

## 实施路线图

### Phase 1: 基础设施（第1-2周）

- [ ] 创建 `services/command/` 目录结构
- [ ] 实现 `schemas.py` - 命令请求/响应模型
- [ ] 实现 `base.py` - 命令基类
- [ ] 实现 `registry.py` - 命令注册表
- [ ] 创建单元测试框架
- [ ] 文档：架构设计

### Phase 2: 迁移核心命令（第3-4周）

- [ ] 迁移 `/reset` 命令
- [ ] 迁移 `/stop-stream` 命令
- [ ] 迁移 `/exec` 命令
- [ ] 迁移 `/inspect` 命令
- [ ] 迁移 `/code_log` 命令
- [ ] 迁移 `/system` 命令
- [ ] 迁移 `/debug_on` 命令
- [ ] 单元测试覆盖率 >80%

### Phase 3: 适配器集成（第5-6周）

- [ ] OneBot V11 适配器集成
- [ ] Discord 适配器集成
- [ ] Telegram 适配器集成
- [ ] Email 适配器集成
- [ ] 集成测试

### Phase 4: 高级功能（第7-8周）

- [ ] 命令权限管理 UI
- [ ] 动态启用/禁用命令
- [ ] 命令性能监控
- [ ] 文档完善

### Phase 5: 废弃和清理（第9周）

- [ ] 废弃旧的OneBot命令系统
- [ ] 迁移所有引用
- [ ] 清理废弃代码
- [ ] 发布版本说明

---

## 代码示例

### 完整的 Discord 命令处理示例

```python
# adapters/discord/adapter.py

class DiscordAdapter(BaseAdapter[DiscordConfig]):

    async def forward_message(self, request: PlatformSendRequest) -> PlatformSendResponse:
        """处理Discord消息"""

        # ... 其他处理 ...

        # 检查是否是命令
        if content.startswith("/"):
            parts = content[1:].split(" ", 1)
            command_name = parts[0]
            command_args = parts[1] if len(parts) > 1 else ""

            # 获取用户信息
            user_id = request.user_id
            username = await self.get_username(user_id)
            chat_key = request.chat_key

            # 调用统一命令处理
            response = await self.handle_command(
                chat_key=chat_key,
                user_id=user_id,
                username=username,
                command_name=command_name,
                args=command_args,
                is_super_user=user_id in config.SUPER_USERS,
                platform_event={"message": request}
            )

            # 发送响应
            return PlatformSendResponse(
                success=response.status == CommandResponseStatus.SUCCESS,
                message=response.message,
                message_id=...
            )
        else:
            # 正常消息处理
            return await self._handle_normal_message(request)
```

### 帮助命令实现

```python
# services/command/built_in/help.py

class HelpCommand(BaseCommand):
    """帮助命令"""

    @property
    def metadata(self) -> CommandMetadata:
        return CommandMetadata(
            name="na_help",
            aliases=["help", "帮助"],
            description="显示可用命令的帮助信息",
            usage="na_help [command_name]\n\n例: na_help reset",
            permission=CommandPermission.PUBLIC,
            category="system"
        )

    async def execute(
        self,
        context: CommandExecutionContext,
        args: str
    ) -> CommandResponse:
        """执行help命令"""
        from nekro_agent.services.command.registry import command_registry

        if not args.strip():
            # 显示所有可用命令
            commands = command_registry.list_commands()

            message = "📚 **可用命令列表:**\n\n"

            by_category = {}
            for cmd in commands:
                cat = cmd.category
                if cat not in by_category:
                    by_category[cat] = []
                by_category[cat].append(cmd)

            for category in sorted(by_category.keys()):
                message += f"**{category.upper()}:**\n"
                for cmd in by_category[category]:
                    perm = f"[{cmd.permission.value}]" if cmd.permission != CommandPermission.PUBLIC else ""
                    message += f"  • `/{cmd.name}` {perm} - {cmd.description}\n"
                message += "\n"

            message += "使用 `/na_help <command_name>` 查看具体命令用法"

            return CommandResponse(
                status=CommandResponseStatus.SUCCESS,
                message=message
            )

        else:
            # 显示特定命令的帮助
            cmd_name = args.strip()
            cmd = command_registry.get_command(cmd_name)

            if not cmd:
                return CommandResponse(
                    status=CommandResponseStatus.NOT_FOUND,
                    message=f"命令不存在: {cmd_name}"
                )

            meta = cmd.metadata
            message = f"""
**命令:** `/{meta.name}`
**描述:** {meta.description}
**权限:** {meta.permission.value}
**分类:** {meta.category}
**别名:** {', '.join(meta.aliases) if meta.aliases else '无'}

**使用方法:**
```
{meta.usage}
```
"""

            return CommandResponse(
                status=CommandResponseStatus.SUCCESS,
                message=message.strip()
            )
```

---

## 风险评估

### 技术风险

| 风险 | 概率 | 影响 | 缓解策略 |
|------|------|------|---------|
| 命令执行行为改变 | 中 | 高 | 详细的单元和集成测试 |
| 权限检查遗漏 | 中 | 中 | 权限模块单独评审 |
| 性能下降 | 低 | 中 | 基准测试对比 |
| 向后兼容性 | 低 | 高 | 长期兼容旧命令格式 |

### 迁移风险

| 风险 | 缓解策略 |
|------|---------|
| 命令中断服务 | 分批迁移，保留旧系统并行运行 |
| 用户习惯改变 | 保持命令名称和别名一致性 |
| 文档过期 | 自动生成命令文档 |

### 缓解策略

1. **充分的测试** - 新系统与旧系统并行测试
2. **渐进迁移** - 逐个命令迁移，逐个适配器适配
3. **自动化检查** - CI/CD 流程验证兼容性
4. **降级方案** - 必要时快速回滚到旧系统

---

## 预期收益

### 短期（1-2个月）

- ✅ 命令系统可跨平台使用
- ✅ 代码重复度大幅降低
- ✅ 开发效率提升 30%+

### 中期（3-6个月）

- ✅ Discord、Telegram 等平台完全支持命令
- ✅ 用户体验一致性提升
- ✅ Bug 修复时间减少 50%

### 长期（6个月+）

- ✅ 易于添加新平台
- ✅ 命令系统可插件化
- ✅ 维护成本显著降低

---

## 相关文档

- [适配器架构设计](./Adapter_Architecture.md)
- [权限系统设计](./Permission_System.md)
- [命令开发指南](./Command_Development_Guide.md)

---

## 讨论与反馈

本文档为初稿，欢迎反馈和讨论：

- **架构是否合理？**
- **是否遗漏了某些命令？**
- **迁移步骤是否可行？**
- **性能考虑是否完整？**

