# 钉钉只读知识库问答

该组件为独立 `dek-qa` Hermes 实例提供只读知识库能力。仓库记录索引构建、MCP 工具、访问控制、安全配置模板与测试；当前部署和在线验收范围见 `HANDOVER.md`。

## 安全边界

- 正式语料仅为 `wiki/**/*.md`。
- `source/` 只在构建索引时解析显式 `source_url` 或兼容字段 `url`；正式笔记必须通过可唯一解析的 wikilink 指向该 source 文件，URL 才进入 `source_urls`。来源链接客观记录信息实际取自哪里，可以是监管机构网站、公众号、培训材料或第三方整理页；不得把第三方材料描述成监管机构原文。任意正文 URL 不会自动提升为来源链接；未唯一解析的来源标为 `unknown`。
- wiki 或 source 的任一路径段只要含“排除”即跳过；`ingestion/`、`_/`、`_raw/`、`.git/`、浏览器 profile 和审计文件也不进入索引。
- 运行时读取单个 `0600` 索引，不读取 vault。MCP 仅公开 `dek_kb_search`、`dek_kb_get` 与 `dek_kb_recent` 三个只读工具；`dek_kb_get` 只接受检索返回的不透明 ID。
- 用户及群均采用非空白名单、默认拒绝。会话键为 `chat_type + chat_id + user_id`，不公开历史搜索工具。
- 系统提示必须要求回答仅使用工具证据；只有 `source_status=verified` 时才可附 `source_urls`，并可按 `source_names`、`source_types` 客观说明来源名称和类型；否则来源链接明确视为未知。无证据时回答“未在已审核知识库中找到足够依据。”

## 本地构建和测试

```bash
uv venv /tmp/dek-qa-test-venv --python 3.12
uv pip install --python /tmp/dek-qa-test-venv/bin/python --require-hashes -r requirements-dek-qa.lock.txt
DEK_QA_HERMES_SOURCE=/opt/dek-qa/hermes-agent PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/srv/projects/dek \
  /tmp/dek-qa-test-venv/bin/python -m unittest \
  qa.tests.test_dek_qa qa.tests.test_hermes_dingtalk_boundary qa.tests.test_dependency_lock -v
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/srv/projects/dek /tmp/dek-qa-test-venv/bin/python -m qa.dek_qa.build_index --vault /srv/projects/dek --output /tmp/dek-kb.candidate.json
```

锁文件不能只从六项 QA 直接依赖独立解析。先从已核验的 Hermes Agent 0.21.0 `uv.lock` 导出 core + mcp 的完整约束，再由解析器把该约束与 `requirements-dek-qa.txt` 合并，并为 Linux/Python 3.12 生成单一带哈希锁文件：

```bash
PYTHONPATH=/srv/projects/dek python3 -m qa.dek_qa.dependency_lock \
  --build-combined-lock \
  --hermes-project /opt/dek-qa/hermes-agent \
  --direct requirements-dek-qa.txt \
  --output requirements-dek-qa.lock.txt
```

生成器在私有临时目录中固定使用 `constraints.txt`、`hermes.txt` 和 `direct.txt` 三个相对名称，避免调用方绝对路径进入 uv 注释并破坏字节级复现。完整 `uv.lock` 中名称只有一个锁定版本的 registry 包作为约束，core + mcp 导出决定实际安装集合；同名多环境分叉（当前如 scipy）不会被错误平铺成互相冲突的无 marker 约束。这样新加入的 dingtalk-stream 传递依赖也不能漂离 Hermes 已审核的唯一版本。生成后必须运行 `qa.tests.test_dependency_lock`，并在全新 seeded venv 中先按哈希锁安装，再以 `--no-deps -e /opt/dek-qa/hermes-agent` 安装已核验的 Hermes 源副本，最后执行 `uv pip check`。当前计数口径为锁文件 84 个包、seed 安装的 pip 1 个、editable Hermes 1 个，共 86 个；pip 不属于锁文件。不得使用 Hermes 的 `dingtalk` extra：该 extra 还会引入 `alibabacloud-dingtalk` 及其当前 `cryptography<49` 元数据约束，而本实例只需要 `dingtalk-stream`。候选索引包含构建器版本、输入内容摘要和文档数；不得直接覆盖运行索引，部署步骤见 `DEPLOYMENT_PLAN.md`。

## 独立实例配置

服务账号的 `HERMES_HOME` 为 `/var/lib/dek-qa/hermes`，不得复制 `/root/.hermes`。模型认证与钉钉认证必须分别写入仅 `dek-qa` 可读的凭据文件（目录 `0700`、文件 `0600`）：

