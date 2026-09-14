# Ingestion

本文件夹用于保存可纳入 git 的来源摄入说明、结构化运行记录和 rough 草稿。

- 每次遍历/增量摄入 source 笔记后，将结构化运行记录保存到 `logs/source_ingest_YYYYMMDD_HHMM_report.json`。
- 这里使用 `ingest`，表示“将外部来源内容摄入本地知识库”：包括抓取远端、解析清洗、主题筛选、写入 source、生成 `rough/` 草稿、记录跳过/失败原因。
- 记录文件应包含：摄入日期、逐来源状态、远端最新日期/数量、是否发现新增、是否生成 `rough/` 草稿、跳过或失败原因。
- 临时抓取缓存、HTML、PDF、脚本调试输出仍放在 `_/`，不纳入 git；可复用的摄入记录放在 `logs/`，摄入草稿放在 `rough/`。
- `updated_with_new` 必须同时生成 rough，并在报告中以 `rough_sources` 建立 rough 到 source 的对应关系；否则整次写入安全停止。
- rough 使用 `pending_review / needs_clarification / approved / promoted / rejected` 状态；`promoted` 必须填写 `wiki_target`，`rejected` 必须在正文记录理由。
- rough 的 `published_date` 表示内容发布日期，`ingested_at` 表示摄入日期；“最近内容”以 `published_date` 为准，不使用文件修改时间。
