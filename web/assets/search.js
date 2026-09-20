(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.DEKSearch = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  function normalize(value) {
    return String(value || "")
      .normalize("NFKC")
      .toLowerCase()
      .replace(/[\p{P}\p{S}]+/gu, " ")
      .replace(/\s+/g, " ")
      .trim();
  }

  function compact(value) {
    return normalize(value).replace(/\s/g, "");
  }

  function subsequenceScore(needle, haystack) {
    if (!needle || !haystack || needle.length > haystack.length) return 0;
    let at = 0;
    let first = -1;
    let last = -1;
    let gaps = 0;
    for (const character of needle) {
      const found = haystack.indexOf(character, at);
      if (found < 0) return 0;
      if (first < 0) first = found;
      if (last >= 0) gaps += found - last - 1;
      last = found;
      at = found + 1;
    }
    return Math.max(0.35, 0.82 - gaps / Math.max(needle.length * 4, 1) - first / Math.max(haystack.length * 4, 1));
  }

  function substringEditDistance(needle, haystack) {
    if (!needle || !haystack) return Infinity;
    let previous = new Array(haystack.length + 1).fill(0);
    for (let row = 1; row <= needle.length; row += 1) {
      const current = [row];
      for (let column = 1; column <= haystack.length; column += 1) {
        current[column] = Math.min(
          previous[column] + 1,
          current[column - 1] + 1,
          previous[column - 1] + (needle[row - 1] === haystack[column - 1] ? 0 : 1),
        );
      }
      previous = current;
    }
    return Math.min(...previous);
  }

  function termScore(term, value) {
    const query = compact(term);
    const target = compact(value);
    if (!query || !target) return 0;
    const position = target.indexOf(query);
    if (position >= 0) return 1 - Math.min(position / Math.max(target.length, 1), 0.2);
    if (query.length >= 4) {
      const subsequence = subsequenceScore(query, target);
      const distance = substringEditDistance(query, target.slice(0, 2000));
      const tolerance = Math.min(3, Math.max(1, Math.floor(query.length / 4)));
      const approximate = distance <= tolerance ? 0.78 - (distance / query.length) : 0;
      return Math.max(subsequence, approximate);
    }
    return 0;
  }

  function scoreDocument(document, query) {
    const terms = normalize(query).split(" ").filter(Boolean);
    if (!terms.length) return 0;
    const fields = [
      [document.title, 5],
      [(document.tags || []).join(" "), 4],
      [document.path, 2],
      [document.text, 1],
    ];
    let total = 0;
    for (const term of terms) {
      let best = 0;
      for (const [value, weight] of fields) best = Math.max(best, termScore(term, value) * weight);
      if (!best) return 0;
      total += best;
    }
    return total / terms.length;
  }

  function searchDocuments(documents, query, limit = 20) {
    return documents
      .map((document, index) => ({ document, index, score: scoreDocument(document, query) }))
      .filter(item => item.score > 0)
      .sort((left, right) => right.score - left.score || left.index - right.index)
      .slice(0, limit)
      .map(item => item.document);
  }

  function queryTerms(query) {
    return normalize(query).split(" ").filter(Boolean);
  }

  function escapeText(value) {
    return String(value).replace(/[&<>"']/g, character => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    })[character]);
  }

  // [start, end) ranges of `text` where a query term occurs, merged. Matching is
  // on the lower-cased text itself so the positions stay valid for the original.
  function matchRanges(text, query) {
    const lowered = String(text).toLowerCase();
    if (lowered.length !== String(text).length) return [];
    const ranges = [];
    for (const term of queryTerms(query)) {
      for (let at = lowered.indexOf(term); at >= 0; at = lowered.indexOf(term, at + term.length)) {
        ranges.push([at, at + term.length]);
      }
    }
    ranges.sort((left, right) => left[0] - right[0] || left[1] - right[1]);
    const merged = [];
    for (const range of ranges) {
      const last = merged[merged.length - 1];
      if (last && range[0] <= last[1]) last[1] = Math.max(last[1], range[1]);
      else merged.push([...range]);
    }
    return merged;
  }

  // `text` as HTML-safe markup with the query's words wrapped in <mark>.
  function highlight(text, query) {
    const source = String(text);
    let html = "";
    let cursor = 0;
    for (const [start, end] of matchRanges(source, query)) {
      html += escapeText(source.slice(cursor, start)) + "<mark>" + escapeText(source.slice(start, end)) + "</mark>";
      cursor = end;
    }
    return html + escapeText(source.slice(cursor));
  }

  function resultSnippet(document, query, length = 140) {
    const text = String(document.text || "").replace(/\s+/g, " ").trim();
    if (!text) return "";
    let position = -1;
    const first = matchRanges(text, query)[0];
    if (first) position = first[0];
    else {
      // No literal hit (a fuzzy match): fall back to the normalised position.
      const normalizedText = normalize(text);
      for (const term of queryTerms(query)) {
        position = normalizedText.indexOf(term);
        if (position >= 0) break;
      }
    }
    const start = Math.max(0, position < 0 ? 0 : position - 35);
    return `${start ? "…" : ""}${text.slice(start, start + length)}${start + length < text.length ? "…" : ""}`;
  }

  function resultUrl(document, indexUrl) {
    const siteRoot = new URL("../", indexUrl);
    return new URL(document.url, siteRoot).href;
  }

  function isoDay(value) {
    const parsed = value instanceof Date ? value : new Date(value);
    const year = parsed.getFullYear();
    const month = String(parsed.getMonth() + 1).padStart(2, "0");
    const day = String(parsed.getDate()).padStart(2, "0");
    return `${year}-${month}-${day}`;
  }

  function recentDocuments(documents, days, asOf) {
    const end = isoDay(asOf || new Date());
    const endMs = new Date(`${end}T00:00:00`).getTime();
    const start = isoDay(new Date(endMs - (days - 1) * 86400000));
    return documents
      .filter(document => {
        const day = String(document.date || "").slice(0, 10);
        return day >= start && day <= end;
      })
      .sort((left, right) => String(right.date || "").localeCompare(String(left.date || "")));
  }

  function recentDocumentsInRange(documents, start, end) {
    return documents
      .filter(document => {
        const day = String(document.date || "").slice(0, 10);
        if (!day) return false;
        if (start && day < start) return false;
        if (end && day > end) return false;
        return true;
      })
      .sort((left, right) => String(right.date || "").localeCompare(String(left.date || "")));
  }

  // How many documents under `prefix` ("wiki", "wiki/16_x") fall in the date range.
  // With no bounds every document counts, dated or not, so the figure is the total.
  function countInRange(documents, prefix, start, end) {
    const inside = prefix + "/";
    let count = 0;
    for (const document of documents) {
      if (!(document.path === prefix || String(document.path).startsWith(inside))) continue;
      if (start || end) {
        const day = String(document.date || "").slice(0, 10);
        if (!day || (start && day < start) || (end && day > end)) continue;
      }
      count += 1;
    }
    return count;
  }

  // ---- Home page ----------------------------------------------------------
  // The release only carries the directory data (manifest.json); the page itself
  // is built here, from installed code, so changing it needs a deploy, not a release.
  const HOME_SECTION_TITLES = { wiki: "Wiki · 正式知识", source: "Source · 来源材料" };

  function firstDocumentUrl(node) {
    let target = node;
    while (target.type === "directory" && target.children && target.children.length) target = target.children[0];
    return target.url || "#";
  }

  function countHtml(path, total, className, isSection) {
    const text = isSection ? String(total) : `共 ${total} 篇`;
    return `<span class="${className}" data-count-path="${escapeText(path)}" data-total="${total}">${text}</span>`;
  }

  function homeHtml(tree, options) {
    const resolve = (options && options.resolve) || (url => url);
    const indexPath = (options && options.indexPath) || "assets/search-index.json";
    const sections = tree.map(root => {
      const cards = root.children.map(child => {
        const name = escapeText(child.name);
        const total = child.type === "directory" ? child.count : 1;
        return `<div class="folder-card"><a class="folder-card-link" href="${escapeText(resolve(firstDocumentUrl(child)))}" title="${name}"><strong>${name}</strong>${countHtml(child.path, total, "card-count", false)}</a></div>`;
      }).join("");
      const title = HOME_SECTION_TITLES[root.path] || escapeText(root.name);
      return `<section class="home-section"><h2>${title}${countHtml(root.path, root.count, "section-count", true)}</h2><div class="folder-grid">${cards}</div><p class="section-empty" hidden>该时段内暂无新增内容</p></section>`;
    }).join("");
    const filters = '<section class="recent-filters"><h2>信息速览</h2>'
      + '<div class="recent-tabs" role="group" aria-label="按天数快速筛选">'
      + '<button type="button" class="recent-tab" data-days="7">7天</button>'
      + '<button type="button" class="recent-tab" data-days="30">30天</button>'
      + '<button type="button" class="recent-tab" data-days="90">90天</button>'
      + '<button type="button" class="recent-tab" data-days="undated">无日期</button>'
      + '<button type="button" class="recent-tab active" data-days="0">全部</button>'
      + '<button type="button" class="recent-tab" id="recent-custom" aria-controls="recent-range" aria-expanded="false">自定义</button>'
      + '</div>'
      + '<div class="recent-range" id="recent-range" hidden>'
      + '<label>开始日期 <input type="date" id="recent-start"></label>'
      + '<label>结束日期 <input type="date" id="recent-end"></label>'
      + '</div>'
      + '<p class="recent-summary" id="recent-summary" aria-live="polite"></p>'
      + '</section>';
    const recent = '<section class="recent-section"><div class="recent-table">'
      + '<div class="recent-head"><span>日期</span><span>内容</span><span>路径</span></div>'
      + `<div id="recent-list" class="recent-list" data-index="${escapeText(indexPath)}">正在加载信息速览…</div></div>`
      + '<div class="list-footer" id="recent-footer" hidden></div></section>';
    return filters + sections + recent;
  }

  // ---- Paging (the same pager and page-size box the review list uses) --------
  const PAGE_SIZE = 15;
  const MIN_PAGE_SIZE = 5;
  const MAX_PAGE_SIZE = 100;

  // An empty or unreadable box means the default; anything else is held to 5-100.
  function clampPageSize(value) {
    const parsed = parseInt(value, 10);
    return Number.isFinite(parsed) ? Math.min(MAX_PAGE_SIZE, Math.max(MIN_PAGE_SIZE, parsed)) : PAGE_SIZE;
  }

  // Page numbers to show: the first and last page, and the current one with 5 on
  // each side (at most 11 in a run), `null` marking a gap between runs.
  const PAGE_REACH = 5;
  function pageWindow(page, pages) {
    const shown = new Set([1, pages]);
    for (let number = page - PAGE_REACH; number <= page + PAGE_REACH; number += 1) {
      if (number >= 1 && number <= pages) shown.add(number);
    }
    const window = [];
    let previous = 0;
    for (const number of [...shown].sort((a, b) => a - b)) {
      if (number - previous > 1) window.push(null);
      window.push(number);
      previous = number;
    }
    return window;
  }

  function pagerHtml(page, pages) {
    const link = (number, label) => `<a href="#" data-page="${number}">${label}</a>`;
    const off = label => `<span class="disabled">${label}</span>`;
    const parts = [page > 1 ? link(1, "首页") : off("首页"), page > 1 ? link(page - 1, "上一页") : off("上一页")];
    for (const number of pageWindow(page, pages)) {
      if (number === null) parts.push('<span class="gap">…</span>');
      else if (number === page) parts.push(`<span class="current" aria-current="page">${number}</span>`);
      else parts.push(link(number, number));
    }
    parts.push(page < pages ? link(page + 1, "下一页") : off("下一页"), page < pages ? link(pages, "末页") : off("末页"));
    parts.push(`<span class="page-info">第 ${page}/${pages} 页</span>`);
    return `<nav class="pager" aria-label="分页">${parts.join("")}</nav>`;
  }

  function pageJumpHtml(pages) {
    // novalidate: a number past either end is taken to the last / first page, not refused by the browser.
    return `<form class="page-jump" novalidate>跳转到第<input type="number" name="page_jump" min="1" max="${pages}" step="1" inputmode="numeric" aria-label="跳转到页码">页<button type="submit">跳转</button></form>`;
  }

  function pageSizeHtml(size) {
    return `<form class="page-size">每页<input type="number" name="page_size" min="${MIN_PAGE_SIZE}" max="${MAX_PAGE_SIZE}" step="1" value="${size}" inputmode="numeric" aria-label="每页条数">条</form>`;
  }

  // ---- The list's place in the address bar ---------------------------------
  // ?page=3&range=90&size=40 - the page, and what it is a page of, so a refresh,
  // the back button or a shared link lands on the same rows.
  const RANGES = ["0", "7", "30", "90", "undated", "custom"];
  const ISO_DAY = /^\d{4}-\d{2}-\d{2}$/;

  function listStateFromQuery(search) {
    const query = new URLSearchParams(search || "");
    const range = RANGES.includes(query.get("range")) ? query.get("range") : "0";
    const day = name => (ISO_DAY.test(query.get(name) || "") ? query.get(name) : "");
    const page = parseInt(query.get("page"), 10);
    return {
      range,
      start: range === "custom" ? day("start") : "",
      end: range === "custom" ? day("end") : "",
      page: Number.isFinite(page) && page > 0 ? page : 1,
      size: query.has("size") ? clampPageSize(query.get("size")) : PAGE_SIZE,
    };
  }

  // The query string for a state, leaving out whatever is the default ("" when all are).
  function listStateToQuery(state) {
    const query = new URLSearchParams();
    if (state.page > 1) query.set("page", String(state.page));
    if (state.range && state.range !== "0") query.set("range", state.range);
    if (state.range === "custom") {
      if (ISO_DAY.test(state.start || "")) query.set("start", state.start);
      if (ISO_DAY.test(state.end || "")) query.set("end", state.end);
    }
    if (state.size && state.size !== PAGE_SIZE) query.set("size", String(state.size));
    const text = query.toString();
    return text ? "?" + text : "";
  }

  // Documents with no date at all (directory pages, notes that carry no date), by path.
  function undatedDocuments(documents) {
    return documents.filter(document => !String(document.date || "").slice(0, 10))
      .sort((left, right) => String(left.path).localeCompare(String(right.path)));
  }

  function countUndated(documents, prefix) {
    const inside = prefix + "/";
    let count = 0;
    for (const document of undatedDocuments(documents)) {
      if (document.path === prefix || String(document.path).startsWith(inside)) count += 1;
    }
    return count;
  }

  return { normalize, scoreDocument, searchDocuments, resultSnippet, highlight, homeHtml, undatedDocuments, countUndated, PAGE_SIZE, clampPageSize, pageWindow, pagerHtml, pageJumpHtml, pageSizeHtml, listStateFromQuery, listStateToQuery, resultUrl, recentDocuments, recentDocumentsInRange, countInRange };
});