- 模型：为该实例单独签发最小额度 API key，变量名按选定 Hermes provider 要求配置；也可使用工作负载身份/本机模型端点，禁止复制管理员 key。
- 钉钉：`DINGTALK_CLIENT_ID`、`DINGTALK_CLIENT_SECRET`。
- 人员权限只由钉钉应用可见范围管理：`DINGTALK_ALLOWED_USERS=*`、`DINGTALK_ALLOW_ALL_USERS=true`。Hermes 不维护逐人白名单。群聊仍使用非空 `DINGTALK_ALLOWED_CHATS` 限定允许群，并保持 `require_mention=true`。

由管理员在服务器本地使用交互式秘密录入或受控 secrets manager 写入，不在聊天、Git、命令历史或 systemd unit 中填写值。上线前将独立 profile 的 `platform_toolsets.dingtalk` 设为空，只启用本 MCP 的两个工具；同时禁用 terminal、file、web、browser、skills、memory、session_search、cronjob、delegation 和 code execution。

仓库提供的 `qa/config/config.yaml` 默认保持 DingTalk `enabled: false`，因此在录入凭据并完成上线复核前不会建立 Stream 连接。DingTalk adapter 从 `platforms.dingtalk.extra` 读取 `allowed_users`、`allowed_chats` 和 `require_mention`；运行配置将 `allowed_users` 设为 `[*]`，把逐人授权交给钉钉应用可见范围，同时写入非空群白名单并保持 `group_sessions_per_user: true`。不得仅把这些键放在 `platforms.dingtalk` 顶层，因为当前 Hermes 解析器不会把用户/群白名单自动桥接到 adapter。

构建完成后将 `qa/` 的只读部署副本放在 `/opt/dek-qa/app`。systemd 运行时通过 `InaccessiblePaths=/srv/projects/dek` 完全禁止访问生产 vault；`/var/lib/dek-qa/index` 和 `/var/lib/dek-qa/secrets` 显式只读，仅 `/var/lib/dek-qa/hermes` 允许写入运行状态。unit 必须设置 `HERMES_DISABLE_LAZY_INSTALLS=1`，避免 Hermes 在启动或模型探针期间把可选后端依赖写入已锁定 venv。

`requirements-dek-qa.txt` 显式固定 Stream SDK、MCP 客户端及代码直接导入的 `requests`、`websockets`；`requirements-dek-qa.lock.txt` 固定传递依赖并记录制品哈希。依赖只安装到独立 venv，不修改管理员 Hermes 环境。为使模型直接看到且只能看到两个知识库工具，独立 profile 还必须设置 `tools.tool_search.enabled: off`；否则渐进式工具发现可能把两个 MCP 工具替换为通用桥接工具。

## 一次性 Stream ID 收集器

`qa.dek_qa.stream_id_collector` 独立于 Hermes gateway。它随机生成最长 15 分钟有效的配对码，只接受包含完整且边界准确的 `DEK-QA PAIR <配对码>` 的文本消息，以兼容群聊中的 `@机器人` 前缀；其他消息仅作协议 ACK，不保存正文且不回复。候选记录只包含适配器鉴权所需的发送者 ID、会话 ID/类型，以及核对企业归属所需的双方 corp ID。候选不代表授权，也不会自动写入白名单。

候选文件写入 `/var/lib/dek-qa/candidates/`，目录 `0700`、文件创建即 `0600`。默认收集两条不同事件（建议一条私聊、一条群聊），或 5 分钟后退出：

```bash
sudo -u dek-qa env PYTHONPATH=/opt/dek-qa/app \
  /var/lib/dek-qa/venv/bin/python -m qa.dek_qa.stream_id_collector \
  --ttl-seconds 300 --max-events 2
```

启动时终端只显示随机配对消息、候选文件路径和有效秒数，不显示钉钉凭据。运行期间不要关闭终端；依次向机器人私聊发送该完整配对消息，并在测试群中 @机器人后发送相同消息。完成后先人工核对候选 ID 和企业归属，再另行批准写入正式白名单。

收集器不使用 SDK 自带的无限重连循环，而是只建立一次 Stream 会话。超时、事件收集完成、`SIGTERM` 或 `Ctrl+C` 都会设置停止事件，关闭 WebSocket，取消并等待 keepalive 与消息任务后退出，且不会重连。根日志、SDK 日志及异常文本会在输出前统一遮蔽 ticket、Token、Secret、Authorization 和 Webhook 值。
