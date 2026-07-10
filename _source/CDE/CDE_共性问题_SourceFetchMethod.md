---
purpose: CDE 共性问题 source 笔记抓取与增量整理方法
scope:
  - "[[CDE_共性问题-受理共性问题]]"
  - "[[CDE_共性问题-常见一般性技术问题]]"
  - "[[CDE_共性问题-化学仿制药共性问题]]"
---

本文件记录 CDE「信息公开 > 共性问题」页面的数据抓取方法，用于维护 `wiki/_source/` 下的 CDE source 笔记。

## 与来源笔记的关系

本方法笔记为多个 CDE source 笔记共用。各 source 笔记在 `## 整理规则` 下链接到本笔记；本笔记通过 frontmatter `scope` 维护反向适用范围。

## 适用页面

入口页面：

[https://www.cde.org.cn/main/xxgk/listpage/07edef25f1e7354bfd8490baa0ce056b](https://www.cde.org.cn/main/xxgk/listpage/07edef25f1e7354bfd8490baa0ce056b)

对应 source 笔记：

- `[[wiki/_source/CDE/CDE_共性问题-受理共性问题.md]]`
- `[[wiki/_source/CDE/CDE_共性问题-常见一般性技术问题.md]]`
- `[[wiki/_source/CDE/CDE_共性问题-化学仿制药共性问题.md]]`

## 核心原则

CDE 页面存在动态加载和反爬初始化，直接用 `curl`、`Invoke-WebRequest` 或普通 HTML 解析通常只能拿到 JS 外壳，不能稳定得到真实表格内容。

稳定做法是：

1. 用浏览器打开 CDE 页面，等待页面初始化完成。
2. 在页面上下文中调用网站前端已经加载好的 `myAjax`。
3. 请求接口 `/xxgk/getCommonQuestionList`。
4. 按分页合并 `records`。
5. 将结构化字段写回 Markdown 表格。

## 接口

接口路径：

```text
/xxgk/getCommonQuestionList
```

请求方式：

```text
POST
```

主要参数：

```js
{
  pageSize: 20,
  pageNum: 1,
  probleContent: "",
  probleType: 2
}
```

## probleType 映射

| probleType | 页面栏目 | 对应 source 笔记 |
| --- | --- | --- |
| 1 | 常见一般性技术问题 | `[[wiki/_source/CDE/CDE_共性问题-常见一般性技术问题.md]]` |
| 2 | 受理共性问题 | `[[wiki/_source/CDE/CDE_共性问题-受理共性问题.md]]` |
| 3 | 共性问题专家意见 | 当前接口曾返回 0 条；如后续出现数据，再新建/维护对应 source 笔记 |
| 4 | 化学仿制药共性问题 | `[[wiki/_source/CDE/CDE_共性问题-化学仿制药共性问题.md]]` |

## 返回字段映射

接口返回结构通常为：

```js
{
  code: 200,
  data: {
    records: [],
    total: 267,
    pages: 14
  }
}
```

`records` 中的字段映射为：

| JSON 字段 | Markdown 表格列 |
| --- | --- |
| `probleContent` | 问题 |
| `answerContent` | 解答 |
| `publishTime` | 发布日期 |

不写入网页表格中的「序号」列。

## 写回 Markdown 规则

保留 source 笔记原 frontmatter，并在 frontmatter 与正文之间保留一个空行。

写回前按 `AGENTS.md` 中的来源笔记主题筛选规则过滤：本库以“化学药品制剂”为关键主题；若 CDE 问答中混有仅面向中药、化妆品、医疗器械等无关主题的条目，正式 source 内容不摘录。若因排查需要保留全量抓取结果，应放入 `_` 目录作为过程缓存，而不是写入 `## 内容`。

正文表格格式：

```markdown
| 问题 | 解答 | 发布日期 |
| --- | --- | --- |
| ... | ... | 2026-07-06 |
```

处理细节：

- `answerContent` 中的换行转换为 `<br>`，避免破坏 Markdown 表格。
- 单元格中的竖线 `|` 需要转义为 `\|`。
- 不额外增加序号列。
- 不改写问答实质内容。

## 后续维护：增量抓取策略

2026-07-09 的执行是历史全量抓取。后续再次维护这 3 个数据源时，默认需求应理解为：

> 回顾 `[[wiki/_source/CDE/CDE_共性问题-受理共性问题.md]]`、`[[wiki/_source/CDE/CDE_共性问题-常见一般性技术问题.md]]`、`[[wiki/_source/CDE/CDE_共性问题-化学仿制药共性问题.md]]` 三个 source 笔记对应的数据源；如官网有新增，则增量抓取并写入。

增量维护建议流程：

1. 读取 3 个 source 笔记 frontmatter 中的 `path`，确定对应 `probleType`。
2. 通过浏览器页面上下文调用 `/xxgk/getCommonQuestionList`。
3. 优先抓取每个 `probleType` 的第一页；如第一页最旧记录仍未命中本地已有记录，再继续抓取下一页。
4. 本地去重判断优先使用：
   - `probleContent`（问题）
   - `publishTime`（发布日期）
5. 若官网记录的“问题 + 发布日期”在本地表格中不存在，则视为新增记录。
6. 新增记录按官网返回顺序插入表格顶部，即表头下方，使 source 笔记保持官网默认的倒序时间顺序。
7. 若“问题 + 发布日期”已存在，但 `answerContent` 与本地“解答”不一致，应视为官网内容修订：
   - 优先更新对应行的解答；
   - 如修订范围较大或匹配不确定，应回退为全量刷新该 source 笔记。
8. 如果增量匹配结果异常，例如本地第一条在官网前几页都找不到、官网总数明显少于本地行数、或页面栏目结构变化，应停止机械增量，改为全量抓取并人工核对。

注意：

- source 表格仍不增加「序号」列。
- 不建议在 source 表格中额外加入 `idCODE` 列；如需保留接口主键，可临时写入 `_` 目录下的过程缓存。
- `_` 目录中的抓取缓存属于过程数据，不作为最终知识库正文内容。

## 参考 Playwright 页面上下文逻辑

下面是方法示意，不要求逐字复用；实际执行时可按环境调整 Chrome 路径、等待时间等。

```js
async function fetchCommonQuestions(page, type) {
  return await page.evaluate(async (type) => {
    function req(pageNum) {
      const params = {
        pageSize: 20,
        pageNum,
        probleContent: "",
        probleType: type
      };

      return new Promise((resolve, reject) => {
        try {
          myAjax("/xxgk/getCommonQuestionList", params, "post")
            .done(res => resolve(res))
            .fail(err => reject(String(err)));
        } catch (e) {
          reject(e.message || String(e));
        }
      });
    }

    const first = await req(1);
    const total = first.data.total || 0;
    const pages = first.data.pages || 0;
    const records = [...(first.data.records || [])];

    for (let p = 2; p <= pages; p++) {
      const r = await req(p);
      records.push(...(r.data.records || []));
    }

    return { total, pages, records };
  }, type);
}
```

## 最近一次执行记录

2026-07-09 执行时：

- `probleType=1`：267 条，写入 `[[wiki/_source/CDE/CDE_共性问题-常见一般性技术问题.md]]`
- `probleType=2`：96 条，写入 `[[wiki/_source/CDE/CDE_共性问题-受理共性问题.md]]`
- `probleType=3`：0 条，未建立单独 source 笔记
- `probleType=4`：39 条，写入 `[[wiki/_source/CDE/CDE_共性问题-化学仿制药共性问题.md]]`

临时抓取缓存曾保存为 `[[_/cde_common_questions_fetched.json]]`，该文件属于过程数据，不作为长期知识内容。

