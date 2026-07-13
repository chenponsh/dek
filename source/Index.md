---
cssclasses:
  - dv-source-index-table
---

```dataview
TABLE WITHOUT ID
  file.link AS "题目",
  entity_alias AS "主体",
  url AS "URL",
  path AS "路径",
  dateformat(last_updated, "yyyy-MM-dd") AS "最后更新"
FROM "source"
WHERE entity != null
SORT last_updated DESC, entity_alias ASC, file.name ASC
```
