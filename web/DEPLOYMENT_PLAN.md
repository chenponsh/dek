> [!warning] Superseded architecture evidence
> 仅保留历史证据。已批准的替代架构以 `deploy/PRODUCTION_ROLLOUT.md` 为准。

# DEK Web 生产部署方案（已废止）

状态：开发与只读范围 API 验证完成，尚未部署。

## 前置条件

1. 钉钉应用登记回调：`https://regkb.chenponai.com/auth/callback`。
2. 保持 Kbot 当前可使用范围不变；网页每次登录实时读取同一 AgentId 的范围。
3. 生成独立 `DEK_WEB_CLAIM_SECRET`，不得复用 Kbot 或 Hermes 密钥。
4. 部署前备份 Nginx 配置，并记录候选站点与应用摘要。

## 隔离设计

- 运行用户：独立 `dek-web`，无登录 shell。
- 只读站点：`/var/lib/dek-web/site`。
- 运行配置：`/var/lib/dek-web/secrets/environment`，`0600 root:root`。
- 应用代码：`/opt/dek-web/app`，`root:root`，运行用户不可写。
- 监听：仅 `127.0.0.1:9120`。
- 不授予 `/srv/projects/dek`、Kbot profile、索引、日志或管理员 Hermes 的读取权限。
- 网站只能读取构建后的 Wiki/Source 页面。

## Nginx 路由

- `/` 反向代理至知识库认证服务，不直接使用 `alias` 暴露静态目录。
- `/auth/callback` 仅接受钉钉 OAuth 回调。
- `/login` 跳转 Hermes 登录页；Hermes Dashboard、API 和 WebSocket 位于 `/hermes/`。
- 旧 `/kb/wiki/`、`/kb/source/` 文章链接重定向到根路径；`/kb` 与 `/kb/` 跳转 `/login`。
- 增加 HTTPS、请求大小限制、超时、安全响应头和登录端点限速。

## 切换步骤

1. 从当前仓库重新构建候选站点并复验页面数、排除路径和断链。
2. 创建时间戳备份；安装 app、site、环境文件和候选 unit。
3. `systemd-analyze verify` 通过后启动 `dek-web.service`。
4. 先在回环地址验证：无会话拒绝、伪造/过期会话拒绝、静态路径穿越拒绝。
5. `nginx -t` 通过后原子安装站点配置并 reload。
6. 使用一个具有 Kbot 权限的账号验证登录和 Wiki/Source；使用一个无权限账号验证拒绝。
7. 未获得上述真实证据前，不标记端到端验收通过。

## 回滚

1. 恢复 Nginx 时间戳备份并 reload。
2. 停止并 disable `dek-web.service`。
3. 恢复上一版 app/site/config；Kbot 与摄入 timer 全程不操作。
