> [!warning] Superseded architecture evidence
> 仅保留历史证据；这不是替代架构的在线验证。

# QA 修复证据清单（脱敏，已废止）

历史基线记录时间：2026-09-08T17:28:03+0800；当前修复与部署证据更新于2026-09-09。

本文件只记录可复核的版本、内容摘要、计数和测试范围，不包含凭据、白名单值、用户/群/会话 ID、消息正文、日志正文、运行配置正文或投递记录。

## 历史基线与版本（2026-09-08，已被后续 Git 状态取代）

- 仓库 HEAD：`a1f6cf7e6172b7094ff539016223dcc2ab57a893`；本轮文件仍未暂存、未提交、未推送。
- 独立运行环境：Python `3.12.3`；uv `0.12.8`；Hermes Agent 包版本 `0.21.0`。
- `/opt/dek-qa/hermes-agent` 是 editable 本地安装，不含 Git 元数据，因此不存在可验证的部署 commit。其 6061 个 Python/配置/锁文件的脱敏 manifest SHA-256 为 `6d4583076a13a5ed67e6ffe232ea69cb07f564f26055ad6e66dd1d487561e353`；这只能标识当前副本，不能替代来源 commit。
- Hermes `pyproject.toml` SHA-256：`c70c8b52f6cc08a4e65f0fc1713c26814fd4f19811bc7e01de645009b2a76600`。
- Hermes `uv.lock` SHA-256：`383cd8f98ec23dc3fe4cf63759ec73be5a869cc953f068b4e79ec4e8ed00287d`。
- 实际 DingTalk adapter SHA-256：`fbcad324849c05929ab3873709e5e72b354627bd6328c36c475c6e44d4a0dc0b`。
- 实际 gateway 鉴权 mixin SHA-256：`20e3599bc4ea88b651cbdcee70c536de201419dc8097edea286078da4e201c05`。
- 实际 MCP 客户端实现 SHA-256：`8b0bd48bd96df60a2c92bb9d5b6f0fd5f794e9dfb5f39e012ef25a749fd395b5`。

## 当前仓库与部署状态（2026-09-09）

- 当前仓库 HEAD 与 `origin/main` 均为 `55da7ef57f3c56752e07943432724cf0ad046611`，ahead/behind `0/0`。
- 当前共有11个未提交候选文件：`deploy/systemd/dek-qa.service`、`qa/dek_qa/dependency_lock.py`、`qa/dek_qa/mcp_server.py`、`qa/tests/test_dependency_lock.py`、`qa/tests/test_dek_qa.py`、`requirements-dek-qa.txt`、`requirements-dek-qa.lock.txt`、`qa/README.md`、`qa/DEPLOYMENT_PLAN.md`、`qa/EVIDENCE.md`、`qa/HANDOVER.md`；未暂存、未提交、未推送。
- 已备份部署前 app、venv、profile、Hermes 源、unit 和版本1索引到 root-only 目录 `/var/lib/dek-qa/backups/deploy-20260909_141551`。版本1索引备份 SHA-256 为 `189f3ec4fae11d82136ad57fcd9faf6c8a990469e01cd1eb215936af6fe5b41a`。
- 生产已切换到版本2索引，1066 文档，SHA-256 为 `49c02890a7e4ff365a5633d08dce6d74cda5021f8a3c9e458175bf931ddd0f4d`；unit SHA-256 为 `bd65ea67d1803bbe05d5af151380a8673930423550289b7c39bbb87fb63bf01e`。
- 生产 profile 只把原有两组白名单和 `require_mention` 从 DingTalk 顶层迁入 `extra`；迁移前后各值计数与逐值摘要一致，其他配置相等。真实 `PlatformConfig`/adapter 解析得到用户2、群2、`require_mention=true`，凭据存在。
- 首次把候选 venv 目录重命名到最终路径时发现 uv console-script shebang 保留创建路径，`hermes` 无法执行；服务尚未启动，因此没有在线影响。失效候选保留为 `venv.relocated-broken-20260909_141551`，生产 venv 已直接在稳定最终路径重建，入口 shebang 与路径一致。
- Hermes MCP 客户端首次在线启动时发送标准 `_meta`，旧服务端把它误判为非法参数并停放 MCP。修复后服务端只额外接受规范的可选 `_meta`/`cursor`，仍拒绝未知字段；52/52 回归测试和真实 MCP SDK initialize/list/search/get 通过，`hermes mcp test dek_kb` 显示连接成功并发现2个工具。
- 生产 Hermes 模型端到端调用 `dek_kb_search`→`dek_kb_get`，退出码0，返回内容同时包含直接索引预期的不透明 ID 和内部路径。
- 首次生产模型探针触发 Hermes 默认 lazy dependency 机制向 venv 安装6个不在组合锁中的包，安装包数从86漂移到92。已保留漂移环境并在最终路径重新按锁构建；unit 现固定 `HERMES_DISABLE_LAZY_INSTALLS=1`。服务启动30秒及最终 MCP/模型探针后仍为86包，`uv pip check` 无冲突。
- `dek-qa.service` 当前 `active/running`、`enabled`、`NRestarts=0`；DingTalk adapter 进程保持一条外部 TLS 长连接。摄入 timer 仍为 `active/enabled`。

