# DEK 知识库：运维与开发交接（给管理员 Hermes / 后续维护者）

写于 2026-09-21。读完这份文档再动手。文档里的每条“规矩”都是实际出过事故才写下的。

## 0. 工作边界（先读这一节）

1. **审核人的“批准”和“发布”点击是用户的，不是你的。** 永远不要替用户批准、发布或伪造决定。
2. **不要放宽安全控制**：免审批发布只允许“纯删除”；批准决定与具体草稿快照绑定；构建器只信任已签名的批准包；`.pdf`、`.png` 之外的文件类型不得随意加入发布白名单。若系统的安全检查拦下了你，**不要绕过**，把原因和你想做的事告诉用户，由用户决定。
3. **下列动作先问用户，得到明确同意再做**：删除任何数据（wiki 条目、草稿、来源行）、重置抓取起点 `last_updated`、修改机器人（钉钉问答）的回答规则、重启会打断人的服务（`dek-review` 重启会让审核人已打开的表单失效）、改动 systemd 单元。
4. **推送前先看有没有人正在审核**：`wc -l /var/lib/dek-review/decisions/decisions.jsonl` 和 `journalctl -u dek-review --since -10min`。推送本身现在安全（发布程序会把批准叠在最新的 origin 上），但不要在用户批准与发布之间部署或重启。
5. **不要为了“试一下”去运行 `dek-source-ingest.service`**：它会推送草稿到仓库。要看抓取会产生什么，用无写入的演练（见 §7）。
6. 用中文向用户汇报；说实话，包括自己的失误和没有验证过的部分。

## 1. 系统总览（数据怎么流）

```
官网/接口 ──抓取──▶ source/ 来源笔记（新行/新文章）
                    └─▶ ingestion/rough/ 待审草稿（每个问题一条，含 source_url）
审核页 /review/ ──审核人批准──▶ decisions.jsonl（只增不改，带 MAC）
发布：dek-review-publish ─▶ dek-builder（无网络沙箱，跑 6 步检查+生成网站） ─▶ dek-review-publish（签名+推送 origin）
      ─▶ dek-activator（激活为线上版本） ─▶ dek-source-refresh（刷新审核页的数据快照）
线上网站 regkb.chenponai.com（dek-web，钉钉登录） ；钉钉问答机器人 dek-qa 读已发布版本里的 dek-kb.json
```

- **仓库**：`/srv/projects/dek`，分支 `main`，远端 `https://github.com/chenponsh/dek.git`。
- **线上是“静态发布版”**：每次发布生成一个整站快照。网页里的 JS/CSS 来自已安装的代码（重启 dek-web 生效），页面内容来自发布快照。
- **来源笔记 `source/`** 是中间层；**`wiki/`** 是正式知识（每条 `wiki/<大类>/<小类>/<编码>-<序号>.md`，frontmatter 有 `no/date/question/source/source_url/tag_pages/tags`）。
- **图片**放 `wiki/_images/`，**PDF 附件**放 `wiki/_attachments/`，条目里用相对路径引用（`../_images/x.png`、`../../_attachments/x.pdf`）；只允许英文文件名、真 PDF、≤25MB。

### 关键路径与服务

| 东西 | 位置 |
|---|---|
| 各服务安装目录 | `/opt/dek-{web,review,qa,publisher,builder,activator,source-ingest}/current`（由 `install_release.sh` 更新） |
| 审核决定队列 | `/var/lib/dek-review/decisions/decisions.jsonl` |
| 审核页数据快照 | `/var/lib/dek-review/input/repository.bundle`（抓取结束和每次发布结束时重写） |
| 线上版本 | `/var/lib/dek-activate/control/active.json`（`sequence`、`commit`）；每版在 `/var/lib/dek-activate/releases/<decision>-<decision>/` |
| 发布程序状态 | `/var/lib/dek-publisher/state/<decision_id>.json` |
| 构建器失败记录 | `/var/spool/dek-activate/builds/.builder-failures/`（同一个包失败 3 次后不再重试，删记录可重试） |
| 钉钉机器人运行规则 | `/var/lib/dek-qa/hermes/profiles/dek-qa/SOUL.md`（**与仓库 `qa/config/SOUL.md` 是两份**，改仓库后要复制过去并重启 `dek-qa`） |
| 抓取配置 | `ingestion/automation/config.json`（来源清单、`earliest_date`） |
| 抓取程序 | `ingestion/automation/{cli.py,sources.py,fetchers.py,core.py}` |

审核页按钮：`立即拉取最新源`（→ 抓取）、`发布已批准内容`（→ 发布）。抓取约 1～2 分钟，结束后审核页数据快照才更新。

## 2. 每次改动的标准流程

