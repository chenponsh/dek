# 钉钉只读知识库问答

该组件为独立 `dek-qa` Hermes 实例提供只读知识库能力。仓库记录索引构建、MCP 工具、访问控制、安全配置模板与测试；当前部署和在线验收范围见 `HANDOVER.md`。

## 安全边界

- 正式语料仅为 `wiki/**/*.md`。
- `source/` 只在构建索引时解析显式 `source_url`；正式笔记必须通过可唯一解析的 wikilink 指向该 source 文件，URL 才进入 `official_urls`。任意正文 URL 不会被提升为官方来源；未唯一解析的来源标为 `unknown`。
- wiki 或 source 的任一路径段只要含“排除”即跳过；`ingestion/`、`_/`、`_raw/`、`.git/`、浏览器 profile 和审计文件也不进入索引。
- 运行时读取单个 `0600` 索引，不读取 vault。MCP 仅公开 `dek_kb_search` 与 `dek_kb_get`，后者只接受检索返回的不透明 ID。
- 用户及群均采用非空白名单、默认拒绝。会话键为 `chat_type + chat_id + user_id`，不公开历史搜索工具。
- 系统提示必须要求回答仅使用工具证据；只有 `source_status=verified` 时才可附 `official_urls`，否则来源链接明确视为未知。无证据时回答“未在已审核知识库中找到足够依据。”

## 本地构建和测试

```bash
uv venv /tmp/dek-qa-test-venv --python 3.12
uv pip install --python /tmp/dek-qa-test-venv/bin/python --require-hashes -r requirements-dek-qa.lock.txt
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/srv/projects/dek /tmp/dek-qa-test-venv/bin/python -m unittest qa.tests.test_dek_qa -v
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/srv/projects/dek /tmp/dek-qa-test-venv/bin/python -m qa.dek_qa.build_index --vault /srv/projects/dek --output /tmp/dek-kb.candidate.json
```

锁文件由 `uv 0.12.8` 使用 Python 3.12 目标生成：`uv pip compile requirements-dek-qa.txt --python-version 3.12 --generate-hashes --output-file requirements-dek-qa.lock.txt`。候选索引包含构建器版本、输入内容摘要和文档数；不得直接覆盖运行索引，部署步骤见 `DEPLOYMENT_PLAN.md`。

## 独立实例配置

服务账号的 `HERMES_HOME` 为 `/var/lib/dek-qa/hermes`，不得复制 `/root/.hermes`。模型认证与钉钉认证必须分别写入仅 `dek-qa` 可读的凭据文件（目录 `0700`、文件 `0600`）：

- 模型：为该实例单独签发最小额度 API key，变量名按选定 Hermes provider 要求配置；也可使用工作负载身份/本机模型端点，禁止复制管理员 key。
- 钉钉：`DINGTALK_CLIENT_ID`、`DINGTALK_CLIENT_SECRET`。
- 白名单：`DINGTALK_ALLOWED_USERS` 和 `DINGTALK_ALLOWED_CHATS`，均不得为空或使用 `*`；`DINGTALK_ALLOW_ALL_USERS=false`。

由管理员在服务器本地使用交互式秘密录入或受控 secrets manager 写入，不在聊天、Git、命令历史或 systemd unit 中填写值。上线前将独立 profile 的 `platform_toolsets.dingtalk` 设为空，只启用本 MCP 的两个工具；同时禁用 terminal、file、web、browser、skills、memory、session_search、cronjob、delegation 和 code execution。

仓库提供的 `qa/config/config.yaml` 默认保持 DingTalk `enabled: false`，用户和群白名单为空，因此在录入凭据并完成上线复核前不会建立 Stream 连接。DingTalk adapter 从 `platforms.dingtalk.extra` 读取 `allowed_users`、`allowed_chats` 和 `require_mention`；配置时必须在该层级同时写入非空白名单，且 `group_sessions_per_user: true`。不得仅把这些键放在 `platforms.dingtalk` 顶层，因为当前 Hermes 解析器不会把用户/群白名单自动桥接到 adapter。

构建完成后将 `qa/` 的只读部署副本放在 `/opt/dek-qa/app`。systemd 运行时通过 `InaccessiblePaths=/srv/projects/dek` 完全禁止访问生产 vault；`/var/lib/dek-qa/index` 和 `/var/lib/dek-qa/secrets` 显式只读，仅 `/var/lib/dek-qa/hermes` 允许写入运行状态。

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