## 依赖可复现性

- 直接依赖已显式包含：`dingtalk-stream==0.24.3`、`mcp==2.0.0`、`httpx2==2.12.0`、`starlette==1.3.1`、`requests==2.33.0`、`websockets==15.0.1`。
- `requirements-dek-qa.txt` SHA-256：`cf9cd61a31185476e31c94e94cb5a230d92da90e535a27c17b6b992a612d7447`。
- 旧锁文件仅从六项 QA 直接依赖解析，未把 Hermes `pyproject.toml`/`uv.lock` 作为基础约束。它与 Hermes 锁重叠的 41 个包中有 20 个版本漂移，不仅是部署时最先暴露的 certifi、cryptography、pydantic、pydantic-core；还包括 aiohappyeyeballs、annotated-types、anyio、attrs、cffi、charset-normalizer、click、idna、opentelemetry-api、propcache、rpds-py、sse-starlette、typing-extensions、typing-inspection、uvicorn、yarl。
- 新方案从 Hermes Agent 0.21.0 `uv.lock` 锁定导出 core + mcp 完整依赖，再与 QA 直接依赖共同解析；不使用会额外引入 `alibabacloud-dingtalk` 的 Hermes dingtalk extra。pydantic `2.13.4` 与 pydantic-core `2.46.4` 由同一次解析产生，而不是事后手工覆盖。
- 新的 Linux/Python 3.12 单一带哈希锁文件包含 84 个包，SHA-256：`a86d658421c8f231fd93e91eb791a1ea9a1ee5a87b95c0b723060c0c6e912d14`。两次在不同输出目录独立生成得到相同 SHA-256，`cmp` 返回 0；新增路径独立性回归后共有 6 项锁兼容测试。
- 在全新 `/tmp` Python 3.12 seeded venv 中从零按哈希安装 84 个包，再以 `--no-deps` 安装 Hermes Agent 0.21.0 editable 副本；`uv pip check` 检查 86 个包无冲突。86 的口径是锁文件 84 个包、seed 安装的 pip、editable Hermes。
- 最终生产环境运行完整 QA/Hermes 边界/锁兼容 unittest：52/52 通过；其中真实 stdio MCP 覆盖 initialize、tools/list、search/get、标准 `_meta`、可选 cursor、连续非法请求后合法请求。DingTalk adapter 导入及仓库模板经真实 `PlatformConfig.from_dict()` 解析通过。
- 使用独立服务账号、生产专用 profile/OAuth 状态和 EnvironmentFile 中的代理设置执行最小模型生成探针，退出码 0，并精确返回 `DEK_QA_MODEL_PROBE_OK`。未加载 EnvironmentFile 的首次尝试在 90 秒外层超时，因此生产 unit 必须继续加载该文件。

## 索引只读审计与候选构建

