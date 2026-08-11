# CC 插件编辑器分支：审查分析与测试流程

> 分支：`feature/plugin-dev-cc-editor-workflow`（对比 `main`，约 +11.5k 行 / 50 文件）
> 审查方式：8 视角静态审查（逐行扫描 ×3 分区、删除行为审计、跨文件追踪、复用/简化/效率/层次/规范）→ 逐项人工验证 → 修复 → 修复后全量复审
> 文档更新日期：2026-08-10

---

## 1. 功能架构分析

### 1.1 分支内容概览

| 模块 | 内容 |
| --- | --- |
| `nekro_agent/services/plugin_dev/` | 新增：CC 沙盒生命周期（sandbox.py）、生成任务与提案流水线（tasks.py）、版本记录与回滚（versioning.py）、宿主自检封装（self_check.py）、插件文件网关（host_file_gateway.py） |
| `nekro_agent/services/plugin/checker.py` | 新增：插件静态/加载/冒烟检查器（隔离子进程 + 临时数据目录） |
| `nekro_agent/services/plugin/collector.py` | 大改：一等公民的包形态（文件夹）插件支持、来源优先级去重（内置 > 工作目录 > 云端） |
| `nekro_agent/runtime_bootstrap.py` | 从 `__init__.py` 拆出的启动逻辑（复审确认逻辑等价迁移，无行为丢失） |
| `nekro_agent/routers/plugin_dev.py` | 管理端 API + 沙盒内部网关（token 鉴权，仅 static 检查 / 提案创建） |
| `run_nekro_cli.py` | `na plugin check` CLI（子进程隔离检查） |
| `frontend/src/pages/plugins/cc-editor.tsx` | 新页面：CC 驱动的插件编辑器（任务流、提案审查、历史回滚） |
| `frontend/src/pages/plugins/editor.tsx` | 旧编辑器改造：包插件支持、运行时启停 |
| OneBot 适配器 | 每频道入群欢迎 / 退群提醒开关（迁移到核心 effective config） |

### 1.2 核心数据流

```
前端 cc-editor
  └─ POST /plugin-dev/generate ──► create_task（队列上限 3，全局互斥执行）
       └─ _execute_task（最多 3 轮生成/修复）
            ├─ prepare_task_workspace：把当前插件代码暂存到沙盒工作副本
            ├─ PluginDevSandboxService.start()：确保 CC 容器运行（挂载工作区 + 只读参考源码）
            ├─ stream_generate：CC 在容器内编辑工作副本，并执行注入的静态自检命令
            ├─ 候选收集：优先内部网关提案，否则扫描工作副本 diff
            ├─ 宿主机 static 复核（checker.py，绝不执行候选代码）
            └─ create_proposal（diff + 全文件集 + before_sha256）
  └─ 用户审查 diff → POST /proposals/{id}/apply
       └─ smoke 级复核（隔离子进程真实加载）→ 并发修改校验 → 原子多文件写入
          → 版本记录（可回滚）→ 失败时全量回滚
```

### 1.3 信任边界

- **CC 沙盒 → 宿主**：只能通过内部网关（`X-Internal-API-Token`，每次容器重建/停止轮换），能力限定为读文件、static 自检、创建提案；不能直接写真实插件目录。
- **候选代码执行时机**：迭代期一律 static（AST 级，不 import 候选、不加入 sys.path）；只有用户确认应用后才做 smoke（等同于用户授权运行）。
- **路径安全**：`plugin_editor` 与 `host_file_gateway` 双侧做路径穿越/符号链接校验（注意：两套实现并行，规则已有细微差异，见 §5 技术债）。

---

## 2. 审查结论摘要

初审共产出 56 个候选，去重后 44 个，逐项读码验证：