```
1. 改代码/内容
2. deploy/tools/run_gates.sh            # 全部通过（退出码 0）才能继续
3. git add … && git commit && git push origin main
4. deploy/tools/install_release.sh      # 把已提交的代码装进各服务（不会重启任何服务）
5. 按需重启：dek-review / dek-qa / dek-web（先确认没人在审核，见 §0.4）
6. 涉及构建器的改动：deploy/tools/check_builder_as_builder.sh   # 用构建器自己的身份、断网跑一遍
7. 之后每次有人发布：deploy/tools/publish_check.sh
```

- 改了 `deploy/systemd/*.service`：手工 `install -m 644 deploy/systemd/X.service /etc/systemd/system/` 然后 `systemctl daemon-reload`（安装脚本不管这个）。
- 新增的 `ingestion/automation/*.py` 模块，必须同时加进 `deploy/source_ingest_entrypoint.py` 的必需文件列表，否则摄入服务启动就失败。
- 安装清单 `deploy/PACKAGE.sha256` 由 `deploy/tools/gen_package_manifest.py` 生成（`run_gates.sh` 会自动重生成）；漏进清单的文件**不会被安装**。夹具文件保持 LF 换行。
- **网页内容的修改不需要重启**，但要等下一次发布才在线上生效（见 §4）。

## 3. 踩过的坑（都出过事故）

1. **提交前按真实退出码判断测试。** 曾两次用 `cmd; echo ok` 提交了有失败测试的代码。构建器每次发布都会跑 `ingestion/automation/tests`、`web/tests`、`qa/tests`，**一个失败的测试会挡住之后所有发布**。始终用 `run_gates.sh`。
2. **不要以 root 身份、用构建器的环境（HOME=/var/empty/dek-builder）跑构建步骤。** qa 测试会在那里创建 root 拥有的 `.hermes`，真正的构建器就会因权限错误失败。用 `check_builder_as_builder.sh`，并确认 `/var/empty/dek-builder` 为空。
3. **发布失败可能是静默的。** 当前这条决定的构建失败时，`dek-review-publish-manual.service` 仍显示成功。每次发布后运行 `publish_check.sh`，看版本号是否前进。
4. **批准的草稿必须还在仓库里。** 审核页的列表来自数据快照，点“立即拉取”后要等抓取**跑完**再刷新页面；抓取期间页面会拒绝批准（这个保护已做）。批准一条仓库里已被删除的草稿，发布会报 `rough source is unreadable`，该决定被标为“不可重试”。
5. **数据整体重置后必须跑审计**：`python3 -m ingestion.automation.audit --root .` 退出码 0。审计不过会让所有发布的构建失败（2026-09-19 出过）。
6. **抓取起点语义**：每个来源笔记 frontmatter 的 `last_updated`；实际起点 = `max(last_updated, earliest_date 前一天)`。`earliest_date`（现为 2026-03-01）之前的内容**不再抓取、不参与比对**，以 Word 初始化为准。改回 `last_updated` 才会重新抓回更早的内容（先问用户）。
7. **同步发布（免审批）只接受纯删除。** 想让线上快速删掉内容：删除 → 推送 → `systemctl start dek-review-publish-manual.service`。新增/修改 wiki 内容无法这样上线，会随下一次经批准的发布一起带上线（发布是基于 origin 最新提交构建的）。已有条目内容不能被审核决定覆盖（会报 `wiki_path already published with different content`）。
8. **抓取产出的关键词粗筛**在 `ingestion/automation/sources.py`（`_NOT_DRUG`、`_DRUG`、`_BJ_*` 等），是判断“是否属于化学药品制剂”的实际逻辑，文档 `AGENTS.md` 写的“逐条判断”与之并不完全一致。
9. **中文文件名给 Windows `scp` 会失败**：让用户先复制成英文名再传。
10. **pip 走内部镜像，需要去掉代理环境变量**：`env -u https_proxy -u http_proxy … pip install --target …`。PDF 读取库 pypdf 装在摄入服务自己的目录 `/var/lib/dek-source-ingest/vendor/python`，系统 Python 里没有；手工跑相关代码要 `PYTHONPATH=/var/lib/dek-source-ingest/vendor/python`。
11. **杀进程用端口/PID，不要 `pkill -f 模式`**（会杀掉自己的 shell）。
12. **升级服务后要重启长驻服务**（`dek-web`、`dek-review`、`dek-qa`），`install_release.sh` 不会重启。

## 4. 发布相关的实际行为

