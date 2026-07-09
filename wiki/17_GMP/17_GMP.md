---
aliases:
  - "#17_GMP"
tags:
  - "17_GMP"
master:
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
FROM "wiki/17_GMP"
WHERE no != null
SORT file.folder ASC, no ASC
```
