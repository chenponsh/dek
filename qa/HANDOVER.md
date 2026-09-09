# dek-qa 钉钉只读知识库问答交接

## 目标与知识边界

- 目标：钉钉 Kbot 仅依据已经审核的 `wiki/**/*.md` 回答，并在答案中提供正式笔记路径和可追溯的官方 URL；证据不足时明确说明未找到可靠依据，不猜测。
- `source/` 只用于从正式笔记引用关系中补充官方 URL，不作为回答正文语料。
- 排除 `ingestion/rough/`、`ingestion/logs/`、`_/`、`_raw/`、审计文件、浏览器 profile、凭据和管理员会话历史。

## Git 与本地实现状态

- 仓库：`/srv/projects/dek`
- 分支：`main`
- HEAD：`a1f6cf7e6172b7094ff539016223dcc2ab57a893`
- `main...origin/main`：ahead/behind `0/0`（交接前已执行 `git fetch origin --prune`）。
- 当前没有 tracked 修改；未跟踪内容如下，尚未 `git add`、commit 或 push：
  - `qa/README.md`：独立只读问答实例的部署、安全和操作说明。
  - `qa/config/config.yaml`：安全默认 profile 模板；默认不连接钉钉且白名单为空，不能直接替代运行中的受保护配置。
  - `qa/config/SOUL.md`：问答边界、引用和证据不足时的回答规则。
  - `qa/dek_qa/__init__.py`：Python 包入口。
  - `qa/dek_qa/access.py`：用户/会话白名单校验和访问控制。
  - `qa/dek_qa/build_index.py`：仅从正式知识范围构建只读索引。
  - `qa/dek_qa/index.py`：只读索引查询与正式来源引用处理。
  - `qa/dek_qa/mcp_server.py`：只暴露 `dek_kb_search`、`dek_kb_get`。
  - `qa/dek_qa/stream_id_collector.py`：一次性 Stream 候选 ID 收集器；不授权、不调用模型、不检索或回复。
  - `qa/tests/test_dek_qa.py`：索引、引用、越权拒绝、会话隔离、收集器与日志脱敏测试。
  - `qa/tests/test_hermes_dingtalk_boundary.py`：针对实际独立 Hermes DingTalk adapter 和 gateway 鉴权层的脱敏合成输入集成测试。
  - `deploy/systemd/dek-qa.service`：独立服务模板。
  - `requirements-dek-qa.txt`：隔离环境依赖清单。
  - `requirements-dek-qa.lock.txt`：Python 3.12 传递依赖及制品哈希锁文件。
  - `qa/EVIDENCE.md`：脱敏版本、哈希、验证范围和缺失证据清单。
  - `qa/DEPLOYMENT_PLAN.md`：不自动恢复服务的部署及回滚方案。
  - `qa/HANDOVER.md`：本交接记录。

## 管理员 Hermes 与独立 dek-qa（禁止混用）

### 管理员 Hermes

- 程序：`/root/.local/bin/hermes`
- 安装目录：`/usr/local/lib/hermes-agent`
- profile/状态目录：`/root/.hermes`
- Dashboard 服务：`/etc/systemd/system/hermes-dashboard.service`
- 实际 Dashboard 启动入口：`/root/.local/bin/hermes dashboard --host 127.0.0.1 --port 9119 --no-open`
- 管理员交互 CLI 可复制启动命令：

  ```bash
  cd /root && /root/.local/bin/hermes
  ```

### 独立 dek-qa

- 服务账号：`dek-qa`
- Hermes 程序：`/var/lib/dek-qa/venv/bin/hermes`
- 独立 Hermes 副本：`/opt/dek-qa/hermes-agent`
- 问答应用：`/opt/dek-qa/app`
- 运行根目录：`/var/lib/dek-qa`
- profile：`/var/lib/dek-qa/hermes/profiles/dek-qa`
- 受保护环境文件：`/var/lib/dek-qa/secrets/environment`（`0600 dek-qa:dek-qa`）
- 受保护运行配置：`/var/lib/dek-qa/hermes/profiles/dek-qa/config.yaml`（`0600 dek-qa:dek-qa`）
- 服务：`/etc/systemd/system/dek-qa.service`
- 服务入口：`/var/lib/dek-qa/venv/bin/hermes -p dek-qa gateway run --external-supervisor`
- 不得复制或复用 `/root/.hermes` 的会话、记忆或凭据；也不得用管理员 Hermes 启动 dek-qa profile。

