# QA 修复证据清单（脱敏）

记录时间：2026-09-08T17:28:03+0800。

本文件只记录可复核的版本、内容摘要、计数和测试范围，不包含凭据、白名单值、用户/群/会话 ID、消息正文、日志正文、运行配置正文或投递记录。

## 基线与版本

- 仓库 HEAD：`a1f6cf7e6172b7094ff539016223dcc2ab57a893`；本轮文件仍未暂存、未提交、未推送。
- 独立运行环境：Python `3.12.3`；uv `0.12.8`；Hermes Agent 包版本 `0.21.0`。
- `/opt/dek-qa/hermes-agent` 是 editable 本地安装，不含 Git 元数据，因此不存在可验证的部署 commit。其 6061 个 Python/配置/锁文件的脱敏 manifest SHA-256 为 `6d4583076a13a5ed67e6ffe232ea69cb07f564f26055ad6e66dd1d487561e353`；这只能标识当前副本，不能替代来源 commit。
- Hermes `pyproject.toml` SHA-256：`c70c8b52f6cc08a4e65f0fc1713c26814fd4f19811bc7e01de645009b2a76600`。
- Hermes `uv.lock` SHA-256：`383cd8f98ec23dc3fe4cf63759ec73be5a869cc953f068b4e79ec4e8ed00287d`。
- 实际 DingTalk adapter SHA-256：`fbcad324849c05929ab3873709e5e72b354627bd6328c36c475c6e44d4a0dc0b`。
- 实际 gateway 鉴权 mixin SHA-256：`20e3599bc4ea88b651cbdcee70c536de201419dc8097edea286078da4e201c05`。
- 实际 MCP 客户端实现 SHA-256：`8b0bd48bd96df60a2c92bb9d5b6f0fd5f794e9dfb5f39e012ef25a749fd395b5`。

## 依赖可复现性

- 直接依赖已显式包含：`dingtalk-stream==0.24.3`、`mcp==2.0.0`、`httpx2==2.7.0`、`starlette==1.3.1`、`requests==2.33.0`、`websockets==15.0.1`。
- `requirements-dek-qa.txt` SHA-256：`508f28f05a7e03517709f6eadf9d41dde82ec1684ea842d59a5c7c89ee7ff820`。
- 带传递依赖和制品哈希的 `requirements-dek-qa.lock.txt` SHA-256：`783c88d7b060b2b269ede3046c9622d22b0c5f2d14a9e64d113f60b88aeb52dd`。
- 已在新建的临时 Python 3.12 venv 中使用 `uv pip install --require-hashes` 安装 41 个锁定包；随后运行核心 unittest，40 项通过。临时环境已删除。

## 索引只读审计与候选构建

- 当前运行索引保持未修改：版本 `1`，1066 文档，SHA-256 `189f3ec4fae11d82136ad57fcd9faf6c8a990469e01cd1eb215936af6fe5b41a`。
- 对当前运行索引逐路径检查：含“排除”的文档为 `0`。这证明当前产物未发现该类污染，不证明旧构建器规则正确。
- 修复后的候选索引位于 `/tmp/dek-qa-proposed-index-v2.json`，未替换运行索引：版本 `2`，1066 文档，SHA-256 `49c02890a7e4ff365a5633d08dce6d74cda5021f8a3c9e458175bf931ddd0f4d`。
- 候选索引输入 SHA-256：`d6922f5debcb00c58179ada75c01ebacd9ae545528255c57e82f626b2fc51770`；构建器版本 `2`；构建器 SHA-256 `4c8616ce714f768bf7ae37119f017edd6039ea0d01f41a1d5220a6e92415c6af`，与候选 metadata 一致；含“排除”的文档为 `0`。
- 旧索引有 22 个文档含 `official_urls`。按“只有唯一可解析的显式 `source/...` wikilink + source 文件显式 `source_url` 才可验证”的新规则，候选索引中 0 个文档获 `verified` URL；946 个标为 `source_status=unknown`，120 个标为 `none`。未修改 wiki/source 正文，也未将旧正文 URL 继续冒充为已验证官方来源。
- 同名 source stem（包括没有 source_url 的文件）会使构建失败；带目录的错误引用不会退回同名 stem。

