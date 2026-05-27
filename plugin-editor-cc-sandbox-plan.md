# 插件编辑器与独立 CC 插件生成沙盒方案

## Context

当前插件编辑器只操作 `WORKDIR_PLUGIN_DIR` 下的本地插件文件，AI 生成功能走 `nekro_agent/services/plugin/generator.py` 的普通模型调用。这个设计适合作为基础款：不想启用 Claude Code 沙盒的用户仍然可以手动编辑、保存、导入、重载插件。

新的目标不是把现有编辑器替换掉，也不是依赖用户已有 Workspace，而是新增一套**专用于插件生成/修复/升级的后台 Claude Code 沙盒**：

- 普通编辑区继续保留，作为兜底和轻量编辑入口。
- 插件生成流程独立运行在后台 CC 沙盒中，不绑定用户工作区。
- 后台 CC 沙盒可以获得更完整的插件开发上下文、专用 skill、专用后端能力，因此质量比普通模型生成更好。
- CC 沙盒需要能在受控范围内操作真实宿主机插件文件，但权限必须严格收敛。
- 由于 NA 的 GitHub 源码有时与稳定版不一致，可能对齐预览版，因此需要维护插件/模板/上下文的版本文件，并设计回退机制。

推荐边界：**插件编辑器是人工可控的源码入口；后台 CC 插件生成沙盒是高级生成/修改引擎；真实文件写入必须经过后端权限网关与版本保护，不给 CC 任意宿主机权限。**

## 设计原则

1. **现有编辑器保留**
   - Monaco 编辑、保存、导入、删除、重载、启用/禁用逻辑继续存在。
   - 普通用户不启用 CC 沙盒时，仍可完整使用插件编辑器。

2. **插件生成沙盒独立于 Workspace**
   - 不复用用户创建的 Workspace。
   - 系统维护一个后台 `plugin-dev` CC 沙盒实例或按任务临时实例。
   - 该沙盒只服务插件生成、修复、升级、迁移任务。

3. **CC 不直接拿宿主机高权限**
   - 不把整个项目根目录或数据目录裸挂给 CC 可写。
   - CC 通过专用后端 API / MCP / skill 操作文件。
   - 后端做路径白名单、文件类型限制、版本记录、diff 审核和回退。

4. **版本文件是核心保护层**
   - 每个插件文件写入前后都记录版本信息。
   - 生成任务记录所依据的 NA 版本、插件模板版本、插件 API 版本、目标文件 hash。
   - 支持从最近版本回退。

5. **生成结果默认可审阅**
   - CC 可以生成 patch 或完整文件。
   - 前端展示 diff。
   - 用户确认后再应用。
   - 只有明确选择“自动应用”的高级模式才直接写入真实插件目录。

## 推荐架构

### 1. 基础编辑器层

保留现有文件：

- `frontend/src/pages/plugins/editor.tsx`
- `frontend/src/services/api/plugin-editor.ts`
- `nekro_agent/routers/plugin_editor.py`

现有功能继续作为基础款：

- 文件列表
- 文件读取
- 文件保存
- 文件删除
- 新建模板
- 导入插件
- 重载插件
- `.py` / `.py.disabled` 启停
- 普通 AI 生成可保留为“轻量生成”或后续降级入口

这层不强制依赖 Docker、Claude Code、GitHub 或 Workspace。

### 2. 独立后台 CC 插件生成沙盒

新增一个系统级服务：`PluginDevSandboxService`。

建议后端位置：

- `nekro_agent/services/plugin_dev/sandbox.py`
- `nekro_agent/services/plugin_dev/tasks.py`
- `nekro_agent/services/plugin_dev/versioning.py`
- `nekro_agent/services/plugin_dev/host_file_gateway.py`
- `nekro_agent/routers/plugin_dev.py`

职责：