## 当前服务状态（2026-09-08 17:28 CST 前后）

- `hermes-dashboard.service`：`active/running`，`enabled`。
- `dek-qa.service`：已按修复要求暂停，为 `inactive/dead`、`disabled`、`MainPID=0`；不得自动恢复试用。
- `dek-source-ingest.timer`：`active`、`enabled`；下一次计划为北京时间 `2026-09-09 09:15`。
- `dek-source-ingest.service`：当前 `inactive`，由 timer 触发；未被本次问答接入修改。

## 本轮代理与内部提示修复

### 已完成并验证

- 仅在 dek-qa 的受保护环境中配置本机 HTTP/HTTPS 代理；未修改系统全局代理。
- 钉钉域名加入 dek-qa 自身的 `NO_PROXY/no_proxy`，代理绕过判断已验证为 true，保持 Stream 原网络路径。
- `compression.codex_gpt55_autoraise_notice: false`：只隐藏阈值自动提升提示，自动压缩仍启用。
- `onboarding.home_channel_prompt: false`：独立 Hermes 副本增加配置门控，不设置 home channel。
- 在与正式服务等效的 systemd 沙箱中完成无知识库、无工具的最小模型探针：认证、连接和固定短文本生成成功，无代理/超时错误。
- 重启后 `dek-qa.service` 稳定运行、无重启、Stream TLS 连接存在；脱敏日志未发现已知秘密值、启动错误或认证错误。
- 重启前确认旧测试问题没有待投递、活动处理或待恢复记录，因此不会自动重放。

### 后续在线验证状态

- 代理修复后的私聊和群聊知识问答、`dek_kb_search`/`dek_kb_get` 调用、正式路径与 URL 引用已完成单用户小范围在线核对；证据范围和限制见“2026-09-08 单用户小范围在线验收汇总”。
- home channel 和上下文压缩提示在已核对的新回复中未再出现；未将这一观察扩大为所有消息类型的长期保证。
- 本地修改的独立 Hermes 门控会在替换或升级 `/opt/dek-qa/hermes-agent` 时被覆盖，升级前必须迁移或重新应用并测试。

## 已通过测试与已知问题

- 修复前 dek-qa 离线测试基线：`24/24` 通过；本轮修复后的最新结果见末尾“审查修复”章节。
- home channel 配置门控：`5/5` 用例通过。
- 独立 Hermes `gateway/run.py` Python 编译检查通过。
- systemd 服务模板和已安装 unit 此前经 `systemd-analyze verify` 通过。
- 模型沙箱探针退出码为 0，认证和生成成功。
- 旧的私聊/群聊请求曾因主模型直连超时失败，且模型未产生工具调用；修复后的有限在线问答已成功，范围见后文验收汇总。
- 辅助标题生成曾报告外部辅助提供商不可用；不影响主模型探针，但可能继续产生脱敏 warning。
- 独立运行时使用的 SQLite 版本会关闭 WAL 并退回 DELETE journal mode；目前是安全降级警告，不是问答失败原因。

## 下一步最小验收

1. 未授权用户在线拒绝：待有不在白名单的协作用户时执行，不为测试扩大白名单。
2. 同群不同授权用户历史隔离：待存在第二名已获授权用户时执行，不以离线证据替代在线验收。
3. 群聊未 `@` 的本地过滤：待能取得明确入站证据时复验，不能仅凭没有回复判定通过。
4. 工具故障回复：待能安全、可控地制造真实在线工具故障时复验。
5. 私聊与群聊隔离目前只验证持久化会话分离和回复未泄露；未捕获或逐字段检查实际模型请求上下文。
6. 当前 `dek-qa.service` 已停止并保持 disabled，修复和部署复核完成前暂停单用户试用。

