# dek-qa 修复部署与回滚方案

状态：已于 2026-09-09 按用户明确授权执行；`dek-qa.service` 已启动并 enable，`dek-source-ingest.timer` 未停止或修改。实际结果见 `EVIDENCE.md` 与 `HANDOVER.md`。

## 部署前门槛

1. 独立复审通过，仓库测试、实际 Hermes/DingTalk 边界测试、锁文件全新环境安装和 systemd verify 全部通过。
2. `dek-qa.service` 必须保持 `inactive/dead`、`disabled`、`MainPID=0`；摄入 timer 必须保持 `active/enabled`。
3. 记录仓库 HEAD、候选文件 SHA-256、当前运行 app/unit/index SHA-256；备份不得进入 Git。
4. 确认没有修改 wiki/source 正文、白名单、profile 凭据或管理员 Hermes。

## 分阶段部署

### A. 独立依赖环境

1. 用 Python 3.12 新建 staging venv。
2. 先核对 Hermes `pyproject.toml`、`uv.lock` 与 `EVIDENCE.md` 的摘要；锁文件必须由 Hermes 0.21.0 core + mcp 的锁定导出和 QA 直接依赖共同解析生成，不得从 QA 六项依赖单独解析，也不得人工只覆盖已暴露的冲突包。
3. 使用 `requirements-dek-qa.lock.txt` 和 `--require-hashes` 安装，再以 `--no-deps` 安装已核验的 Hermes editable 源副本；运行锁兼容回归、`uv pip check`、完整 QA 测试和最小模型连接测试。任一失败即停止，不修改生产 venv。
4. Hermes Agent `0.21.0` 当前来自 `/opt/dek-qa/hermes-agent` editable 副本且无 Git 元数据。正式部署前必须以 `EVIDENCE.md` 中的 manifest 和关键文件哈希核对该副本；若无法一致，不继续。
5. 不修改管理员 Hermes 环境。

### 2026-09-09 失败批次边界

固定提交 `55da7ef57f3c56752e07943432724cf0ad046611` 的首次部署在依赖检查阶段失败：旧 QA 锁把 Hermes 已锁定的多个传递依赖升级，`uv pip check` 首先报告 certifi、cryptography、pydantic 三项不兼容（pydantic-core 同步漂移）。该批次已从时间戳备份完整恢复 app、venv、profile、unit 和版本 1 索引；profile 迁移、候选 unit 安装、索引替换和服务启动均未执行。旧运行版本不是新版本已部署。后续部署必须使用经独立复审的新提交和新锁摘要，并重新获得部署授权。

### B. 应用与配置模板

1. 将当前 `/opt/dek-qa/app`、已安装 unit 和运行索引分别备份到新建的 root-only/dek-qa-only 时间戳目录，禁止覆盖旧备份。
2. 把仓库 `qa/` 同步到新的只读 staging app 目录；不把 `EVIDENCE.md` 中引用的运行数据或 `/tmp` 候选索引复制进 app。
3. 核对 staging app 中 `index.py`、`mcp_server.py`、`SOUL.md` 和测试文件哈希。
4. 运行 profile 含秘密，不用仓库模板覆盖；只对明确允许的非秘密键做结构化差异检查。人员授权由钉钉应用可见范围负责：将 DingTalk 的 `allowed_users` 设为 `[*]`，并设置 `DINGTALK_ALLOWED_USERS=*`、`DINGTALK_ALLOW_ALL_USERS=true`。原有 `allowed_chats` 必须逐值保持不变，`require_mention=true`；这些 adapter 门控均位于 `platforms.dingtalk.extra`。`group_sessions_per_user`、`platform_toolsets.dingtalk` 和 `tools.tool_search.enabled` 不变。
5. 用实际 `load_gateway_config()` 解析迁移后的受保护配置，断言 adapter 收到用户通配符、非空群白名单及 `require_mention=true`，gateway 对 DingTalk 使用显式 allow-all；失败则不继续部署。

### C. 索引候选

1. 从目标仓库快照重新构建到新的候选路径，不直接写 `/var/lib/dek-qa/index/dek-kb.json`。
2. 要求：索引版本 4、文档数 1066、含“排除”路径数 0、metadata 文档数一致、输入摘要和产物摘要与当次构建记录一致。
3. 抽样执行 search → get；确认 get 返回 `source_status`、`source_urls`、`source_names` 和 `source_types`，且未确认来源不会出现在 `source_urls`。
4. 候选索引只用于当轮比较，不作为无需复建即可部署的长期制品。

### D. 切换与验收

1. 保持服务停止，先原子切换 app 和版本 2 索引；版本 2 代码与版本 1 索引不混用。uv venv 的 console-script shebang 含创建时绝对路径，因此不得把 `venv.new-*` 重命名为 `venv`；必须直接在稳定最终路径构建，或使用从一开始就固定的版本目录加稳定 symlink。
2. 设置 app 为 root 所有且不可由 `dek-qa` 写，索引目录最小权限、索引文件 `0600 dek-qa:dek-qa`。
3. 安装收紧后的 unit：仅 `/var/lib/dek-qa/hermes` 可写，index 和 secrets 显式只读，并设置 `HERMES_DISABLE_LAZY_INSTALLS=1` 防止运行时依赖漂移。执行 daemon-reload 和 `systemd-analyze verify`，验证通过后按授权启动并 enable。
4. 在等效 systemd 沙箱中运行只读 MCP stdio initialize/list/search/get 和非法输入后续请求测试，确认 Hermes 必要状态写入仍只发生在 `/var/lib/dek-qa/hermes`。
5. 复核 `dek-qa.service` 为 active/enabled 且无重启，摄入 timer 仍为 active/enabled。

## 恢复服务验收

1. 按用户明确授权启动并 enable。
2. 验证模型只看到三个只读 MCP 工具，执行 search → get，未知来源不输出来源链接，第三方材料不被描述成监管机构原文。
3. 验证无模型工具扩权、无白名单变化、无跨 profile 状态写入。
4. 在线证据仍按 `EVIDENCE.md` 的缺失项逐项标记；没有入站证据时不得以“未回复”判通过。

## 回滚

1. 停止服务并保持 disabled。
2. 原子恢复同一批次备份的 app、venv、unit 和版本 1 索引，不能混搭版本。
3. daemon-reload 后运行语法、权限和离线 MCP 验证；不自动启动。
4. 摄入 timer 全程不操作；回滚后再次只读确认其仍为 active/enabled。