- **19 个正确性缺陷确认并已全部修复**（含 3 个会让核心功能基本不可用的高严重度缺陷）
- **2 个判定为有意设计**，保留但需要写入变更说明（见 §5）
- **质量类**：5 个死代码/冗余已清理，3 个事件循环阻塞/热路径浪费已修复；大型重复实现（与 workspace 模块的 4 处重复等）记为技术债
- **额外收获**：发现 `poe frontend-typecheck` 是空跑（solution tsconfig + `tsc --noEmit` 不检查任何文件），修正为 `tsc -b` 并清零暴露出的 108 个存量类型错误（其中含 `themeConfig.ts` 的 `alpha` 未导入这类真实潜在运行时崩溃）

修复后门禁状态（2026-08-10 实测）：

| 门禁 | 命令 | 结果 |
| --- | --- | --- |
| 后端 lint | `poe lint` | ✅ 通过 |
| 后端类型 | `poe typecheck` | ✅ 0 错误 |
| 前端类型+lint | `poe frontend-check` | ✅ 0 错误 / 0 警告（现为真实检查） |
| 后端测试 | `poe test` | ✅ 203 passed |

---

## 3. 缺陷与修复对照表

### 3.1 高严重度（功能不可用级）

| # | 缺陷 | 根因 | 修复 |
| --- | --- | --- | --- |
| C-1 | 每次 `start()` 都销毁重建健康容器并轮换 token（并发操作会杀死运行中的 CC 会话） | aiodocker `Stream` 无 `__aiter__`，`async for` 恒抛 TypeError 被吞，挂载检测恒 False | `sandbox.py::_container_file_exists` 改用 `stream.read_out()` 读到 EOF；state 记录 `reference_source_mounted` 防重建死循环 |
| C-14 | SSE 流对 404/401/502 静默结束，`isGenerating` 永久 true，页面锁死 | `stream.ts` 自定义 `onopen` 替换了库的默认校验且不查 `response.ok`；`onclose` 在 autoReconnect 下静默 return | `onopen` 校验 ok + content-type；4xx 定义为 `FatalStreamError` 停止重连并回调 `onError`（触发轮询兜底，兜底 404 会复位 `isGenerating`） |
| C-4 | 单个损坏/旧版提案文件毒化所有任务（每个任务烧完一轮 CC 后必然失败） | `get_latest_pending_proposal_for_task` 在按 task_id 过滤前对目录内每个文件无保护 `model_validate` | 逐文件 try/except，坏文件跳过并告警 |

### 3.2 数据正确性

| # | 缺陷 | 修复 |
| --- | --- | --- |
| C-2 | 包任务中 CC 删除主文件时，任意其他文件的内容会被写入主文件（提案静默损坏插件目录） | `_primary_content_from_files` 去掉任意回退；工作副本候选缺主文件时给出明确失败进入修复轮（内部网关本就拒绝删除主文件） |
| C-18 | 任务运行中刷新页面，任务产出结果时会用磁盘内容覆盖已恢复的未保存代码（挂载 effect 闭包捕获 `selectedFile=''`） | `syncProposalFileContext` 改读 `selectedFileRef.current` |
| C-9 | 删除/禁用与内置插件同名的工作目录文件会把内置插件整个卸载（无恢复路径） | `plugin_editor.py` 两处 unload 改为 `scope="local"`；测试加 scope 断言作回归守卫 |

### 3.3 状态机与流程

| # | 缺陷 | 修复 |
| --- | --- | --- |
| C-15 | 草稿 taskId 终态后永不清除：每次进页面重放通知；30 天清理后变成每次进页面 404 | 只持久化运行中/`waiting_apply` 任务；`applyTaskSnapshot` 终态清 `activeTaskId` |
| C-7 | 插件路由构建失败被当作"无路由"报成功；重载路径静默丢失全部路由 | `router_manager.py::mount_plugin_router` 按 `_router_func` 存在性区分两种 None，构建失败返回 False |
| C-17 | loadFailed 插件在两个编辑器里都无法禁用（运行时开关被后端拒绝，文件级回退不可达） | 前端分支改为 `pluginInfo && !pluginInfo.loadFailed`；`isDisabledPluginEntry` 判定前置 |
| C-21 | 包内文件删除成功但重载失败时接口报错，前端不刷新列表（与注释声明的语义矛盾） | 降级为 `logger.warning`，删除本身视为成功 |
| C-5 | 停止沙盒时容器已被外部删除 → 状态误置 `failed` 且跳过 token 轮换 | 容器已不存在按"已停止"处理，照常轮换 token |
| C-3 | 容器 create/start/show 失败时泄漏无主容器 | 失败路径就地 `_remove_container` 后再抛 |