## 安全控制

- 白名单仅包含本轮批准的 1 名测试用户和 2 个测试会话；配置中保存适配器所需的两种等价用户字段，但没有扩大为额外人员。
- `ALLOW_ALL=false`；群聊 `require_mention=true`；`group_sessions_per_user=true`。
- 钉钉平台工具集为空，唯一 MCP 为 `dek_kb`，仅提供 `dek_kb_search` 和 `dek_kb_get`。
- Shell、任意文件、Git、浏览器、代码执行、摄入、cron、delegation、memory 和跨会话搜索等工具不开放。
- systemd 以 `dek-qa:dek-qa` 运行，`NoNewPrivileges=true`、`ProtectSystem=strict`、`ProtectHome=true`；`/srv/projects/dek`、`/root`、systemd 配置及运行凭据目录对模型进程不可访问。
- 凭据文件和运行配置为 `0600 dek-qa:dek-qa`；不得在日志、聊天、Git 或命令行中打印其内容。
- 摄入流程由独立的 `dek-source-ingest.timer/service` 及其安全门控制，dek-qa 无权运行摄入或修改 `source/`、`wiki/`、`ingestion/rough/`。

## 备份与回滚

- 本轮代理、profile 与提示门控修改前备份：
  `/var/lib/dek-qa/backups/minimal-online-fix-20260908_140225/`
- 白名单写入前备份：
  `/var/lib/dek-qa/backups/whitelist-20260908_133052/`
- 回滚本轮最小修复时，先停止 `dek-qa.service`，再分别恢复：
  - `environment.before` → `/var/lib/dek-qa/secrets/environment`，保持 `0600 dek-qa:dek-qa`；
  - `config.yaml.before` → `/var/lib/dek-qa/hermes/profiles/dek-qa/config.yaml`，保持 `0600 dek-qa:dek-qa`；
  - `gateway-run.py.before` → `/opt/dek-qa/hermes-agent/gateway/run.py`，保持 `0644 root:root`。
- 恢复后先执行 Python 编译、配置安全门和 systemd 验证，再由管理员决定是否启动；不得自动恢复成全员访问，也不得影响摄入 timer。

## 2026-09-08 MCP 工具暴露修复（15:49 CST）

### 根因与最小修复

- 根因是独立运行环境 `/var/lib/dek-qa/venv` 缺少可选的 `mcp` Python SDK；Hermes 因此在启动时跳过 MCP discovery，模型请求中没有知识库工具。
- MCP 服务端本身没有故障：修复前已在 `dek-qa` 账号下完成 `initialize`、`tools/list`、`dek_kb_search` 和 `dek_kb_get` 的真实 stdio 通道测试。
- 已在独立 venv 安装与当前独立 Hermes `pyproject.toml` 一致的固定版本：`mcp==2.0.0`、`httpx2==2.7.0`、`starlette==1.3.1`；未修改管理员 Hermes 环境。
- 已将上述依赖写入 `requirements-dek-qa.txt`，并增加 MCP SDK 运行时依赖回归测试。
- Hermes 的渐进式工具发现默认会把两个 MCP 工具替换为 `tool_search`、`tool_describe`、`tool_call` 三个桥接工具。独立 profile 已设置 `tools.tool_search.enabled: off`，因此模型工具定义现在严格只有：
  - `mcp__dek_kb__dek_kb_search`
  - `mcp__dek_kb__dek_kb_get`
- `platform_toolsets.dingtalk` 仍为空，所有内置工具继续禁用；MCP server 通过独立的全局 MCP 配置注入，没有开放其他工具。

### 指令与安全语义