## 实际 Hermes/DingTalk 边界

- 身份在 DingTalk adapter 接收层已经可用：适配器从入站消息取得发送者 ID 和 staff ID，在调用 gateway handler 前执行用户白名单、群白名单和 mention 条件。
- 进入 gateway 后，`GatewayAuthorizationMixin` 对 `SessionSource.user_id` 再执行默认拒绝鉴权；会话分组逻辑在后续层使用用户和会话身份。
- 针对 `/opt/dek-qa/hermes-agent` 实际代码运行 4 项合成输入集成测试：未授权用户在 dispatch 前拒绝、未授权群在 dispatch 前拒绝、gateway 默认拒绝复核、仓库模板白名单键经真实解析器进入 adapter extra；4/4 通过。测试使用虚构标识，不读取或输出真实白名单。
- 运行 profile 的 `dek_kb` MCP 配置经脱敏结构检查为本地 `stdio`，命令是独立 venv Python，未配置远程 URL。MCP 服务本身不接收 DingTalk 身份；身份/会话门控发生在进入模型与 MCP 调用之前。因此没有为了形式审查而把 `access.py` 机械导入 MCP。
- 历史运行 profile 的白名单键位于 DingTalk 顶层，真实解析时不会自动进入 adapter extra；因此历史 adapter 层群白名单是否生效为未确认。仓库模板已修正，运行 profile 尚未迁移，白名单值未修改。
- 以上是实际部署代码路径的离线集成证据，不是新的 DingTalk 在线投递验收。

## 本轮验证结果

- 独立运行环境完整 unittest：44/44 通过（其中实际 Hermes/DingTalk 边界测试 4/4）。
- 全新锁定依赖环境核心 unittest：40/40 通过。
- MCP 连续流测试依次注入 parse error、非法 JSON-RPC 对象、非法 call 信封和工具 schema 错误；最后的合法 `tools/list` 仍由同一 stdio 进程处理。工具 schema 错误使用 `result.isError=true`，协议/信封错误使用标准 JSON-RPC error。
- `systemd-analyze verify deploy/systemd/dek-qa.service`：通过。
- `uv pip check --python /var/lib/dek-qa/venv/bin/python`：100 个已安装包兼容。
- Python 编译检查：通过。
- 候选文件扫描：17 个文件，凭据赋值模式 0 命中，危险 AST 调用 0 命中。

## 服务状态

- `dek-qa.service`：`inactive/dead`、`disabled`、`MainPID=0`；修复期间未恢复。
- `dek-source-ingest.timer`：`active`、`enabled`；本轮未停止或修改。
- 仓库候选 unit 把可写范围从整个 `/var/lib/dek-qa` 收紧为 `/var/lib/dek-qa/hermes`，并显式将 index 和 secrets 标为只读。该 unit 尚未部署；只完成静态 verify，运行兼容性仍需部署阶段验证。

## 明确缺失的证据

以下历史证据没有捕获，本轮不补造：

- 未捕获历史模型请求的完整 payload，不能逐字段证明模型上下文隔离。
- 未授权真实用户在线拒绝未执行。
- 同群两名已授权用户的在线历史隔离未执行。
- 群聊未 @消息缺少可证明钉钉已投递到本地 adapter 的入站证据。
- 真实在线 MCP 故障回复未执行。
- 旧 22 个 URL 的直接来源关系和相关日期语义未获验证；新候选因此全部降为未知，不以旧回答或 URL 路径片段反推。
- `/opt/dek-qa/hermes-agent` 缺 Git 元数据；只能提供当前内容 manifest，不能声称可从某个 commit 精确重建。
- 收紧后的 systemd unit 尚未部署运行；目前只有模板语法和静态边界验证。