### 3.4 输入处理与显示

| # | 缺陷 | 修复 |
| --- | --- | --- |
| C-13 | `editor.tsx` 删除文件必抛 `nextFiles is not defined`（typecheck 空跑放行） | 本地变量统一命名；根因（typecheck 空跑）一并修复 |
| C-16 | 导入文件误抄删除流程逻辑（按残留 `fileToDelete` 过滤列表 + 错误文案） | 改为追加导入文件 + 正确文案 |
| C-8 | 欢迎/退群开关迁移后，存量用户在适配器里的旧设置被静默忽略 | 适配器旧开关保留为总开关（AND 语义），频道级来自 effective config |
| C-6 | `na plugin check ./x.py` 相对路径在 chdir 后按仓库根解析 | `main()` 在 chdir 前按调用方 cwd 解析 `path` / `--report-file` |
| C-11 | `__init__.py.disabled` 入口的包插件 static 检查恒失败且入口未被检查 | `_iter_candidate_python_files` 单独补入入口文件 |
| C-12 | `if TYPE_CHECKING:` 块内 import 被当作运行时导入报 error | 该块整体跳过（`else` 分支仍检查） |
| C-19 | DiffViewer 把 `---`/`+++` 开头的内容行（如删除的 `--force`）整行隐藏 | 头部过滤精确化（`'--- '`/`'+++ '` 带空格 + `@@ -n,m +n,m @@` 正则）；组件加 memo |

### 3.5 性能与清理

| 项 | 修复 |
| --- | --- |
| 每次 `start()` 同步全量重建源码快照（GitPython + copytree 阻塞事件循环，且在早退检查之前） | 快照只在确需（重）建容器时刷新，且放入 `asyncio.to_thread` |
| CC 每个工具事件同步全量重写任务 JSON（O(N²)） | 热路径按 0.5s 节流，流结束强制落盘（与 SSE 0.8s 轮询对齐） |
| 草稿每 0.8s 全量序列化写 localStorage（可达 MB 级） | 随 `generatedCode` 死状态一并消除（该 state 无任何渲染消费，仅在 localStorage 空转） |
| 死代码 | 删除 `get_task_status`、`get_available_tools`、`staged_entry_path` 冗余字段；任务状态集合三处合一（`TERMINAL_TASK_STATUSES` 单一定义） |
| 主题规范 | plugin-file-select 硬编码 rgba 与 `palette.mode` 条件改为 token；cc-editor 工具条 `color` → `tone` |

### 3.6 typecheck 基建（108 → 0）

`tsc -b` 启用后暴露的存量错误分类与处置：

- **~82 个**：`ActionButton`/`IconActionButton` 的 `Omit<'color'>` 类型与实际透传行为不符（运行时 color 一直生效）→ 放宽组件类型对齐运行时（零视觉变化），tone 体系保留为新代码首选
- **真实潜在 bug**：`themeConfig.ts` 使用未导入的 `alpha`（该样式路径执行会 ReferenceError）→ 补导入；`MessageHistory` 不可达死分支（'at' 已在 659 行被 inline group 消化）→ 删除
- **其余**：`draft` 判空、`.at()` 目标库、forEach 闭包收窄（改 for...of）、`t()` 签名缺 options、多传 props、隐式 any、IME `isComposing` 类型等逐项修复

---

## 4. 复审确认要点（修复后二次审查）

以工作区 diff（+264/-148，29 文件）逐块复核，重点确认：