- 部署前版本1索引已备份；当前生产索引为版本 `2`，1066 文档，SHA-256 `49c02890a7e4ff365a5633d08dce6d74cda5021f8a3c9e458175bf931ddd0f4d`。
- 对当前运行索引逐路径检查：含“排除”的文档为 `0`。
- 候选索引输入 SHA-256：`d6922f5debcb00c58179ada75c01ebacd9ae545528255c57e82f626b2fc51770`；构建器版本 `2`；构建器 SHA-256 `4c8616ce714f768bf7ae37119f017edd6039ea0d01f41a1d5220a6e92415c6af`，与候选 metadata 一致；含“排除”的文档为 `0`。
- 旧索引有 22 个文档含 `official_urls`。按“只有唯一可解析的显式 `source/...` wikilink + source 文件显式 `source_url` 才可验证”的新规则，候选索引中 0 个文档获 `verified` URL；946 个标为 `source_status=unknown`，120 个标为 `none`。未修改 wiki/source 正文，也未将旧正文 URL 继续冒充为已验证官方来源。
- 同名 source stem（包括没有 source_url 的文件）会使构建失败；带目录的错误引用不会退回同名 stem。

## 实际 Hermes/DingTalk 边界

- 身份在 DingTalk adapter 接收层已经可用：适配器从入站消息取得发送者 ID 和 staff ID，在调用 gateway handler 前执行用户白名单、群白名单和 mention 条件。
- 进入 gateway 后，`GatewayAuthorizationMixin` 对 `SessionSource.user_id` 再执行默认拒绝鉴权；会话分组逻辑在后续层使用用户和会话身份。
- 针对 `/opt/dek-qa/hermes-agent` 实际代码运行 4 项合成输入集成测试：未授权用户在 dispatch 前拒绝、未授权群在 dispatch 前拒绝、gateway 默认拒绝复核、仓库模板白名单键经真实解析器进入 adapter extra；4/4 通过。测试使用虚构标识，不读取或输出真实白名单。
- 运行 profile 的 `dek_kb` MCP 配置经脱敏结构检查为本地 `stdio`，命令是独立 venv Python，未配置远程 URL。MCP 服务本身不接收 DingTalk 身份；身份/会话门控发生在进入模型与 MCP 调用之前。因此没有为了形式审查而把 `access.py` 机械导入 MCP。
- 历史运行 profile 的白名单键位于 DingTalk 顶层，真实解析时不会自动进入 adapter extra。当前生产 profile 已将原值无损迁入 `extra`；真实 adapter 解析得到用户2、群2、`require_mention=true`。
- 在线 Hermes 进程维持 DingTalk Stream TLS 长连接；本次没有伪造外部入站事件，因此未授权用户、双授权用户隔离和未 @消息仍保留为待人工在线抽查项。

## 本轮验证结果

- 最终生产环境完整 unittest：52/52 通过（其中实际 Hermes/DingTalk 边界测试 4/4、依赖锁测试6/6）。
- MCP 连续流测试依次注入 parse error、非法 JSON-RPC 对象、非法 call 信封和工具 schema 错误；最后的合法 `tools/list` 仍由同一 stdio 进程处理。工具 schema 错误使用 `result.isError=true`，协议/信封错误使用标准 JSON-RPC error。
- `systemd-analyze verify deploy/systemd/dek-qa.service`：通过。
- `uv pip check --python /var/lib/dek-qa/venv/bin/python`：86 个已安装包兼容；与84个锁定包 + pip + editable Hermes 的集合完全一致，无额外或缺失包。
- Python 编译检查：通过。
- `pip-audit -r requirements-dek-qa.lock.txt --no-deps --disable-pip`：`No known vulnerabilities found`。
- 脱敏候选扫描：私钥、AWS/GitHub token 和危险 `eval` 为0命中；`shell=True` 的唯一文本命中是本证据行自身，代码中为0；唯一凭据赋值模式命中是明确标记 `FICTIONAL` 的测试夹具。

## 服务状态