- 三个独立 QA `SOUL.md` 已同步更新，明确要求每次知识问答执行 search → get，未获得工具证据时禁止生成结论、`wiki/...` 路径或官方 URL。
- 指令明确区分：
  - 工具成功但证据不足：回答“未在已审核知识库中找到足够依据。”
  - 工具不可用、失败或结果不可解析：回答“知识库查询失败，请联系管理员检查工具状态。”
- 修改前备份位于 `/var/lib/dek-qa/backups/mcp-tool-enforcement-20260908/`，包含仓库、部署副本和运行 profile 三份 `SOUL.md.before`。
- 未修改 `/root/.hermes/SOUL.md`、管理员 Hermes、白名单、会话隔离、vault 权限或摄入 timer。

### 已完成的离线/沙箱验证

- dek-qa 单元测试：`24/24` 通过。
- 在运行中 `dek-qa.service` 的 mount namespace、`dek-qa` 服务账号及相同 profile 下验证：
  - MCP discovery：2 个工具；
  - 模型工具定义：2 个，且仅为上述 search/get；
  - `search` 实际命中后使用返回的不透明 ID 调用 `get` 成功；
  - 读取结果路径属于 `wiki/`，引用 URL 字段来自工具返回值。
- `dek-qa.service` 重启后为 `active/running`、`disabled`，MCP watchdog/stdio 子进程正在运行。
- `dek-source-ingest.timer` 保持 `active/enabled`。

### 尚待端到端验收

- 本节离线/沙箱验证不包含机器人主动发消息，也不等同于钉钉端到端验收；后续在线结果见下一节。

## 2026-09-08 单用户小范围在线验收汇总（16:33 CST）

### 已通过

- 模型连通性与回复投递：已授权私聊、已授权测试群均成功；无代理、认证或投递错误。
- 知识库调用：私聊实际执行 `search → get`；群聊实际执行两次 `search` 后执行 `get`。
- 引用来源：两条知识答案中的 `wiki/...` 路径和官方 URL 均来自各自 `get` 返回结果，未发现回复额外构造路径或 URL。
- 正文忠实性：两条知识答案的实质结论均获各自 `get` 正文支持。
- 无依据拒答：测试问题实际调用 `search`，调用成功且返回结构正常；候选不足以支持问题时执行拒答，未调用 `get`，未生成路径或 URL。这是“检索成功但证据不足”，不是工具故障。
- 私聊与群聊上下文隔离：私聊测试标记仅存在于私聊会话；群聊会话的持久化上下文不含该标记，群聊回复未泄露标记。两个会话使用不同 session ID 和 session key，回复均成功投递。此次只核对了状态库中的持久化会话消息，没有捕获或逐字段检查实际发送给模型的完整请求上下文，因此结论限定为“持久化会话隔离及回复未泄露已验证”，不宣称模型请求上下文已独立验收。
- 既有离线测试仍为 `24/24` 通过；其中用户/会话隔离测试是离线证据，不替代未执行的多用户在线测试。

### 未执行或证据不足

- 未授权用户在线拒绝：因当前没有其他用户配合，本轮未执行；不得记为在线通过，也未扩大白名单。
- 同群不同授权用户在线隔离：当前只有一名授权用户，本轮未执行；仅保留已有离线测试证据。
- 群聊未 `@`：服务状态库中没有模型请求、工具调用或回复任务，但 journald 没有可证明该消息已由钉钉投递至本地适配器的入站记录；无法区分“钉钉未投递”和“本地在持久化前过滤”，因此端到端过滤验收证据不足。
- 工具故障在线回复：尚未通过真实钉钉请求制造安全、可控的工具故障；当前只有指令和测试层验证，不记为在线通过。

### 来源链接和日期元数据待修复