1. **`_start_unlocked` 状态机闭环**：早退条件 `source_enabled && reference_source_mounted && !file_exists`；旧 state 文件无新字段时 pydantic 默认 False，不会误触发重建；stale 分支移除容器后正确落入重建路径。
2. **`_stop_unlocked` 控制流**：except 内"仍在运行"提前 return，"已不存在"落空穿过 try/finally 走统一的 stopped + token 轮换。
3. **候选主文件守卫的副作用**：主文件缺失分支不写入 `rejected_signatures`（candidate_files 保持空），后续轮次仍可接受修复后的同签名候选。
4. **`TYPE_CHECKING` 分支语义**：`if TYPE_CHECKING:` 的 `orelse`（运行时真正执行的 else 分支）仍被检查。
5. **stream.ts 对其他 SSE 消费方的影响**：聊天/系统事件等共用 `createEventStream`；行为变化为「4xx 停止重连并回调 onError、5xx/网络保持退避重连、200 但非 event-stream 视为可重试错误」——均为期望改进。共享流管理器（`createSharedEventStreamManager`）在致命错误后需重新订阅才会重连，与既有语义一致。
6. **aiodocker API**：`Exec.start(detach=False) -> Stream`（同步 overload）实测确认，`async with` + `read_out()` 为库文档姿势。
7. **测试契约**：`test_plugin_reload_rules` 两处 fake 增加 `scope` 形参与断言，固化 C-9 的修复语义。

复审未发现新引入的缺陷。

**已知行为变化（有意，需写入变更说明）**：

- 参考源码快照改为仅在（重）建容器时刷新——长期运行的沙盒容器内 `/workspace/nekro-agent-source` 不再跟随宿主代码实时变化，重启沙盒即可刷新。
- 包内文件删除后顶层包重载失败不再报接口错误（插件保持卸载并记录 warning，加载失败可在插件列表看到）。

---

## 5. 有意保留项与技术债

**保留的设计决策**（建议写入 Release Note）：

1. **C-20 插件重复 key 语义**：从 main 的"后加载者覆盖"改为"内置 > 工作目录 > 云端，先占优先"。依赖 workdir 覆盖内置插件的部署会静默失效（启动日志有 warning）。
2. **C-10 CLI_MODE 模型白名单**：检查环境只注册 `DBPluginData`；插件聚合导入其他模型（`from nekro_agent.models import DBChatChannel`）在检查环境会失败而生产正常。现有内置插件无此用法，第三方插件可能踩到。

**技术债**（建议独立 PR，均为行为保持重构）：

- `plugin_dev/sandbox.py` 与 `workspace/`（container.py、manager.py）的 4 处重复：空闲端口分配、settings.json 生成（×3 份）、容器配置组装、应用版本读取（vs `tools/common_util.get_app_version`）
- `editor.tsx` 与 `cc-editor.tsx` 的插件启停/信息块（约 60 行）应抽共享 hook
- `host_file_gateway` 与 `plugin_editor` 的两套路径校验规则应下沉合并（符号链接/反斜杠处理已有分歧）
- 插件形态判定（collector / checker / self_check 三份实现）应泛化为单一模块
- checker 手写 DDL 与 `DBPluginData` 模型已漂移（`target_chat_key` VARCHAR(64) vs 256；SQLite 下无运行时影响，但模型加列后检查环境会缺列）
- SSE 任务推送可改增量（当前 mtime + payload 去重的全量推送）；`ClaudeCodeFlowLog` 可加 memo

**环境问题**（与代码无关，本机记录）：

- `.venv` 内有 root 属主的陈旧 nekro-agent 安装 → pyproject 变更后 `poe` 同步失败，需 `UV_NO_SYNC=1` 绕过；建议空闲时 `sudo uv sync --all-extras` 修复
- `data/` 为 root 属主（bot 运行产物）→ 测试需临时数据目录（见 §6.1）；**不要**修改 `data/` 属主（bot 可能在运行）

---

## 6. 测试流程