- `dek-qa.service`：`active/running`、`enabled`、`NRestarts=0`。
- `dek-source-ingest.timer`：`active`、`enabled`；本轮未停止或修改。
- 已部署 unit 把可写范围收紧为 `/var/lib/dek-qa/hermes`，将 index 和 secrets 标为只读，并禁用 lazy installs；静态 verify 与实际运行均通过。

## 2026-09-09 部署失败与回滚

- 首次部署在锁定依赖安装后的 `uv pip check` 停止；没有进入 profile 迁移、候选 unit 安装、运行索引替换或短时启动。
- 失败批次已从 `/var/lib/dek-qa/backups/deploy-55da7ef-20260909_090050` 完整恢复 app、venv、受保护 profile、systemd unit 和版本 1 索引；备份目录为 0700、文件为 0600，哈希清单校验通过。
- 回滚后生产 venv 的 100 个包依赖检查兼容；运行索引仍为版本 1、1066 文档、SHA-256 `189f3ec4fae11d82136ad57fcd9faf6c8a990469e01cd1eb215936af6fe5b41a`。这明确是旧运行版本，不是提交 `55da7ef57f3c56752e07943432724cf0ad046611` 已部署。
- 本次依赖修复阶段未修改生产 venv、运行索引、受保护 profile、白名单或 systemd unit，也未启动服务。

## 明确缺失的证据

以下历史证据没有捕获，本轮不补造：

- 未捕获历史模型请求的完整 payload，不能逐字段证明模型上下文隔离。
- 未授权真实用户在线拒绝未执行。
- 同群两名已授权用户的在线历史隔离未执行。
- 群聊未 @消息缺少可证明钉钉已投递到本地 adapter 的入站证据。
- 真实在线 MCP 故障回复未执行。
- 旧 22 个 URL 的直接来源关系和相关日期语义未获验证；新候选因此全部降为未知，不以旧回答或 URL 路径片段反推。
- `/opt/dek-qa/hermes-agent` 缺 Git 元数据；只能提供当前内容 manifest，不能声称可从某个 commit 精确重建。

## 2026-09-09 钉钉可见范围权限切换

- 用户明确选择钉钉应用可见范围作为唯一的逐人授权来源。生产 profile 的 `platforms.dingtalk.extra.allowed_users` 已由原 2 个用户改为 `[*]`，EnvironmentFile 已设置 `DINGTALK_ALLOWED_USERS=*`、`DINGTALK_ALLOW_ALL_USERS=true`；Hermes 不再维护逐人名单。
- 原 2 个 `allowed_chats` 逐值保持不变，`require_mention=true`、`group_sessions_per_user=true` 保持不变。`platform_toolsets.dingtalk=[]` 且 `tools.tool_search.enabled=off`，没有开放钉钉内置工具或通用工具桥。
- 修改前的生产 profile 和 EnvironmentFile 已备份到 `/var/lib/dek-qa/backups/dingtalk-visibility-20260909_164020`。结构化写入脚本确认除目标用户授权键外 profile 其他内容相等；未输出用户、群、模型或钉钉凭据。
- 使用实际 Hermes `PlatformConfig`、DingTalk adapter 与 `GatewayAuthorizationMixin` 解析生产配置：adapter 用户集合为通配符，任意合成用户通过 gateway 用户授权，群列表仍为 2 项且必须 `@`。完整回归最终为 52/52 通过，`systemd-analyze verify` 通过。
- `dek-qa.service` 于 16:41:47 CST 重新启动后为 `active/running`、`enabled`、`NRestarts=0`，PID 172742 与钉钉端点保持 TLS 连接。旧进程关闭时 DingTalk SDK 断连超过 5 秒，systemd 记录一次旧实例 `exit-code`；新实例没有自动重启且运行正常。
- 用户于切换后使用一个原先不在 Hermes 两人名单、但位于钉钉应用可见范围内的真实账号完成私聊验收，并确认机器人正常响应。该项属于用户人工在线观测，证明钉钉可见范围、Stream 入站、Hermes 用户通配和回复投递的端到端链路通过。

