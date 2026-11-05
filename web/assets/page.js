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
    const backlinks = data.backlinks || [];
    const backlinksSection = backlinks.length
      ? `<section class="backlinks"><h2>反向链接</h2>${linkList(backlinks)}</section>`
      : "";
    const home = data.kind === "home" ? " home-page" : "";
    const wikiPath = String(data.path || "");
    const reviewHref = data.reviewHref || (data.kind === "wiki" && /(^|\/)\d+-\d{4}\.md$/.test(wikiPath)
      ? "/review/knowledge?path=" + encodeURIComponent(wikiPath)
      : "");
    const reviewAction = reviewHref
      ? `<div class="document-actions"><a href="${e(safeHref(reviewHref))}">调整分类</a></div>`
      : "";
    const breadcrumbBlock = reviewAction
      ? `<div class="document-top-row"><div class="breadcrumbs">${crumbs}</div>${reviewAction}</div>`
      : `<div class="breadcrumbs">${crumbs}</div>`;
    // The home page's breadcrumb already says 首页, so it gets no HOME chip.
    const kind = data.kind === "home" ? "" : `<span class="kind">${e(String(data.kind).toUpperCase())}</span>`;
    return `<header><button id="menu-toggle" aria-label="打开目录">☰</button><img class="brand-logo" src="${e(root)}assets/logo.png" alt="臣邦医药" width="150" height="28"><strong>DEK 知识库</strong>`
      + `<div class="search-wrap"><div class="search-box"><input type="search" id="global-search" data-index="${e(root)}assets/search-index.json" placeholder="输入关键词…" autocomplete="off"><button type="button" id="search-button" disabled>加载中…</button></div><span id="search-status" aria-live="polite"></span><div id="search-results"></div></div>`
      + `<div class="user-menu" data-auth-me="${e(root)}auth/me"><a class="review-entry" href="/review/">知识审核</a><span id="user-name">正在读取…</span><a href="${e(root)}auth/logout">退出</a></div>`
      + `<button id="theme-toggle" aria-label="切换主题">◐</button></header>`
      + `<aside class="sidebar"><nav id="nav-tree" data-manifest="${e(root)}manifest.json" data-current="${e(data.path)}"></nav></aside>`
      + `<main class="document${home}">${breadcrumbBlock}${kind}<h1>${e(data.title)}</h1><div class="badges">${badges}</div>${propertiesHtml(data)}<article>${bodyHtml || ""}</article>${backlinksSection}</main>`
      + `<button type="button" class="chat-launcher" id="chat-launcher" aria-label="打开 Kbot AI 助手" aria-controls="chat-panel" aria-expanded="false"><span class="chat-launcher-icon" aria-hidden="true"><svg viewBox="0 0 24 24" focusable="false"><path d="M12 2l1.15 3.1a5.8 5.8 0 0 0 3.45 3.45L19.7 9.7l-3.1 1.15a5.8 5.8 0 0 0-3.45 3.45L12 17.4l-1.15-3.1a5.8 5.8 0 0 0-3.45-3.45L4.3 9.7l3.1-1.15a5.8 5.8 0 0 0 3.45-3.45L12 2Z"/><path d="M19 15l.55 1.45A2.65 2.65 0 0 0 21.1 18l1.45.55-1.45.55a2.65 2.65 0 0 0-1.55 1.55L19 22.1l-.55-1.45a2.65 2.65 0 0 0-1.55-1.55l-1.45-.55L16.9 18a2.65 2.65 0 0 0 1.55-1.55L19 15Z"/></svg></span><span class="chat-launcher-label">AI 助手</span></button>`
      + `<section class="chat-panel" id="chat-panel" aria-labelledby="chat-title" hidden><header class="chat-head"><div class="chat-identity"><span class="chat-head-icon" aria-hidden="true"><svg viewBox="0 0 24 24" focusable="false"><path d="M12 2l1.15 3.1a5.8 5.8 0 0 0 3.45 3.45L19.7 9.7l-3.1 1.15a5.8 5.8 0 0 0-3.45 3.45L12 17.4l-1.15-3.1a5.8 5.8 0 0 0-3.45-3.45L4.3 9.7l3.1-1.15a5.8 5.8 0 0 0 3.45-3.45L12 2Z"/></svg></span><div><strong id="chat-title">DEK AI 助手</strong><small>基于已审核知识内容回答 · 共享资源自动参与</small></div></div><button type="button" id="chat-close" aria-label="关闭 DEK AI 助手"><svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="m6 6 12 12M18 6 6 18"/></svg></button></header><div class="chat-messages" id="chat-messages" aria-live="polite"><div class="chat-message assistant"><p>您好，我是 Kbot，DEK 法规知识库 AI 助手。我可以基于已审核、已发布的知识内容，协助您查询药品注册、审评审批、注册检验与核查、药学、临床、非临床及上市后变更等问题。请直接告诉我您想了解的事项。</p></div></div><form class="chat-form" id="chat-form"><div class="chat-documents" id="chat-documents" hidden><div class="chat-documents-head"><strong>共享资源</strong><small>团队可见 · 自动参与 AI 问答 · 同等权重</small></div><div class="chat-document-list" id="chat-document-list"></div></div><label class="sr-only" for="chat-input">向 Kbot 提问</label><div class="chat-composer"><button type="button" class="chat-upload" id="chat-upload" aria-label="上传共享资源" title="上传共享资源" hidden><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 16V4m0 0L7 9m5-5 5 5M5 15v4h14v-4"/></svg></button><input type="file" id="chat-file" accept=".pdf,.docx,.txt,.md" hidden><textarea id="chat-input" maxlength="2000" rows="1" placeholder="询问已审核知识库或共享资源…"></textarea><button type="submit" id="chat-send" aria-label="发送问题" disabled><svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M5 12h14M13 6l6 6-6 6"/></svg></button></div><div class="chat-input-hint" id="chat-input-hint">Enter 发送 · Shift+Enter 换行</div></form></section>`;
  }

  // Review pages use their own server-rendered chrome. Reuse the exact same
  // assistant markup from pageHtml instead of maintaining a second Kbot UI.
  function assistantHtml() {
    const rendered = pageHtml({ root: "", kind: "home", crumbs: [], tags: [], title: "", path: "" }, "");
    const start = rendered.indexOf('<button type="button" class="chat-launcher"');
    return start < 0 ? "" : rendered.slice(start);
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
    if (!document) return;
    if (!dataElement || !body) {
      if (!document.getElementById("chat-launcher")) document.body.insertAdjacentHTML("beforeend", assistantHtml());
      return;
    }
    let data;
    try { data = JSON.parse(dataElement.textContent); } catch (error) { return; }
    document.body.insertAdjacentHTML("afterbegin", pageHtml(data, body.innerHTML));
    tidyOverviewTables(document);
    document.getElementById("page-loading")?.remove();
  }

  return { pageHtml, assistantHtml, mount, escapeText, overviewProject };
});