### 6.1 自动化门禁（提交前必跑）

```bash
# 后端
poe lint          # ruff
poe typecheck     # basedpyright

# 前端（tsc -b 现在是真实检查）
poe frontend-check          # typecheck + eslint（0 警告）
poe frontend-check-full     # 提交前：额外跑 vite build

# 后端测试（本机因 data/ 与 .venv 属主问题需要以下前缀；CI/干净环境直接 poe test）
PYTHONPATH=$(pwd) NEKRO_DATA_DIR=$(mktemp -d) UV_NO_SYNC=1 poe test
```

预期：全部通过（当前基线：ruff ✅ / basedpyright 0 错误 / tsc 0 错误 / eslint 0 警告 / pytest 203 passed）。

### 6.2 手工回归：CC 沙盒生命周期

前置：已拉取 cc-sandbox 镜像；管理面板 → 插件编辑器（CC）。

| 步骤 | 操作 | 预期 |
| --- | --- | --- |
| S1 容器复用 | 启动沙盒 → `docker ps` 记下容器名 → 提交一个生成任务 → 任务运行中刷新沙盒状态页/再次点启动 | 容器名不变；日志**没有**「参考源码挂载已过期，正在重建容器」；运行中的任务不中断 |
| S2 参考源码 | 进入容器 `docker exec <c> ls /workspace/nekro-agent-source` | 有 `run_nekro_cli.py`、`nekro_agent/`；宿主改代码后需重启沙盒才刷新（预期行为） |
| S3 外部删除后停止 | `docker rm -f <容器>` → 面板点「停止」 | 状态显示已停止（不是故障）；再次启动正常 |
| S4 启动失败回收 | （可选）用 `python -m http.server <端口>` 占住端口段内所有端口后启动 | 启动报错后 `docker ps -a | grep nekro-plugin-dev-cc` 无残留容器 |
| S5 事件循环 | 任务运行全程在聊天频道发消息 | Bot 响应无秒级卡顿（快照/落盘已移出事件循环或节流） |

### 6.3 手工回归：任务与提案流水线

| 步骤 | 操作 | 预期 |
| --- | --- | --- |
| T1 正常闭环 | 选中插件 → 输入需求 → 生成 → 审查 diff → 应用 | 流式日志推进；应用后文件内容更新、版本历史新增一条 |
| T2 坏提案免疫 | 向 `data/plugin_dev/proposals/` 写入 `proposal-bad.json`（内容 `{}`）→ 提交新任务 | 任务正常完成；后端日志出现「跳过无法解析的插件开发提案文件」 |
| T3 主文件保护 | 包插件任务，提示词要求"删除主文件 X 并把逻辑移到新文件" | 任务不产出"主文件被写入他文件内容"的提案；日志出现「候选缺少任务主文件」并进入修复轮或明确失败 |
| T4 取消与恢复 | 运行中点停止；另开任务后重启后端服务 | 取消任务终态 cancelled；重启后遗留任务被标记 failed（`recover_stale_plugin_dev_tasks`） |
| T5 并发修改防护 | 生成提案后、应用前，手动改动目标插件文件 → 点应用 | 报「提案创建后已被修改」，拒绝写入 |
| T6 应用失败回滚 | （可选，构造 smoke 失败的候选）应用 | 报「插件复核未通过」，插件文件/任务状态无变化 |

### 6.4 手工回归：cc-editor 前端状态机

| 步骤 | 操作 | 预期 |
| --- | --- | --- |
| F1 陈旧任务免锁死 | localStorage 的 `nekro-plugin-cc-editor-draft` 中把 `taskId` 改为 `plugin-dev-deadbeef` → 刷新页面 | 出现一次「流式失败回退轮询」+ 错误提示后页面**解锁**（文件可切换、可发送）；不会永久转圈 |
| F2 草稿生命周期 | 任务 applied/failed 后刷新页面（多次） | 不再重放成功/失败通知；waiting_apply 的任务刷新后恢复提案并可应用 |
| F3 刷新不丢改动 | 编辑代码不保存 → 提交任务 → 任务运行中刷新页面 → 等任务完成 | 编辑器保留未保存改动（同文件场景），不被磁盘内容覆盖 |
| F4 diff 完整性 | 让提案中包含删除 `"--force"` / 新增 `"++counter"` 这类行 | 审查界面完整显示这些行（不被当作 diff 头部过滤） |
| F5 认证过期 | token 过期后停留在任务页 | 收到明确错误提示而非静默卡死 |

