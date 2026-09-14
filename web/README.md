# DEK 内部知识库 Web

只读生成器，将已审核的 `wiki/` 与 `source/` 转换为接近 Obsidian 阅读体验的静态网站。

## 当前能力

- Wiki、Source 左侧导航及统一全文搜索
- Markdown 表格、代码块、标题目录
- Obsidian Wiki-link、来源链接和反向链接
- 标签、面包屑、深色/浅色主题及移动端布局
- 仅处理 `wiki/**/*.md`、`source/**/*.md`
- 任一路径段含“排除”的文件均跳过
- 不读取或发布 `.obsidian`、`_raw`、`ingestion`、`_`、Git、配置、日志、索引或凭据

## 构建候选站点

```bash
PYTHONPATH=/srv/projects/dek /var/lib/dek-qa/venv/bin/python -m web.site \
  --vault /srv/projects/dek \
  --output /tmp/dek-web-candidate
```

本地只读预览：

```bash
python3 -m http.server 9120 --bind 127.0.0.1 --directory /tmp/dek-web-candidate
```

该预览服务器不包含身份认证，只能绑定回环地址，不得直接对公网监听。

## 身份认证边界

`web.auth` 定义了短期签名授权声明：只有 `kbot_allowed=true` 的有效员工声明才放行，缺失、篡改、过期或无 Kbot 权限均拒绝。`web.app` 与 `web.dingtalk_gateway` 已实现只读 WSGI 服务、OAuth 跳转、一次性 state、回调换取身份、安全 Cookie、企业校验、路径穿越防护，以及 Kbot 应用可使用范围校验：

1. 钉钉客户端内免登或浏览器扫码登录；
2. 向钉钉服务端验证身份；
3. 使用 Kbot 的 `agentId` 实时调用钉钉“获取企业内部应用的可使用范围”；
4. 将 OAuth 身份映射为企业 `userId`，并按直接用户、所属部门、所属角色或仅管理员范围判断；
5. 接口异常、响应格式异常、身份缺失或范围不匹配时默认拒绝；
6. 验证通过后签发最长 8 小时的站点会话；
7. Nginx 在返回静态页面前强制执行认证。

运行认证服务至少需要独立配置 `DINGTALK_CLIENT_ID`、`DINGTALK_CLIENT_SECRET`、`DINGTALK_AGENT_ID`、`DEK_WEB_REDIRECT_URI` 和 `DEK_WEB_CLAIM_SECRET`。静态站点本身不保存钉钉凭据，不应绕过认证服务直接发布。

## 测试

```bash
PYTHONPATH=/srv/projects/dek /var/lib/dek-qa/venv/bin/python -m unittest web.tests.test_auth web.tests.test_site -v
```
