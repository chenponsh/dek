(function (root, factory) {
  const api = factory(root);
  if (typeof module === "object" && module.exports) module.exports = api;
  else {
    root.DEKPage = api;
    api.mount(root.document);
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  // A published page is only its content (the <template id="page-body">) and a
  // JSON description of it (<script id="page-data">): title, tags, properties,
  // breadcrumbs, links, contents list. Everything around the content - header,
  // sidebar, headings, panels - is drawn here, from installed code, so changing
  // how pages look is a deploy, not a content release.

  function escapeText(value) {
    return String(value).replace(/[&<>"']/g, character => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    })[character]);
  }

  const e = escapeText;

  // Links in the data are relative paths written by the build; anything that is
  // not a plain relative path or an http(s) address is dropped.
  function safeHref(value) {
    const text = String(value || "");
    if (/^https?:\/\//i.test(text)) return text;
    if (/^[a-z][a-z0-9+.-]*:/i.test(text) || text.startsWith("//")) return "";
    return text;
  }

  function linkList(items) {
    return items.map(item => `<a href="${e(safeHref(item.href))}">${e(item.title)}</a>`).join("");
  }

  function valueHtml(value) {
    if (value && Array.isArray(value.list)) {
      return '<span class="property-list">' + value.list.map(item => `<span>${valueHtml(item)}</span>`).join("") + "</span>";
    }
    return ((value && value.parts) || []).map(part => {
      if (part.href) return `<a class="wikilink" href="${e(safeHref(part.href))}">${e(part.t)}</a>`;
      if (part.broken) return `<span class="broken-link">${e(part.t)}</span>`;
      return e(part.t).replace(/\n/g, "<br>");
    }).join("");
  }

  function externalLinksHtml(data) {
    const urls = (data.externalLinks || []).map(safeHref).filter(url => /^https?:\/\//i.test(url));
    return urls.map(url => `<a class="external" href="${e(url)}" rel="noreferrer" target="_blank">打开来源链接 ↗</a>`).join(" ");
  }

  // The 笔记信息 panel: the note's own fields, the link back to the original page (when there
  // is one) and the note's path last.
  function propertiesHtml(data) {
    const props = data.props;
    if (!props || !props.length) return "";
    const link = externalLinksHtml(data);
    const rows = props.map(row => {
      const cell = row.code !== undefined ? `<code>${e(row.code)}</code>` : valueHtml(row.value);
      const before = row.code !== undefined && link ? `<div class="property-row"><dt>来源链接</dt><dd>${link}</dd></div>` : "";
      return `${before}<div class="property-row"><dt>${e(row.label)}</dt><dd>${cell}</dd></div>`;
    }).join("");
    return `<details class="note-properties" open><summary>笔记信息</summary><dl>${rows}</dl></details>`;
  }

  // The whole page around `bodyHtml` (already-sanitised content from the build).
  function pageHtml(data, bodyHtml) {
    const root = data.root || "";
    const crumbs = (data.crumbs || []).map(item => item.href ? `<a href="${e(safeHref(item.href))}">${e(item.text)}</a>` : e(item.text)).join(" / ");
    const badges = (data.tags || []).map(tag => `<span class="badge">#${e(tag)}</span>`).join("");
    const backlinks = (data.backlinks || []).length ? linkList(data.backlinks) : '<p class="muted">暂无反向链接</p>';
    const home = data.kind === "home" ? " home-page" : "";
    // The home page's breadcrumb already says 首页, so it gets no HOME chip.
    const kind = data.kind === "home" ? "" : `<span class="kind">${e(String(data.kind).toUpperCase())}</span>`;
    return `<header><button id="menu-toggle" aria-label="打开目录">☰</button><strong>DEK 知识库</strong>`
      + `<div class="search-wrap"><div class="search-box"><input type="search" id="global-search" data-index="${e(root)}assets/search-index.json" placeholder="输入关键词…" autocomplete="off"><button type="button" id="search-button" disabled>加载中…</button></div><span id="search-status" aria-live="polite"></span><div id="search-results"></div></div>`
      + `<div class="user-menu" data-auth-me="${e(root)}auth/me"><a class="review-entry" href="/review/">知识审核</a><span id="user-name">正在读取…</span><a href="${e(root)}auth/logout">退出</a></div>`
      + `<button id="theme-toggle" aria-label="切换主题">◐</button></header>`
      + `<aside class="sidebar"><nav id="nav-tree" data-manifest="${e(root)}manifest.json" data-current="${e(data.path)}"></nav></aside>`
      + `<main class="document${home}"><div class="breadcrumbs">${crumbs}</div>${kind}<h1>${e(data.title)}</h1><div class="badges">${badges}</div>${propertiesHtml(data)}<article>${bodyHtml || ""}</article><section class="backlinks"><h2>反向链接</h2>${backlinks}</section></main>`;
  }

  // A folder's overview table lists its entries as 项目 | 问题 | ...; 项目 should be the entry's
  // file name (0101-0001), not its question again. Pages built before the generator did this get
  // it here; for pages that already say it, this changes nothing.
  function overviewProject(href) {
    const file = String(href || "").split("#")[0].split("?")[0].split("/").pop().replace(/\.html$/i, "");
    try { return decodeURIComponent(file); } catch (error) { return file; }
  }

  function tidyOverviewTables(document) {
    document.querySelectorAll("article table").forEach(table => {
      const heads = [...table.querySelectorAll("thead th")].map(cell => cell.textContent.trim());
      if (heads[0] !== "项目" || heads[1] !== "问题") return;
      table.querySelectorAll("tbody tr").forEach(row => {
        const link = row.children[0]?.querySelector("a");
        const name = link && overviewProject(link.getAttribute("href"));
        if (name && link.textContent !== name) link.textContent = name;
      });
    });
  }

  function mount(document) {
    const dataElement = document && document.getElementById("page-data");
    const body = document && document.getElementById("page-body");
    if (!dataElement || !body) return;
    let data;
    try { data = JSON.parse(dataElement.textContent); } catch (error) { return; }
    document.body.insertAdjacentHTML("afterbegin", pageHtml(data, body.innerHTML));
    tidyOverviewTables(document);
    document.getElementById("page-loading")?.remove();
  }

  return { pageHtml, mount, escapeText, overviewProject };
});