### 6.5 手工回归：插件管理与编辑器（editor.tsx）

| 步骤 | 操作 | 预期 |
| --- | --- | --- |
| E1 删除文件 | 删除当前选中的插件文件 | 提示删除成功；自动选中列表下一个文件（无 `nextFiles is not defined` 类错误） |
| E2 导入文件 | 先对某文件打开删除确认再取消 → 导入 `b.py` | 列表出现且选中 `b.py`；无关文件不消失；刷新失败时文案为「加载文件列表失败」 |
| E3 坏插件可禁用 | 写一个 import 即崩溃的插件 → 重载使其进入失败列表 → 编辑器点「禁用插件」 | 文件被重命名为 `.py.disabled`，禁用成功（不再弹「禁用失败」）；重新启用同样走文件重命名 |
| E4 同名遮蔽保护 | workdir 创建与某内置插件同名的 `<name>.py`（重启后被跳过加载）→ 在编辑器删除它 | 内置插件功能不受影响（其命令/沙盒方法仍可用）；日志出现「不是本地插件，跳过卸载」 |
| E5 路由失败可见 | 给带路由的插件的 `mount_router` 工厂函数抛异常 → 重载 | 重载接口报失败（而非成功+路由 404） |
| E6 包内删除语义 | 删除被包入口 import 的子模块文件 | 接口返回成功、列表刷新；插件进入加载失败列表（预期），后端 warning 日志 |

### 6.6 手工回归：配置迁移与 CLI

| 步骤 | 操作 | 预期 |
| --- | --- | --- |
| G1 欢迎开关矩阵 | 按 2×2 组合适配器开关 × 频道 effective 开关，触发入群事件 | 仅"两者皆开"发送欢迎；任一关闭不发送（退群提醒同理） |
| G2 CLI 相对路径 | `cd /tmp && na plugin check ./my_plugin.py`（或 `python run_nekro_cli.py`） | 正确解析 `/tmp/my_plugin.py`；`--report-file report.json` 落在 `/tmp` |
| G3 禁用包检查 | 对入口为 `__init__.py.disabled` 的插件目录跑 `na plugin check <dir> --level static` | 正常执行入口检查（不再报「未找到插件入口文件」） |
| G4 TYPE_CHECKING | 插件写 `if TYPE_CHECKING: import not_installed_pkg` 后跑 static 检查 | 不报「导入的模块不存在」错误 |

### 6.7 性能观察点（可选）

- 任务运行期：`watch -n1 'stat -c %Y data/plugin_dev/tasks/<task>.json'` — 落盘频率 ≤ 2 次/秒
- 浏览器 Performance 面板：任务运行期在 prompt 输入框打字无长任务卡顿；localStorage 写入仅在编辑/关键状态变化时发生
- `docker events` 观察任务全程：无非预期的容器 create/destroy

---

## 7. 结论

分支功能设计（沙盒隔离、static-only 迭代自检、提案-确认-smoke-原子应用的写入链路、版本回滚）是合理且防御性较强的。初审暴露的问题集中在：外部库 API 误用（aiodocker）、错误路径的静默吞噬（SSE onopen、路由构建、提案解析）、状态生命周期不闭环（草稿 taskId、容器 state）、以及被空跑 typecheck 掩盖的低级错误。上述问题已全部修复并通过四道门禁 + 203 项测试；两项设计决策与一批行为保持的重构债已在 §5 明确。按 §6 完成手工回归后即可合并。