- 私聊知识条目：wiki 仅链接安徽栏目主来源笔记，但对应文章摘录实际存在，并保存国家药典委员会具体文章 URL、标题和安徽栏目列表日期。安徽摘录日期为 `2025-10-27`，同一官方文章在 CPC 来源笔记中记录为 `2025-09-30`，wiki 当前 `date: 2025-10-18` 的来源与含义未知。正文中的 CDE URL 是相关问答栏目入口，必须标注为栏目入口，不得构造详情链接或视作本条直接出处。
- 群聊知识条目：wiki 的 `source` 只是普通文本“江苏局官网”，没有对应 source wikilink；全库未找到对应 source 记录。wiki `date: 2022-11-07` 的来源与含义未知；URL 路径中的 `20221101` 只是日期片段，不能推断为发布日期，也不能据此覆盖元数据。发布日期、采集日期和更新时间目前均未知。
- 最小后续修复应先补齐可审计 source 关系和日期字段语义，再重建索引；当前未修改知识库或索引。

### 历史试用边界

- 本节记录的是停止服务前的历史单用户、固定测试会话观测，不代表当前仍在试用。
- 当前服务已停止且保持 `disabled`；用户或群白名单未扩大，摄入 timer 保持运行。

## 2026-09-08 提交前审查修复（17:28 CST 后）

- 统一 wiki/source 路径排除规则：任一路径段含“排除”即跳过。只读检查当前版本 1 运行索引为 1066 文档，未发现此类路径；没有替换运行索引。
- `official_urls` 仅从可唯一解析的 source wikilink 与对应 source 文件显式 `source_url` 产生。正文 URL 不再提升；同名 stem 构建失败；带目录错误引用不回退；无法验证时写 `source_status=unknown`。
- 版本 2 候选索引仍为 1066 文档，0 个含“排除”路径。旧索引 22 个 URL 在新标准下均不能验证，候选中 946 个来源状态为 unknown、120 个为 none；未修改正式 wiki/source 正文。详细哈希见 `EVIDENCE.md`。
- MCP 对 JSON-RPC 对象、工具参数、required/additional properties、limit 类型/范围、document ID 格式执行服务端校验。非法 JSON/信封返回标准 JSON-RPC 错误；工具 schema 错误返回 Hermes 可识别的 `result.isError=true`，避免计入服务器熔断；真实 stdio 回归确认后续合法请求继续处理。
- 搜索 token 不再跨标点连接中文，且至少要求一个有意义的多字符 token 命中；Stream 收集器完成回调改为幂等。
- systemd 候选 unit 的可写范围从 `/var/lib/dek-qa` 收紧到 `/var/lib/dek-qa/hermes`，index 与 secrets 显式只读；尚未部署，运行兼容性仍待部署阶段验证。
- 增加 `requests`、`websockets` 直接依赖和带哈希锁文件；已在全新临时 Python 3.12 venv 中完成锁定安装与核心测试。
- 沿实际 `/opt/dek-qa/hermes-agent` 路径确认：DingTalk 身份在 adapter 层可用并在 gateway dispatch 前门控，gateway 再做默认拒绝鉴权；MCP 是本地 stdio，不接收 DingTalk 身份。因此未把独立 `access.py` 机械导入 MCP。
- 随后发现运行 profile 的用户/群白名单位于 `platforms.dingtalk` 顶层，而 adapter 实际从 `platforms.dingtalk.extra` 读取；历史运行中 adapter 层群白名单是否生效因此标为未确认。仓库模板已修正并加入真实解析器回归测试，但受保护运行 profile 尚未迁移，白名单值未修改。
- 旧的完整模型请求 payload、未授权真实用户、双授权用户、未 @入站和真实工具故障证据仍缺失，没有补造。来源链接和日期元数据问题继续保留。
- 部署与回滚步骤只写入 `DEPLOYMENT_PLAN.md`；未部署、未恢复服务、未 git add/commit/push。

## 已获授权范围

- 已授权：建立独立账号/profile/运行目录与隔离依赖；配置固定测试白名单；部署但不启用服务；小范围 Stream 在线验收；仅为 dek-qa 配置本机代理；关闭两类内部提示；执行无知识库内容的模型探针并重启 dek-qa。
- 未授权：扩大用户或群范围、设置 home channel、开放工具、复制管理员凭据或历史、修改知识库/摄入逻辑、修改摄入 timer、自动提交或推送。