- 用户批准一条草稿 → 点“发布” → 链路约 1.5 分钟 → 线上版本号 `sequence` +1，`publish_check.sh` 应显示新的 `LIVE: sequence`。
- 发布结束后会自动刷新审核页的数据快照，草稿显示为“已发布”。
- 网页只有一个“发布已批准内容”按钮；用户批量批准后点一次即可（多条会串成链依次推送）。
- **2026-03-01 及之后的原始资料，批准前不公开**（`web/site.py` 的 `_hold_back_unapproved_source`，用户 2026-09-21 定的规则）：摄入会把新抓的来源笔记/表格行直接写进仓库，但网站构建时会把它们拿掉——带 `date` 的来源笔记要有已批准的 wiki 条目通过 `source:` 指向它才保留；来源表格里日期 ≥ 2026-03-01 的行，要有指向该来源笔记、且问题文字对得上的 wiki 条目才保留。数据没删，条目批准并发布后下一次构建自然出现。`SOURCE_APPROVAL_FLOOR` 必须等于 `ingestion/automation/config.json` 的 `earliest_date`（有测试守着）。问答机器人的索引本来就只含 wiki。
- 已知的死决定：`FdW3lrI_…`（一条已不存在的北京旧草稿）。日志里的 `DEK publish skipped … cannot be retried` 就是它，不是新故障；审核页会把它显示成“已发布”（草稿不在了），仅显示问题。

## 5. 当前状态（2026-09-21）

- 线上第 14 版。2026-03-01 之前的知识以“官方问答集锦 V8（更新至2026-02-28）”Word 为准，已核对：920 条全部对上，差异已补齐（表格、图片、PDF、日期）。核对脚本与结果在 `/root/dek-word-input/`（`work/compare2.py`、`report_v8_v2.xlsx`）。
- 2026-03-01 及之后的内容已清空并重新抓取，审核页有约 36 条待审草稿（CDE 19、江苏 2、北京 5、安徽 5、CPC 5…）。
- 已自动抓取的来源：上海、江苏×3（中等变更、前置服务、你问我答）、江苏药小问、海南、陕西、北京、安徽、NIFDC、山东检问百答（仅局网站上的文章，微信文章需人工）、CPC（含 PDF 全文提取）、CDE×3（需要浏览器）。
- 钉钉提醒显示问题原文；机器人回答规则改为“每个问题后跟自己的官网链接”（**回答的实际排版还没在钉钉里实测**，请用户用“最近 60 天发布了什么”这类问题验证）。
- 232 条 wiki 条目带官网文章网址（`source_url`）；约 258 条只有栏目页链接；约 419 条没有任何链接。CDE 共性问题在官网上没有每条问题的网址，做不到。

## 6. 待办与待用户决定

**等用户决定（不要自己定）**
- 35 条“我们有、Word 里没有”的条目（山东局申请表第 20 项 32 条：`1620-0007…0038`；药典委 2025 版实施注意事项 3 条：`15-0020…0022`）：保留还是删除。
- 4 条分类差异（`0412` “主要研究者” vs Word 的“国际多中心”）：以谁为准。
- 是否让审核页在批准时检查草稿是否还在仓库（需要审核服务能查看 origin，改动较大）。

**可以做（低风险，先告诉用户你要做什么）**
- 给北京、上海旧条目补 `source_url`（北京新条目已自动带；旧条目要按信件内容匹配）。
- 把“关键词规则、来源开关、抓取下限”做成一份结构化规则文件，再做只读的“规则页面”给审核人看；之后才考虑网页上修改（需确认流程和预览）。用户说过“逻辑先不管，后面再说”，**他再提时再做**。
- 修复 `tests/test_security_final_block.py` 里两个早就存在的失败（一个读主机上的审核环境文件，一个 PATH 断言过时）；它们不在构建器的固定检查里。
- 清理 `/root/dek-word-input/stray-builder-hermes-20260921`（我移走的构建器主目录残留，可删）。

## 7. 常用命令

```bash
# 抓取会产生什么（无写入演练，CDE 关闭；需要 pypdf 时带 PYTHONPATH）
cd /srv/projects/dek && PYTHONPATH=/var/lib/dek-source-ingest/vendor/python python3 - <<'EOF'
import sys; from datetime import datetime; sys.path.insert(0, ".")
from ingestion.automation import cli
c = cli.load_config(); c["cde"]["enabled"] = False
res, writes = cli.inspect(c, datetime.now().astimezone())
for k, v in res["report"].items(): print(k.split("/", 1)[-1][:44], v.get("status"), v.get("new_count"))
EOF

deploy/tools/publish_check.sh                       # 发布后核对
journalctl -u dek-review --since -30min --no-pager  # 审核页请求（POST /decision、/publish、/trigger-ingest）
journalctl -u dek-source-ingest.service --since -1h --no-pager   # 抓取
journalctl -u dek-builder.service --since -15min --no-pager | grep -E "DEK build|BundleError"
```

## 8. 联系用户时

- 说明“做了什么、验证了什么、没验证什么”。区分“已上线”和“已在仓库、等下次发布才上线”。
- 有多个选择时给推荐，不要一次抛很多问题。
- 用户是审核人，不写代码；用生活化的语言，少用术语。
