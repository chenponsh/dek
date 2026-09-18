> [!warning] Superseded architecture evidence
> 本文保留失败候选架构的历史证据，不再作为部署依据。替代架构见 `deploy/PRODUCTION_ROLLOUT.md`。

# 钉钉审核与内容发布（已废止）

基础设施只按 `deploy/PRODUCTION_ROLLOUT.md` 的 Stage A 由管理员逐项安装和验证；自动流程属于 Stage B。

- 审核权限来自大小写精确的企业 `userId`。候选配置保持 `DEK_REVIEWER_IDS=`，空名单拒绝所有审核。
- publisher 只能构建一个不可变内容 release：`site/`、`dek-kb.json`、`release.json`；请求仅携带固定身份和内容摘要，没有命令、任意路径或基础设施字段。
- controller 只固定、安装上述内容，原子切换唯一 `/var/lib/dek-deploy/control/current`，写 transaction/lock、boot-generation/proof 和签名 outcome 元数据，并保留既有耐久回滚。
- Web 使用 `current/site`，QA 使用同一个 `current/dek-kb.json`。
- 自动流程不得修改应用代码、credential、用户/组、systemd unit、nginx 或其他基础设施。publisher 不能写生产 release/current；controller 不能读写 repo、审核状态或各服务 secret。
- 现有 root source-ingest 服务本次不迁移，仍是残余信任边界；其可写 repository 永远不能提供 publisher 可执行代码。publisher 只从 root-owned、不可变的 `/opt/dek-review-publisher/app` 导入，候选 repository 代码只在无 queue/request/Git credential、无 spool 权限的 `dek-gate` 沙箱中执行。
- 生产 content release 为 root:`dek-publish`、目录 `0550`、文件 `0440`；`dek-web` 与 `dek-qa` 通过 `dek-publish` 只读。每个服务 secret 目录独立 `0700`，不得通过共享组泄露。
- `dek-review-publish.timer` 和 `dek-deployment-controller.timer` 出厂不启用；review nginx 候选不存在。管理员完成 Stage A 和真实验收前不得启用。

只有 QA/Web 对同一 release、boot nonce 和内容摘要的 live proof 均通过，controller 才写 `activated`。失败时恢复旧 pointer 和 boot-generation 后验证旧服务；无法确认恢复则写 `rollback_failed`，不得宣称未改变生产。

当前人工未知项：真实 reviewer IDs、review DNS/TLS、生产旧 Web/QA 路径与 unit 状态、主机 ACL/LSM、credential 值和真实账号级路径验收。未确认这些项前不能声称生产可用，也不得部署。
