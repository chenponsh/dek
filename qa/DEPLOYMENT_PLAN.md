# dek-qa 修复部署与回滚方案

状态：仅方案，尚未执行。任何步骤都不得自动启动或 enable `dek-qa.service`，不得停止或修改 `dek-source-ingest.timer`。

## 部署前门槛

1. 独立复审通过，仓库测试、实际 Hermes/DingTalk 边界测试、锁文件全新环境安装和 systemd verify 全部通过。
2. `dek-qa.service` 必须保持 `inactive/dead`、`disabled`、`MainPID=0`；摄入 timer 必须保持 `active/enabled`。
3. 记录仓库 HEAD、候选文件 SHA-256、当前运行 app/unit/index SHA-256；备份不得进入 Git。
4. 确认没有修改 wiki/source 正文、白名单、profile 凭据或管理员 Hermes。

## 分阶段部署

### A. 独立依赖环境

1. 用 Python 3.12 新建 staging venv。
2. 使用 `requirements-dek-qa.lock.txt` 和 `--require-hashes` 安装，运行核心测试。
3. Hermes Agent `0.21.0` 当前来自 `/opt/dek-qa/hermes-agent` editable 副本且无 Git 元数据。正式部署前必须以 `EVIDENCE.md` 中的 manifest 和关键文件哈希核对该副本；若无法一致，不继续。
4. 不修改管理员 Hermes 环境。

### B. 应用与配置模板

1. 将当前 `/opt/dek-qa/app`、已安装 unit 和运行索引分别备份到新建的 root-only/dek-qa-only 时间戳目录，禁止覆盖旧备份。
2. 把仓库 `qa/` 同步到新的只读 staging app 目录；不把 `EVIDENCE.md` 中引用的运行数据或 `/tmp` 候选索引复制进 app。
3. 核对 staging app 中 `index.py`、`mcp_server.py`、`SOUL.md` 和测试文件哈希。
4. 运行 profile 含秘密，不用仓库模板覆盖；只对明确允许的非秘密键做结构化差异检查。保持白名单值完全不变，但在部署时将 DingTalk 的 `allowed_users`、`allowed_chats`、`require_mention` 放入 adapter 实际读取的 `platforms.dingtalk.extra`。迁移前后以脱敏摘要核对元素数量和逐值哈希集合一致，不输出值。`group_sessions_per_user`、`platform_toolsets.dingtalk` 和 `tools.tool_search.enabled` 不变。
5. 用实际 `load_gateway_config()` 解析迁移后的受保护配置，断言 adapter 收到非空用户/群白名单及 `require_mention=true`；失败则不继续部署。

### C. 索引候选

1. 从目标仓库快照重新构建到新的候选路径，不直接写 `/var/lib/dek-qa/index/dek-kb.json`。
2. 要求：索引版本 2、文档数 1066、含“排除”路径数 0、metadata 文档数一致、输入摘要和产物摘要与当次构建记录一致。
3. 抽样执行 search → get；确认 get 返回 `source_status`，且当前未验证来源不会出现在 `official_urls`。
4. 当前 `/tmp/dek-qa-proposed-index-v2.json` 只用于本轮比较，不作为无需复建即可部署的长期制品。

### D. 原子切换（需后续明确授权）

1. 保持服务停止，先切换 app/venv，再以同一部署批次原子替换版本 2 索引；版本 2 代码与版本 1 索引不混用。
2. 设置 app 为 root 所有且不可由 `dek-qa` 写，索引目录最小权限、索引文件 `0600 dek-qa:dek-qa`。
3. 安装收紧后的 unit：仅 `/var/lib/dek-qa/hermes` 可写，index 和 secrets 显式只读。执行 daemon-reload 和 `systemd-analyze verify`，但仍不启动、不 enable。
4. 在等效 systemd 沙箱中运行只读 MCP stdio initialize/list/search/get 和非法输入后续请求测试，确认 Hermes 必要状态写入仍只发生在 `/var/lib/dek-qa/hermes`。
5. 复核 `dek-qa.service` 仍为 inactive/disabled，摄入 timer 仍为 active/enabled，等待人工批准恢复小范围试用。

## 恢复服务前验收（另行授权）

1. 人工批准后才允许一次性启动（不 enable）。
2. 验证模型只看到两个 MCP 工具，执行 search → get，未知来源不输出官方 URL。
3. 验证无模型工具扩权、无白名单变化、无跨 profile 状态写入。
4. 在线证据仍按 `EVIDENCE.md` 的缺失项逐项标记；没有入站证据时不得以“未回复”判通过。

## 回滚

1. 停止服务并保持 disabled。
2. 原子恢复同一批次备份的 app、venv、unit 和版本 1 索引，不能混搭版本。
3. daemon-reload 后运行语法、权限和离线 MCP 验证；不自动启动。
4. 摄入 timer 全程不操作；回滚后再次只读确认其仍为 active/enabled。