1. 管理后台 CC 沙盒生命周期。
2. 初始化插件开发专用工作目录。
3. 注入插件开发专用 skill。
4. 将必要上下文同步给 CC：
   - 当前插件代码
   - NekroAgent 插件 API 摘要
   - 稳定版/预览版差异说明
   - 版本文件
   - 用户需求
5. 接收 CC 生成结果。
6. 通过受限后端网关读写真实插件文件。
7. 记录版本和回退点。

后台沙盒可以有两种实现方式：

#### 推荐 MVP：常驻单例沙盒

- 系统启动后不自动启动，首次生成时懒启动。
- 全局只有一个 `plugin-dev` 沙盒。
- 任务串行排队，避免多个 CC 同时改同一个插件文件。
- 复用会话上下文，有利于质量和速度。

优点：

- 启动成本低。
- 生成更快。
- 方便保留插件开发知识。

缺点：

- 需要做好任务隔离和上下文清理。
- 并发能力较弱。

#### 后续增强：按任务临时沙盒

- 每个生成任务创建临时 CC 沙盒。
- 任务结束后销毁。
- 更隔离，但启动更慢。

### 3. 插件开发专用工作目录

后台 CC 沙盒内部建议使用独立目录：

```text
/plugin-dev/
  workspace/
    current/
      plugin.py
      context.md
      version.json
    output/
      result.py
      result.patch
      summary.md
    docs/
      plugin-api.md
      stable-api.md
      preview-api.md
```

宿主机对应目录可以放在数据目录内，例如：

```text
{OsEnv.DATA_DIR}/plugin_dev_sandbox/
```

注意：这个目录不是 `WORKDIR_PLUGIN_DIR`，它只是 CC 的工作副本目录。

真实插件目录仍是：

```text
WORKDIR_PLUGIN_DIR
```

CC 默认只能操作工作副本目录；要读取/写入真实插件文件，必须走后端受限网关。

## 版本文件设计

### 1. 全局插件开发版本文件

新增文件建议：

```text
{OsEnv.DATA_DIR}/plugin_dev/version.json
```

内容示例：

```json
{
  "schema_version": 1,
  "nekro_agent_channel": "preview",
  "nekro_agent_git_commit": "399e564...",
  "plugin_api_version": "2026.05-preview",
  "stable_plugin_api_version": "2026.04-stable",
  "template_version": "2",
  "updated_at": "2026-05-24T00:00:00Z",
  "notes": "当前源码更接近预览版插件 API，生成时优先使用 preview 文档。"
}
```

用途：

- 告诉 CC 当前 NA 源码对齐的是稳定版还是预览版。
- 告诉 CC 应使用哪个插件 API 文档和模板。
- 作为任务记录的一部分，方便未来回溯“为什么当时生成了这种写法”。

### 2. 单插件版本记录

每个插件文件维护版本历史，例如：

```text
{OsEnv.DATA_DIR}/plugin_dev/history/<plugin-file-safe-name>/
  manifest.json
  20260524-153012-before.py
  20260524-153012-after.py
  20260524-153012.patch
  20260524-153012-summary.md
```

`manifest.json` 示例：

```json
{
  "file_path": "foo.py",
  "current_version_id": "20260524-153012",
  "versions": [
    {
      "version_id": "20260524-153012",
      "task_id": "plugin-dev-xxx",
      "action": "apply_cc_result",
      "before_sha256": "...",
      "after_sha256": "...",
      "plugin_api_version": "2026.05-preview",
      "nekro_agent_git_commit": "399e564...",
      "created_at": "2026-05-24T15:30:12Z",
      "summary": "增加 xxx sandbox method"
    }
  ]
}
```

### 3. 回退机制

新增后端接口：

- `GET /plugin-dev/history/{file_path:path}`
  - 查看某个插件的版本历史。
- `POST /plugin-dev/rollback/{file_path:path}`
  - 请求体包含 `version_id`。
  - 将真实插件文件回退到指定版本的 `before.py` 或 `after.py`。