## 2026-09-10 来源链接中性化候选（未部署）

- 索引版本和构建器版本均升级为 `4`；输出字段由 `official_urls` 改为中性的 `source_urls`，并增加 `source_names`、`source_types`。`source_status=verified` 仅表示链接已确认对应实际材料出处，不表示该出处一定是监管机构网站。
- 读取器兼容版本 2、3 运行索引，并在内存中把旧 `official_urls` 转换为 `source_urls`；版本 4 工具输出不再包含 `official_urls`。
- 候选路径：`/tmp/dek-kb-source-urls-v4.candidate.json`；权限 `0600`；文档数 `1066`；`verified=538`、`unknown=408`、`none=120`；含来源链接文档 `538`，不含 `official_urls` 字段。
- 候选 SHA-256：`6b9c0473c28184dc7b83c6ba9accd44799175cdc941a01d54c6e0b9952d4dc3e`；输入 SHA-256：`f275db8ade3206cff6b2900337440042075c8ff902c8a7627c9836edbf289a06`；构建器 SHA-256：`b3ce85aa7f550b88edf160d0fd91133a7f6d498c328bda1b279dbb07985e9966`。
- 完整 QA/Hermes 边界/依赖锁测试 `57/57` 通过，`git diff --check` 通过。该结果为离线候选验证；生产 app、SOUL、索引和服务均未切换，未执行钉钉端到端验证。

## 2026-09-10 来源链接中性化生产部署

- 用户明确批准后，于 `2026-09-10 16:18 +0800` 切换生产 app、Kbot `SOUL.md` 和版本 4 索引；未覆盖运行 profile 配置或凭据，未修改 systemd unit、白名单、工具权限和摄入 timer。
- 部署备份：`/var/backups/dek-qa/source-urls-v4-deploy-20260910_161742_+0800`，目录权限 `0700 root:root`；包含切换前 app、索引、SOUL 和 unit 及校验清单。
- 生产索引为版本 `4`、文档数 `1066`，SHA-256 为 `6b9c0473c28184dc7b83c6ba9accd44799175cdc941a01d54c6e0b9952d4dc3e`，与当轮候选完全一致；文件权限 `0600 dek-qa:dek-qa`。
- 在生产 app 路径运行完整测试 `57/57` 通过；以 `dek-qa` 用户加载生产索引并执行 search → get 通过，返回 `source_status`、`source_urls`、`source_names`、`source_types`，索引不含 `official_urls`。
- `dek-qa.service` 重启后为 `active/running`，`MainPID=220033`、`NRestarts=0`；进程建立了到远端 `443` 的连接。`dek-source-ingest.timer` 保持 `active/enabled`。
- 停止旧进程时 DingTalk disconnect 在 5 秒内未完成，旧进程以状态 1 退出；新进程随后正常启动且未自动重启。当前日志仍有既有 SQLite 版本安全降级告警（Hermes 自动使用 `journal_mode=DELETE`）及未启用工具的可用性告警。
- 未发送钉钉测试消息，因此真实用户问答、回复投递及来源措辞的在线端到端结果仍标记为未知；已完成的是生产文件、进程、网络连接和只读 MCP 数据路径验证。

## 2026-09-10 内部知识库 Web 候选（未部署）

- 新增 `web/` 只读静态站生成器、Obsidian 风格界面资源、授权声明边界及测试；未修改 Nginx、域名、生产目录、Kbot 权限或钉钉应用配置。
- 候选路径 `/tmp/dek-web-candidate`；共生成 `1168` 个内容页面（Wiki `1066`、Source `102`）及入口页，大小 `9.4M`；不含路径中带“排除”的文档。
- 支持 Wiki/Source 导航、统一搜索、Wiki-link、反向链接、标签、面包屑、页面目录、深浅主题和移动布局。左侧导航从单一 manifest 动态加载，避免在每页复制完整目录。
- 授权模块默认拒绝缺失、篡改、过期及 `kbot_allowed=false` 的声明，只接受短期签名且明确具有 Kbot 权限的员工声明。该模块是认证网关契约，不等于已经完成钉钉免登或扫码登录。
- QA 与 Web 测试合计 `65/65` 通过；优化后候选内链检查 `3746` 条、`0` 断链，候选大小 `9.4M`。浏览器自动化后端未能启动，因此视觉验收尚未完成；正式部署前仍需钉钉应用授权参数、认证网关、Nginx 接入和有权限/无权限账号端到端验收。

