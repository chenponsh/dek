---
aliases:
  - "#16_注册变更/1629_批准后变更管理方案（PACMP）"
tags:
  - "16_注册变更/1629_批准后变更管理方案（PACMP）"
master: "[[wiki/16_注册变更/16_注册变更]]"
cssclasses:
  - dv-compact-table
  - dv-overview-table
---

```dataview
TABLE WITHOUT ID
  file.link AS 项目,
  question AS 问题,
  source AS 来源,
  dateformat(date, "yyyy-MM-dd") AS 日期
FROM "wiki/16_注册变更/1629_批准后变更管理方案（PACMP）"
WHERE no != null
SORT file.folder ASC, no ASC
```