- `GET /plugin-dev/version`
  - 查看当前插件开发版本信息。
- `PUT /plugin-dev/version`
  - 管理员手动设置 stable/preview 对齐信息。

回退要求：

1. 仅 Admin 可操作。
2. 回退前记录当前文件为一个新的版本点。
3. 路径必须仍在 `WORKDIR_PLUGIN_DIR` 内。
4. 回退后不自动重载插件，由用户确认重载。

## 专用 Skill 设计

新增一个给后台 CC 沙盒使用的 skill：

```text
plugin-dev/SKILL.md
```

建议来源：

- 内置技能目录或后台沙盒初始化时写入。

Skill 内容职责：

1. 解释 NekroAgent 插件结构：
   - `NekroPlugin`
   - `ConfigBase`
   - `SandboxMethodType`
   - `mount_sandbox_method`
   - `mount_cleanup_method`
   - 命令系统
   - 插件数据存储
2. 说明稳定版与预览版 API 的差异。
3. 要求生成时读取 `version.json`。
4. 要求所有修改输出 patch 或完整文件。
5. 要求不要直接假设 GitHub main 等于当前稳定版。
6. 要求使用后端提供的受限工具读写真实插件文件。
7. 要求每次结果包含：
   - 修改摘要
   - 风险点
   - 是否需要重载插件
   - 是否涉及数据迁移
   - 使用的 API 版本

Skill 中明确禁止：

- 直接编辑未知宿主机路径。
- 直接删除插件文件。
- 绕过版本记录写入。
- 生成不完整代码片段冒充完整插件文件。

## 受限宿主机文件操作后端

### 1. 为什么需要专用后端

用户希望 CC 沙盒能操作真实宿主机文件，但权限不能太高。因此不能简单给 CC 挂载整个宿主机目录并开放写权限。

推荐做法是：**CC 沙盒只能调用后端提供的受限文件网关**。

### 2. Host File Gateway 能力

新增服务：

- `nekro_agent/services/plugin_dev/host_file_gateway.py`

允许操作范围：

- 只允许 `WORKDIR_PLUGIN_DIR` 下的插件文件。
- 只允许 `.py` 与 `.py.disabled`。
- 可选允许同目录 `README.md`，但 MVP 不开放。
- 不允许访问项目源码、配置、数据库、`.env`、用户上传文件等。

能力：

- `list_plugin_files()`
- `read_plugin_file(file_path)`
- `propose_write_plugin_file(file_path, content, task_id)`
- `apply_proposed_write(file_path, proposal_id)`
- `rollback_plugin_file(file_path, version_id)`
- `get_plugin_history(file_path)`

### 3. 写入流程

推荐默认流程：

1. CC 生成结果。
2. 调用 `propose_write_plugin_file` 创建写入提案。
3. 后端生成 diff 和版本预记录。
4. 前端展示 diff。
5. 用户点击“应用”。
6. 后端执行真实写入，并记录版本。

高级模式才允许：

- CC 直接触发 `apply_proposed_write`。
- 但仍必须经过：
  - 路径校验
  - 文件类型校验
  - before/after 版本记录
  - 最大文件大小限制
  - 任务来源校验

### 4. 权限控制

后端 API：

- Web 管理接口仍使用 `@require_role(Role.Admin)`。
- 给 CC 沙盒调用的内部接口使用专用 token。
- token 只对 plugin-dev 沙盒有效。
- token 只允许访问 `/internal/plugin-dev/*`。

内部接口示例：

- `GET /internal/plugin-dev/files`
- `GET /internal/plugin-dev/file?path=foo.py`
- `POST /internal/plugin-dev/proposals`
- `POST /internal/plugin-dev/proposals/{proposal_id}/apply`
- `GET /internal/plugin-dev/version`

安全要求：

1. token 不复用用户登录 token。
2. token 不暴露给前端。
3. token 存在后台沙盒环境变量中。
4. 后端校验请求来源和 token。
5. 所有写入都写审计日志。