## 2026-09-10 Web 钉钉认证网关续开发（未部署）

- 根据钉钉开放平台当前文档实现 OAuth 登录地址、一次性 5 分钟 state、授权码换取用户 token、当前用户查询、企业校验、8 小时签名会话、安全 Cookie、认证检查端点、静态文件只读服务、路径穿越防护和 CSP。
- 官方“获取企业内部应用的可使用范围”接口需要 Kbot `AgentId` 和“管理微应用的权限”。服务器当前未配置 AgentId；用户确认该权限未开通，并选择暂时跳过真实权限同步。
- 因而真实钉钉客户端保持 fail-closed：在可使用范围适配器完成前，不能把“同企业成员”直接当作“具有 Kbot 权限”，也不会签发生产会话。该状态不可部署为可用网站。
- QA 与 Web 测试合计 `71/71` 通过，`git diff --check` 通过；Kbot 与摄入 timer 均保持 active。未修改 Nginx、钉钉应用、生产站点或凭据，未提交或推送。

## 2026-09-10 Kbot 可使用范围适配器（未部署）

- `/var/lib/dek-web/secrets/environment` 中 AgentId 已确认存在、格式正确，权限为 `0600 root:root`；未记录或输出具体值。
- 使用现有应用凭据只读调用钉钉应用 token 和“获取企业内部应用的可使用范围”接口成功；当前返回直接用户 `3`、部门 `0`、角色 `0`，证明所需接口权限实际上已经可用。
- 认证客户端已实现 unionId → userId 转换，并按直接用户、部门、角色及仅管理员四种范围执行 fail-closed 判断；响应结构、标识符或用户详情异常时拒绝，不降级为企业全员。
- QA、Web、OAuth、权限范围和只读服务测试合计 `99/99` 通过；`git diff --check` 通过。新增 `web/DEPLOYMENT_PLAN.md`，正式 systemd/Nginx/钉钉回调配置仍未执行，需另行批准。

## 2026-09-11 DEK Web 生产服务部署

- 用户明确批准后部署独立 `dek-web` 用户、`/opt/dek-web/app`、`/var/lib/dek-web/site`、root-only 环境文件和 `dek-web.service`；仅监听 `127.0.0.1:9120`。未授予源仓库、Kbot profile、生产索引或管理员 Hermes 读取权限。
- 部署前备份位于 `/var/backups/dek-web/deploy-20260911_093445_+0800`；包含原 AgentId 配置和 Nginx 配置，目录受 root 权限保护。
- Nginx 新增 `/kb/` 反向代理并重载，原 Dashboard `/` 路由保留。未认证 `/kb/` 返回 `302` 到钉钉 OAuth，POST 返回 `403`；离线签名生产检查访问 manifest 返回 `200`，文档 `1168`（Wiki `1066`、Source `102`）。
- `dek-web.service` 为 enabled/active/running，`NRestarts=0`；Nginx、Kbot 和摄入 timer 均 active。`nginx -t`、`systemd-analyze verify`、`git diff --check` 及完整 `100/100` 测试通过。
- 钉钉开发者后台是否已登记 `https://regkb.chenponai.com/kb/auth/callback` 尚无证据；未使用真实员工账号登录，也未验证有权限账号放行、无权限账号拒绝，因此钉钉端到端验收仍为未知。

## 2026-09-11 DEK Web 登录与阅读体验补充

- 用户真实登录先后确认 OAuth 回调、`Contact.User.Read` 和成员信息读权限链路可用；授权成功后生产请求返回 `200`。无权限账号拒绝行为仍未进行真实账号验收。
- 修复 WSGI 中文 URL 解码，中文 Wiki 页面生产离线授权检查返回 `200`；首页不再自动跳转首篇文档。
- manifest 增加真实文件夹层级树：Wiki `1066` 篇、`19` 个顶层目录；Source `102` 篇、`12` 个顶层目录。目录支持逐层展开、当前路径展开、高亮和本地状态记忆。
- 所有 `1168` 个页面增加“笔记信息”，展示现有 `no/date/question/source/tag_pages/tags` 和笔记路径；显式 Wiki-link 才建立 Source 内链，不对纯文本来源猜测关联。构建结果含 `552` 个“来源笔记”页面标记、`14` 个“引用此来源的 Wiki”页面标记。
- 登录声明增加钉钉昵称，页面通过同源 `/auth/me` 仅获取显示名；增加 `/auth/logout` 清除 HttpOnly 会话并返回首页。线上离线授权检查：页面、用户信息均 `200`，退出为 `302` 且清除 Cookie。
- 完整测试 `114/114` 通过，`git diff --check` 通过；Nginx、`dek-web`、Kbot 和摄入 timer 均 active，未提交或推送。
- 搜索升级为浏览器本地模糊检索：Unicode 全角/半角、大小写、空格及标点归一化，多关键词可跨标题、标签、路径和正文匹配，标题与标签优先排序，并允许中文长查询少量漏字。完整测试增加至 `118/118`；生产以“上市许可持有变更”“注册 生产场地”“ＣＤＥ，受理”验证均返回匹配结果，页面、搜索脚本和索引均返回 `200`。
- 修复退出后从中文文章重新登录时的 `502`：根因是 OAuth 回调将 Unicode 返回路径直接写入只允许 Latin-1 的 WSGI `Location` 响应头。回调现将返回路径按 UTF-8 百分号编码；回归测试先复现失败后通过，完整测试 `119/119`。已部署代码验证中文返回路径为 ASCII 安全 URL，入口 `302`，部署后日志无 `UnicodeEncodeError`；真实浏览器二次退出/登录仍待用户复验。
- 搜索交互升级为“输入关键词后点击搜索/按 Enter”：结果显示总数、标题、Wiki/Source 类型、路径和正文摘要；近似匹配新增替换错字、漏字、多字及关键词换序。回归测试覆盖“便更”“持有人信息变更”和词序交换，完整测试 `123/123`；生产三组检索均返回相关结果，实测单次检索约 `53–85ms`，页面、脚本和索引均为 `200`。
- 退出流程改为清除会话后停留在公开、`no-store` 的“已安全退出”页，由用户点击“重新登录”，避免钉钉 SSO 自动回登造成未退出错觉。页面用户名占位改为“正在读取…”，随后仅以 `/auth/me` 的签名会话姓名替换，失败时明确显示不可用；HTML/JS/CSS 禁止缓存。完整测试 `125/125`，生产验证退出 Cookie 已清除、退出页无需认证且稳定 `200`、姓名端点及禁缓存响应有效。
- 修复搜索结果 URL 错误落入 `/kb/assets/wiki/...` 或 `/kb/assets/source/...` 的问题，统一相对站点根 `/kb/` 解析；增加索引加载、加载失败、重试、搜索中及完成状态，未加载时按钮禁用而非静默空查。完整测试 `127/127`；生产生成的 Wiki 结果 URL 返回 `200`，未再指向 assets 路径。
- 生产入口调整：`https://regkb.chenponai.com/` 作为钉钉认证知识库，回调改为 `/auth/callback`；`/login` 跳转 `/hermes/login?next=/hermes/`，Hermes Dashboard 位于 `/hermes/` 并设置 `dashboard.public_url`。离线签名生产验证根首页、中文文章和 `/auth/me` 均 `200`，退出 `302` 到根路径退出页；Hermes 登录页 `200` 且登录提交路由由 Hermes 处理。旧 `/kb/wiki/`、`/kb/source/` 链接永久跳转新路径，`/kb` 跳转 `/login`。真实钉钉新回调和 Hermes 密码登录仍待用户浏览器验收。