## 后端 API 设计

### 管理侧接口

新增 router：`nekro_agent/routers/plugin_dev.py`

接口：

- `GET /plugin-dev/status`
  - 查看后台 CC 插件沙盒状态。
- `POST /plugin-dev/start`
  - 启动后台沙盒。
- `POST /plugin-dev/stop`
  - 停止后台沙盒。
- `POST /plugin-dev/generate`
  - 提交插件生成/修改任务。
- `GET /plugin-dev/tasks/{task_id}`
  - 获取任务状态、日志、结果、proposal。
- `GET /plugin-dev/tasks/{task_id}/stream`
  - SSE 推送任务进度。
- `POST /plugin-dev/tasks/{task_id}/cancel`
  - 取消任务。
- `POST /plugin-dev/proposals/{proposal_id}/apply`
  - 应用 CC 写入提案。
- `DELETE /plugin-dev/proposals/{proposal_id}`
  - 丢弃提案。
- `GET /plugin-dev/history/{file_path:path}`
  - 查看版本历史。
- `POST /plugin-dev/rollback/{file_path:path}`
  - 回退文件。
- `GET /plugin-dev/version`
  - 查看版本文件。
- `PUT /plugin-dev/version`
  - 更新版本文件。

### 内部侧接口

仅后台 CC 沙盒可调用：

- `GET /internal/plugin-dev/version`
- `GET /internal/plugin-dev/files`
- `GET /internal/plugin-dev/file?path=foo.py`
- `POST /internal/plugin-dev/proposals`

MVP 建议不开放内部直接 apply，只能提 proposal。

## 前端设计

### 1. 保留基础编辑器

现有编辑器区域不变。

新增一个模式切换或高级面板：

- “基础编辑”
- “Claude Code 生成”

基础编辑说明：

- 不依赖 CC 沙盒。
- 适合快速修改、小修小补、没有 Docker/CC 的环境。

### 2. Claude Code 生成面板

位置：

- 桌面端右侧 AI 面板。
- 移动端生成器 Tab 内。

展示：

- 后台插件生成沙盒状态。
- 当前版本通道：stable / preview。
- 当前插件 API 版本。
- 当前任务状态。
- 生成日志。
- diff 预览。
- 写入提案。
- 回退入口。

操作：

- “启动插件生成沙盒”
- “让 Claude Code 修改当前插件”
- “取消任务”
- “应用提案”
- “丢弃提案”
- “查看历史”
- “回退到此版本”

### 3. 发送任务内容

前端向 `/plugin-dev/generate` 发送：

```json
{
  "file_path": "foo.py",
  "prompt": "用户需求",
  "current_code": "当前编辑器内容",
  "base_code": "保存前原始内容",
  "dirty": true,
  "mode": "proposal"
}
```

说明：

- 仍然发送当前编辑器内容，保证未保存变更也能被 CC 看到。
- 但真实写入由 proposal/apply 控制。

### 4. 应用结果

两种应用方式：

#### 应用到编辑器

- 只更新 Monaco `code`。
- 不写磁盘。
- 适合用户想继续人工调整。

#### 应用到真实插件文件

- 调 `/plugin-dev/proposals/{proposal_id}/apply`。
- 后端写版本记录。
- 后端写入 `WORKDIR_PLUGIN_DIR`。
- 前端刷新文件内容。
- 提示用户是否重载插件。

## 任务状态设计

任务状态：

- `pending`
- `starting_sandbox`
- `syncing_context`
- `running_cc`
- `creating_proposal`
- `waiting_apply`
- `applied`
- `failed`
- `cancelled`

任务结果包含：

- `task_id`
- `file_path`
- `status`
- `summary`
- `logs`
- `proposal_id`
- `diff`
- `result_code`
- `version_info`
- `error`

## 与现有 Workspace 的关系

这套插件生成沙盒不依赖用户 Workspace。

原因：

1. 用户 Workspace 是通用工作区，可能绑定频道、资源、MCP、记忆，不适合作为系统级插件生成引擎。
2. 插件生成需要特殊宿主机文件网关权限，不能授予普通 Workspace。
3. 独立后台沙盒更容易做上下文优化和质量控制。
4. 不会污染用户 Workspace 的会话和通讯日志。

但可以复用底层能力：

- CC 沙盒容器启动方式。
- `CCSandboxClient` 流式通讯方式。
- SSE 任务日志模式。
- Docker 镜像配置。
- runtime policy 概念。

## 关键文件

后端新增：

- `nekro_agent/routers/plugin_dev.py`
- `nekro_agent/services/plugin_dev/sandbox.py`
- `nekro_agent/services/plugin_dev/tasks.py`
- `nekro_agent/services/plugin_dev/versioning.py`
- `nekro_agent/services/plugin_dev/host_file_gateway.py`
- `nekro_agent/services/plugin_dev/skill.py`
- `nekro_agent/schemas/plugin_dev.py`

后端修改：

- `nekro_agent/routers/__init__.py`
  - 挂载 `plugin_dev` router。
- `nekro_agent/core/config.py`
  - 增加插件生成沙盒配置。
- `nekro_agent/core/os_env.py`
  - 增加 plugin_dev 数据目录常量。

前端修改：

- `frontend/src/pages/plugins/editor.tsx`
- `frontend/src/services/api/plugin-editor.ts`
- 新增 `frontend/src/services/api/plugin-dev.ts`
- `frontend/src/locales/zh-CN/plugins.json`
- `frontend/src/locales/en-US/plugins.json`

可能复用：

- `nekro_agent/services/workspace/client.py`
- `nekro_agent/services/workspace/container.py` 的部分容器管理逻辑
- `frontend/src/services/api/utils/stream.ts`

## MVP 范围

第一版建议只做：

1. 保留现有编辑器。
2. 新增后台 plugin-dev CC 沙盒状态接口。
3. 新增 `/plugin-dev/generate` 任务。
4. CC 根据当前编辑器代码生成完整结果。
5. 后端创建写入 proposal，不自动写真实插件文件。
6. 前端展示 diff。
7. 用户可以选择：
   - 应用到编辑器。
   - 应用到真实插件文件。
8. 应用真实文件前后记录版本。
9. 支持查看历史和回退。
10. 内置 plugin-dev skill 和 version.json。

MVP 暂不做：

- 多沙盒并发。
- 自动应用模式。
- 复杂多文件插件工程。
- CC 直接执行插件测试。
- 直接从 GitHub 拉稳定版/预览版对比。

## 验证方案

后端：

1. 非 Admin 无法调用管理接口。
2. CC 内部 token 无法访问非 `/internal/plugin-dev/*` 接口。
3. 路径穿越、绝对路径、非 `.py` / `.py.disabled` 被拒绝。
4. 生成任务不会直接修改 `WORKDIR_PLUGIN_DIR`。
5. proposal apply 前后会生成版本记录。
6. rollback 会记录新的版本点，并恢复目标内容。
7. version.json 能被读取并注入 CC 上下文。
8. stopped sandbox 能自动懒启动或返回明确错误。

前端：

1. 基础编辑器不启动 CC 也能正常使用。
2. CC 生成面板能显示后台沙盒状态。
3. 有未保存变更时，发送给 CC 的是当前编辑器内容。
4. CC 返回后能显示 diff。
5. “应用到编辑器”不会写磁盘。
6. “应用到真实文件”会写磁盘并刷新编辑器内容。
7. 回退后文件内容恢复，且可继续重载插件。

命令验证：

- 后端改动后运行 `poe lint`。
- 前端改动后运行 `poe frontend-check`。
- UI 改动需启动前端开发服务并手动验证插件编辑器主流程。