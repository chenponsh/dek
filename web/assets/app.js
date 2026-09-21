(() => {
  // Column widths a user drags to: 48px at least, and whatever was stored must still be sane.
  const MIN_COLUMN_WIDTH = 48;
  const MAX_COLUMN_WIDTH = 2000;
  const clampColumnWidth = value => Math.min(MAX_COLUMN_WIDTH, Math.max(MIN_COLUMN_WIDTH, Math.round(Number(value)) || MIN_COLUMN_WIDTH));
  function readStoredWidths(text, count) {
    try {
      const widths = JSON.parse(text);
      if (Array.isArray(widths) && widths.length === count && widths.every(n => Number.isFinite(n) && n >= MIN_COLUMN_WIDTH && n <= MAX_COLUMN_WIDTH)) return widths;
    } catch (error) { /* nothing usable stored */ }
    return null;
  }
  if (typeof document === "undefined") {
    if (typeof module === "object") module.exports = { clampColumnWidth, readStoredWidths, MIN_COLUMN_WIDTH, MAX_COLUMN_WIDTH };
    return;
  }

  const root = document.documentElement;
  const savedTheme = localStorage.getItem("dek-theme");
  if (savedTheme) root.dataset.theme = savedTheme;

  document.querySelector("#theme-toggle")?.addEventListener("click", () => {
    root.dataset.theme = root.dataset.theme === "dark" ? "light" : "dark";
    localStorage.setItem("dek-theme", root.dataset.theme);
  });

  if (document.querySelector(".recent-filters, #home-app")) document.querySelector("main.document")?.classList.add("home-page");

  const sidebar = document.querySelector(".sidebar");
  document.querySelector("#menu-toggle")?.addEventListener("click", () => sidebar?.classList.toggle("open"));

  // Keep the candidate note's number and tags in step with the chosen path, the
  // way the server builds them for a suggested one (only those three keys change).
  function syncCandidateToPath(box, path) {
    const parts = /^wiki\/(.+)\/([^/]+)\.md$/.exec(path);
    if (!box || !parts) return;
    const segments = parts[1].split("/");
    const digits = (/-(\d+)$/.exec(parts[2]) || ["", ""])[1];
    const number = digits.replace(/^0+/, "") || digits;
    const tags = segments.map((_, index) => segments.slice(0, index + 1).join("/"));
    const tagPages = tags.map(tag => `  - "[[${tag.split("/").pop()}]]"`);
    const tagList = tags.map(tag => `  - "${tag}"`);
    const text = box.value.replace(/\r\n/g, "\n");
    const front = /^---\n([\s\S]*?)\n---(\n|$)/.exec(text);
    if (!front) return;
    const lines = front[1].split("\n");
    const out = [];
    for (let i = 0; i < lines.length;) {
      if (/^no:/.test(lines[i])) { out.push(`no: ${number}`); i += 1; }
      else if (/^(tag_pages|tags):/.test(lines[i])) {
        out.push(lines[i].startsWith("tag_pages") ? "tag_pages:" : "tags:", ...(lines[i].startsWith("tag_pages") ? tagPages : tagList));
        i += 1;
        while (i < lines.length && /^\s+- /.test(lines[i])) i += 1;
      } else { out.push(lines[i]); i += 1; }
    }
    box.value = "---\n" + out.join("\n") + "\n---" + front[2] + text.slice(front[0].length);
  }

  document.querySelectorAll(".wiki-path-input").forEach(input => {
    const list = input.nextElementSibling;
    if (!list) return;
    let options = [];
    try { options = JSON.parse(input.dataset.options || "[]"); } catch (_) { options = []; }
    let active = -1;
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-autocomplete", "list");
    input.setAttribute("aria-expanded", "false");
    const items = () => [...list.querySelectorAll(".combo-option")];
    const setActive = index => {
      const rows = items();
      active = rows.length ? (index + rows.length) % rows.length : -1;
      rows.forEach((row, i) => row.classList.toggle("active", i === active));
      if (active >= 0) {
        rows[active].scrollIntoView({ block: "nearest" });
        input.setAttribute("aria-activedescendant", rows[active].id);
      } else input.removeAttribute("aria-activedescendant");
    };
    const close = () => {
      list.classList.remove("open");
      input.setAttribute("aria-expanded", "false");
      setActive(-1);
    };
    const candidateBox = () => input.closest("form")?.querySelector('[name="candidate_markdown"]');
    const choose = row => { input.value = row.dataset.path; syncCandidateToPath(candidateBox(), input.value); close(); };
    const render = () => {
      // Matching is by folder name only; each row shows the full suggested path.
      const query = input.value.trim().toLowerCase();
      const matches = query ? options.filter(([label]) => label.toLowerCase().includes(query)) : options;
      list.innerHTML = matches.slice(0, 30).map(([label, path], i) =>
        `<div class="combo-option" role="option" id="combo-opt-${i}" data-path="${escapeHtml(path)}" title="${escapeHtml(path)}"><span class="combo-path">${escapeHtml(path)}</span></div>`
      ).join("");
      list.classList.toggle("open", matches.length > 0);
      input.setAttribute("aria-expanded", matches.length > 0 ? "true" : "false");
      setActive(-1);
    };
    input.addEventListener("focus", render);
    // Typing a full path (say, changing the number at its end) updates the candidate too;
    // half-typed text that is not a wiki path leaves it alone.
    input.addEventListener("input", () => { render(); syncCandidateToPath(candidateBox(), input.value.trim()); });
    input.addEventListener("change", () => syncCandidateToPath(candidateBox(), input.value.trim()));
    input.addEventListener("blur", () => setTimeout(close, 150));
    input.addEventListener("keydown", event => {
      const open = list.classList.contains("open");
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        if (!open) render();
        setActive(active + (event.key === "ArrowDown" ? 1 : -1));
      } else if (event.key === "Enter") {
        // Never let Enter fall through to the form's default button (批准).
        event.preventDefault();
        const rows = items();
        if (open && active >= 0 && rows[active]) choose(rows[active]);
      } else if (event.key === "Escape" && open) {
        event.preventDefault();
        close();
      }
    });
    list.addEventListener("mousedown", event => {
      const option = event.target.closest(".combo-option");
      if (!option) return;
      event.preventDefault();
      choose(option);
    });
    // The system's suggested folders sit under the box as buttons.
    input.closest("form")?.addEventListener("click", event => {
      const chip = event.target.closest(".suggestion-chip");
      if (!chip) return;
      input.value = chip.dataset.path;
      syncCandidateToPath(candidateBox(), chip.dataset.path);
    });
    // A rough with no wiki_target suggestion of its own prefills this blank;
    // clicking 批准 without picking a folder first used to 400 server-side
    // with no visible reason (the response body deliberately never carries
    // the real one). Catch it here instead, before the round trip.
    const form = input.closest("form");
    if (form) {
      form.addEventListener("submit", event => {
        if (event.submitter?.value !== "approve") return;
        const candidate = form.querySelector('[name="candidate_markdown"]');
        if (!input.value.trim() || !candidate?.value.trim()) {
          event.preventDefault();
          alert("批准前请先在「Wiki 路径」栏搜索并选择一个具体分类文件夹。");
          input.focus();
        }
      });
    }
  });

  document.querySelectorAll("tr[data-href]").forEach(row => {
    row.addEventListener("click", event => {
      if (event.target.closest("a, button, input, textarea, select")) return;
      const target = row.dataset.href;
      if (target) location.href = target;
    });
  });

  // Rows-per-page box on the review list: clamp to 5-100 (empty means 15) and
  // reload the list when the value changes.
  document.querySelectorAll("form.page-size").forEach(form => {
    const input = form.querySelector('input[name="page_size"]');
    if (!input) return;
    const normalise = () => {
      const parsed = parseInt(input.value, 10);
      input.value = Number.isFinite(parsed) ? Math.min(100, Math.max(5, parsed)) : 15;
    };
    form.addEventListener("submit", normalise);
    input.addEventListener("change", () => {
      normalise();
      if (input.value !== input.defaultValue) form.requestSubmit();
    });
  });

  const SIDEBAR_KEY = "dek-sidebar-width";
  const DEFAULT_SIDEBAR_W = 280;
  const MIN_SIDEBAR_W = 200;
  const drawerMode = window.matchMedia("(max-width: 760px)");
  const maxSidebarW = () => Math.max(MIN_SIDEBAR_W, Math.floor(window.innerWidth * 0.5));
  const clampSidebarW = value => Math.min(maxSidebarW(), Math.max(MIN_SIDEBAR_W, Math.round(value)));
  const storedSidebarW = () => {
    try { return Number(localStorage.getItem(SIDEBAR_KEY)); } catch (error) { return 0; }
  };
  const showSidebarW = () => {
    // The drawer layout on narrow screens always uses the default width.
    const saved = storedSidebarW();
    if (!drawerMode.matches && saved >= MIN_SIDEBAR_W) root.style.setProperty("--sidebar-w", clampSidebarW(saved) + "px");
    else root.style.removeProperty("--sidebar-w");
  };
  showSidebarW();
  window.addEventListener("resize", showSidebarW);

  if (sidebar) {
    // Earlier pages carry a handle inside the scrolling sidebar, where the
    // scrollbar covered it and it scrolled out of view; use one fixed handle instead.
    document.querySelectorAll(".sidebar .sidebar-resize-handle").forEach(old => old.remove());
    const handle = document.createElement("div");
    handle.className = "sidebar-resize-handle";
    handle.setAttribute("role", "separator");
    handle.setAttribute("aria-orientation", "vertical");
    handle.setAttribute("aria-label", "调整目录宽度（双击恢复默认）");
    document.body.appendChild(handle);
    handle.addEventListener("pointerdown", event => {
      if (event.button !== undefined && event.button !== 0) return;
      event.preventDefault();
      handle.classList.add("dragging");
      handle.setPointerCapture?.(event.pointerId);
      document.body.style.userSelect = "none";
      document.body.style.cursor = "col-resize";
      let width = clampSidebarW(event.clientX);
      const onMove = moveEvent => {
        width = clampSidebarW(moveEvent.clientX);
        root.style.setProperty("--sidebar-w", width + "px");
      };
      const onUp = () => {
        handle.classList.remove("dragging");
        document.body.style.userSelect = "";
        document.body.style.cursor = "";
        try { localStorage.setItem(SIDEBAR_KEY, String(width)); } catch (error) { /* storage unavailable */ }
        document.removeEventListener("pointermove", onMove);
        document.removeEventListener("pointerup", onUp);
        document.removeEventListener("pointercancel", onUp);
      };
      document.addEventListener("pointermove", onMove);
      document.addEventListener("pointerup", onUp);
      document.addEventListener("pointercancel", onUp);
    });
    handle.addEventListener("dblclick", () => {
      try { localStorage.removeItem(SIDEBAR_KEY); } catch (error) { /* storage unavailable */ }
      showSidebarW();
    });
  }

  // A posting form (the review page's decision and trigger buttons): once submitted, grey the
  // buttons out with a spinner and ignore further clicks, so a slow answer is never asked for twice.
  document.querySelectorAll("form[method=post]").forEach(form => {
    form.addEventListener("submit", event => {
      if (form.dataset.busy === "1") { event.preventDefault(); return; }
      form.dataset.busy = "1";
      const buttons = [...form.querySelectorAll("button[type=submit]")];
      buttons.forEach(button => { button.classList.add("is-busy"); button.setAttribute("aria-busy", "true"); });
      const label = event.submitter?.dataset.busyLabel;
      if (label) event.submitter.textContent = label;
      // disabled only after the browser has read the form: a disabled button is not submitted
      setTimeout(() => buttons.forEach(button => { button.disabled = true; }), 0);
    });
  });
  window.addEventListener("pageshow", event => { if (event.persisted && document.querySelector("form[data-busy='1']")) location.reload(); });

  const userMenu = document.querySelector(".user-menu");
  if (userMenu) {
    fetch(userMenu.dataset.authMe, { credentials: "same-origin", cache: "no-store" })
      .then(response => response.ok ? response.json() : Promise.reject())
      .then(data => { document.querySelector("#user-name").textContent = data.display_name || "登录信息不可用"; })
      .catch(() => { document.querySelector("#user-name").textContent = "登录信息不可用"; });
  }

  const sharedIndexCache = new Map();
  function loadSharedIndex(url) {
    const key = url.href;
    if (!sharedIndexCache.has(key)) {
      sharedIndexCache.set(key, fetch(url, { credentials: "same-origin", cache: "no-store" })
        .then(response => response.ok ? response.json() : Promise.reject())
        .catch(error => { sharedIndexCache.delete(key); throw error; }));
    }
    return sharedIndexCache.get(key);
  }

  const input = document.querySelector("#global-search");
  const searchButton = document.querySelector("#search-button");
  const searchStatus = document.querySelector("#search-status");
  const results = document.querySelector("#search-results");
  let docs = [];
  let indexReady = false;
  if (input) {
    const indexUrl = new URL(input.dataset.index, location.href);
    const loadIndex = () => {
      indexReady = false;
      searchButton.disabled = true;
      searchButton.textContent = "加载中…";
      searchStatus.textContent = "正在加载搜索索引…";
      return loadSharedIndex(indexUrl)
        .then(data => {
          docs = data;
          indexReady = true;
          searchButton.disabled = false;
          searchButton.textContent = "搜索";
          searchStatus.textContent = "";
          results.classList.remove("open");
        })
        .catch(() => {
          docs = [];
          searchButton.disabled = false;
          searchButton.textContent = "重试";
          searchStatus.textContent = "搜索索引加载失败";
          results.innerHTML = '<div class="result empty-result">搜索索引加载失败，请点击“重试”</div>';
          results.classList.add("open");
        });
    };
    loadIndex();
    const runSearch = () => {
      if (!indexReady) {
        loadIndex();
        return;
      }
      const query = input.value.trim();
      if (!query) {
        results.classList.remove("open");
        results.innerHTML = "";
        searchStatus.textContent = "请输入关键词";
        return;
      }
      searchStatus.textContent = "正在搜索…";
      const hits = DEKSearch.searchDocuments(docs, query, 30);
      results.innerHTML = hits.length
        ? `<div class="result-count">找到 ${hits.length} 条相关结果</div>${hits.map(doc => `<a class="result" href="${DEKSearch.resultUrl(doc, indexUrl)}"><strong>${DEKSearch.highlight(doc.title, query)}</strong><small>${doc.kind.toUpperCase()} · ${escapeHtml(doc.path)}</small><span class="result-snippet">${DEKSearch.highlight(DEKSearch.resultSnippet(doc, query), query)}</span></a>`).join("")}`
        : '<div class="result empty-result">没有找到相关内容，请尝试缩短关键词</div>';
      searchStatus.textContent = hits.length ? `搜索完成，共 ${hits.length} 条结果` : "搜索完成，没有结果";
      results.classList.add("open");
    };
    searchButton?.addEventListener("click", runSearch);
    input.addEventListener("keydown", event => {
      if (event.key === "Enter") {
        event.preventDefault();
        runSearch();
      }
    });
  }

  const nav = document.querySelector("#nav-tree");
  if (nav) {
    const manifestUrl = new URL(nav.dataset.manifest, location.href);
    const current = nav.dataset.current || "";
    let savedOpen = [];
    try { savedOpen = JSON.parse(localStorage.getItem("dek-tree-open") || "[]"); } catch (_) { savedOpen = []; }
    const openPaths = new Set(savedOpen);

    function renderNode(node) {
      if (node.type === "document") {
        const active = node.path === current ? " active" : "";
        const href = new URL(node.url, manifestUrl).href;
        return `<a role="treeitem" class="tree-link${active}" href="${href}" title="${escapeHtml(node.name)}"><span class="tree-file-icon">◇</span><span class="tree-label">${escapeHtml(node.name)}</span></a>`;
      }
      const isRoot = node.path === "wiki" || node.path === "source";
      const isCurrentAncestor = current === node.path || current.startsWith(node.path + "/");
      const open = isRoot || isCurrentAncestor || openPaths.has(node.path);
      const label = node.path === "wiki" ? "Wiki · 正式知识" : node.path === "source" ? "Source · 来源材料" : node.name;
      return `<details class="tree-folder${isRoot ? " tree-root" : ""}" data-path="${escapeHtml(node.path)}" ${open ? "open" : ""}><summary role="treeitem"><span class="tree-chevron">›</span><span class="tree-folder-icon">▱</span><span class="tree-label">${escapeHtml(label)}</span><span class="tree-count">${node.count}</span></summary><div role="group">${node.children.map(child => renderNode(child)).join("")}</div></details>`;
    }

    fetch(manifestUrl).then(response => response.json()).then(({ tree }) => {
      const homeActive = current === "首页.md" ? " active" : "";
      const homeHref = new URL("index.html", manifestUrl).href;
      const homeLink = `<a role="treeitem" class="tree-link home-link${homeActive}" href="${homeHref}"><span class="tree-file-icon">⌂</span><span class="tree-label">首页</span></a>`;
      // 来源列表 is the reviewers' page (the server refuses anyone else): first entry after 首页
      const sourcesLink = `<a role="treeitem" class="tree-link sources-link${current === "review:sources" ? " active" : ""}" href="/review/sources"><span class="tree-file-icon">▦</span><span class="tree-label">来源列表</span></a>`;
      nav.innerHTML = homeLink + sourcesLink + `<div role="tree" aria-label="知识库目录">${tree.map(node => renderNode(node)).join("")}</div>`;
      nav.querySelectorAll("details[data-path]").forEach(folder => {
        folder.addEventListener("toggle", () => {
          const path = folder.dataset.path;
          if (folder.open) openPaths.add(path); else openPaths.delete(path);
          localStorage.setItem("dek-tree-open", JSON.stringify([...openPaths]));
        });
      });
      nav.querySelector(".tree-link.active")?.scrollIntoView({ block: "center" });
    });
  }

  // ---- Resizable columns ----------------------------------------------------
  // Every table with a header row gets a drag handle on each header cell's right edge;
  // the home list (a grid, not a <table>) gets one on its 日期 and 内容 headers. Widths
  // are remembered per page and table; double-click a handle to go back to automatic.
  const storedWidths = key => { try { return localStorage.getItem(key); } catch (error) { return null; } };
  const storeWidths = (key, widths) => { try { localStorage.setItem(key, JSON.stringify(widths)); } catch (error) { /* storage unavailable */ } };
  const forgetWidths = key => { try { localStorage.removeItem(key); } catch (error) { /* storage unavailable */ } };

  function addHandle(cell, label, onDrag, onKey, onReset, onEnd) {
    const handle = document.createElement("span");
    handle.className = "col-resizer";
    handle.setAttribute("role", "separator");
    handle.setAttribute("aria-orientation", "vertical");
    handle.setAttribute("aria-label", label);
    handle.title = label;
    handle.tabIndex = 0;
    handle.addEventListener("pointerdown", event => {
      if (event.button !== undefined && event.button !== 0) return;
      event.preventDefault();
      event.stopPropagation();
      const startX = event.clientX;
      const begin = onDrag();
      handle.classList.add("dragging");
      handle.setPointerCapture?.(event.pointerId);
      document.body.style.userSelect = "none";
      document.body.style.cursor = "col-resize";
      const move = moveEvent => begin(moveEvent.clientX - startX);
      const up = () => {
        handle.classList.remove("dragging");
        document.body.style.userSelect = "";
        document.body.style.cursor = "";
        handle.removeEventListener("pointermove", move);
        handle.removeEventListener("pointerup", up);
        handle.removeEventListener("pointercancel", up);
        onEnd();
      };
      handle.addEventListener("pointermove", move);
      handle.addEventListener("pointerup", up);
      handle.addEventListener("pointercancel", up);
    });
    handle.addEventListener("click", event => event.stopPropagation());
    handle.addEventListener("dblclick", event => { event.stopPropagation(); onReset(); });
    handle.addEventListener("keydown", event => {
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
      event.preventDefault();
      onKey((event.key === "ArrowRight" ? 1 : -1) * (event.shiftKey ? 48 : 16));
    });
    cell.appendChild(handle);
  }

  function makeTableResizable(table, index) {
    const headRow = table.querySelector("thead tr");
    const heads = headRow ? [...headRow.children].filter(cell => cell.tagName === "TH") : [];
    if (heads.length < 2 || heads.some(cell => cell.colSpan > 1) || table.dataset.resizable) return;
    table.dataset.resizable = "1";
    if (!table.closest(".table-wrap, .table-scroll")) {
      const wrap = document.createElement("div");
      wrap.className = "table-scroll";
      table.parentNode.insertBefore(wrap, table);
      wrap.appendChild(table);
    }
    table.classList.add("resizable");
    let group = table.querySelector("colgroup");
    if (!group || group.children.length !== heads.length) {
      group?.remove();
      group = document.createElement("colgroup");
      heads.forEach(() => group.appendChild(document.createElement("col")));
      table.insertBefore(group, table.firstChild);
    }
    const cols = [...group.children];
    const key = `dek-cols:${location.pathname}:${index}:${heads.map(cell => cell.textContent.trim()).join("|")}`;
    let fixed = false;
    const widths = () => cols.map(col => parseFloat(col.style.width));
    const apply = list => {
      cols.forEach((col, i) => { col.style.width = list[i] + "px"; });
      table.style.tableLayout = "fixed";
      table.style.width = list.reduce((sum, width) => sum + width, 0) + "px";
      table.classList.add("fixed");
      fixed = true;
    };
    const freeze = () => { if (!fixed) apply(heads.map(cell => clampColumnWidth(cell.getBoundingClientRect().width))); };
    const setWidth = (i, width) => { const list = widths(); list[i] = clampColumnWidth(width); apply(list); };
    const saved = readStoredWidths(storedWidths(key), heads.length);
    if (saved) apply(saved);
    heads.forEach((cell, i) => addHandle(
      cell, "拖拽调整列宽（双击恢复自动）",
      () => { freeze(); const start = widths()[i]; return dx => setWidth(i, start + dx); },
      delta => { freeze(); setWidth(i, widths()[i] + delta); storeWidths(key, widths()); },
      () => {
        cols.forEach(col => { col.style.width = ""; });
        table.style.tableLayout = "";
        table.style.width = "";
        table.classList.remove("fixed");
        fixed = false;
        forgetWidths(key);
      },
      () => storeWidths(key, widths())
    ));
  }

  // The home list: 序号 is a fixed narrow column; 日期 and 路径 can be dragged; 内容 takes the rest.
  const HOME_INDEX_WIDTH = 52;
  function makeGridResizable(box) {
    const head = box?.querySelector(".recent-head");
    if (!head || head.dataset.resizable) return;
    head.dataset.resizable = "1";
    const spans = [...head.children];                    // 序号, 日期, 内容, 路径
    const key = "dek-cols:home-list";
    const setColumns = (first, last) => box.style.setProperty("--recent-cols", `${HOME_INDEX_WIDTH}px ${first}px minmax(0,1fr) ${last}px`);
    const current = () => [spans[1], spans[3]].map(cell => clampColumnWidth(cell.getBoundingClientRect().width));
    const room = (first, last) => head.clientWidth - HOME_INDEX_WIDTH - first - last >= 240;   // keep 内容 readable
    const saved = readStoredWidths(storedWidths(key), 2);
    if (saved && room(...saved)) setColumns(...saved);
    const move = (i, start, dx) => {
      const next = i === 0 ? [clampColumnWidth(start[0] + dx), start[1]] : [start[0], clampColumnWidth(start[1] - dx)];
      if (room(...next)) setColumns(...next);
    };
    spans.slice(1, 3).forEach((span, i) => addHandle(
      span, "拖拽调整列宽（双击恢复自动）",
      () => { const start = current(); return dx => move(i, start, dx); },
      delta => { move(i, current(), delta); storeWidths(key, current()); },
      () => { box.style.removeProperty("--recent-cols"); forgetWidths(key); },
      () => storeWidths(key, current())
    ));
  }

  document.querySelectorAll("table").forEach((table, index) => makeTableResizable(table, index));

  const homeApp = document.querySelector("#home-app");
  function initRecent(recentList) {
    const indexUrl = new URL(recentList.dataset.index, location.href);
    // Scripts are served from the installed code, but the page is only rebuilt by a
    // release: a page built before the nested sub-folder lists were dropped still
    // carries them, so drop them here rather than let them skew the counts.
    document.querySelectorAll(".card-subs").forEach(list => list.remove());
    const tabs = [...document.querySelectorAll(".recent-tab[data-days]")];
    const startInput = document.querySelector("#recent-start");
    const endInput = document.querySelector("#recent-end");
    let recentDocs = [];

    function isoDay(value) {
      const year = value.getFullYear();
      const month = String(value.getMonth() + 1).padStart(2, "0");
      const day = String(value.getDate()).padStart(2, "0");
      return `${year}-${month}-${day}`;
    }

    // The 无日期 tab lists the documents that carry no date; every other choice is a date range.
    let undatedOnly = false;

    // The list shows one page of `listDocs` at a time; choosing a filter starts again at page 1.
    const footer = document.querySelector("#recent-footer");
    let listDocs = [];
    let page = 1;
    let pageSize = DEKSearch.PAGE_SIZE;
    let mode = "0";   // what the list is of: "7" / "30" / "90" days, "0" for 全部, "undated", or "custom"
    let category = "";   // "" for every category, else the chosen card's path (say "wiki/03_药品核查")
    const cards = [...document.querySelectorAll(".folder-card-link")];
    const cardName = path => cards.find(card => card.dataset.path === path)?.querySelector("strong")?.textContent || path;

    // The page, and what it is a page of, live in the address bar (?page=3&range=90&size=40):
    // a refresh, the back button or a shared link lands on the same rows.
    const currentState = () => ({ range: mode, start: startInput?.value || "", end: endInput?.value || "", category, page, size: pageSize });
    function syncUrl(replace) {
      const url = location.pathname + DEKSearch.listStateToQuery(currentState()) + location.hash;
      if (url === location.pathname + location.search + location.hash) return;
      history[replace ? "replaceState" : "pushState"](null, "", url);
    }
    function goToPage(number) {
      page = number;
      renderPage();
      syncUrl();
      const table = document.querySelector(".recent-table");
      if (table && table.getBoundingClientRect().top < 0) table.scrollIntoView({ block: "start" });
    }

    function renderPage() {
      const pages = Math.max(1, Math.ceil(listDocs.length / pageSize));
      page = Math.min(Math.max(1, page), pages);
      if (!listDocs.length) {
        recentList.innerHTML = `<div class="muted">${undatedOnly ? "没有无日期的内容" : "该时间段内没有内容"}</div>`;
        if (footer) footer.hidden = true;
        return;
      }
      const firstNumber = (page - 1) * pageSize + 1;      // 序号 keeps counting across pages
      recentList.innerHTML = listDocs.slice((page - 1) * pageSize, page * pageSize).map((doc, offset) => {
        const href = DEKSearch.resultUrl(doc, indexUrl);
        // The whole path, wrapping at its slashes rather than cut off.
        const location = `${doc.kind.toUpperCase()} · ${doc.path}`;
        return `<a class="recent-item" href="${href}"><span class="recent-index">${firstNumber + offset}</span><span class="recent-date">${escapeHtml(doc.date || "")}</span><strong>${escapeHtml(doc.title)}</strong><small title="${escapeHtml(location)}">${escapeHtml(location).replace(/\//g, "/<wbr>")}</small></a>`;
      }).join("");
      if (footer) {
        footer.innerHTML = DEKSearch.pagerHtml(page, pages) + `<div class="list-tools">${DEKSearch.pageJumpHtml(pages)}${DEKSearch.pageSizeHtml(pageSize)}</div>`;
        footer.hidden = false;
      }
    }

    function renderRecentList(docs) {
      listDocs = docs;
      page = 1;
      renderPage();
    }

    footer?.addEventListener("click", event => {
      const link = event.target.closest("a[data-page]");
      if (!link) return;
      event.preventDefault();
      goToPage(Number(link.dataset.page));
    });
    footer?.addEventListener("change", event => {
      if (event.target.name !== "page_size") return;
      pageSize = DEKSearch.clampPageSize(event.target.value);
      page = 1;
      renderPage();
      syncUrl();
    });
    footer?.addEventListener("submit", event => {
      event.preventDefault();
      if (!event.target.classList.contains("page-jump")) return;
      const number = parseInt(event.target.elements.page_jump.value, 10);
      if (Number.isFinite(number)) goToPage(number);   // beyond either end lands on the first / last page
    });

    const customButton = document.querySelector("#recent-custom");
    const rangeBox = document.querySelector("#recent-range");
    const summary = document.querySelector("#recent-summary");

    function setCustomOpen(open) {
      if (rangeBox) rangeBox.hidden = !open;
      customButton?.classList.toggle("active", open);
      customButton?.setAttribute("aria-expanded", String(open));
    }

    // "全部" shows every card with its total. A chosen period, or 无日期, shows only
    // the categories that have something in it, with that count alone, and a
    // section with nothing says so. Documents without a date are outside any period.
    function updateCounts(start, end) {
      const filtered = Boolean(start || end || undatedOnly);
      document.querySelectorAll("[data-count-path]").forEach(element => {
        const total = Number(element.dataset.total);
        const path = element.dataset.countPath;
        const shown = undatedOnly ? DEKSearch.countUndated(recentDocs, path)
          : (filtered ? DEKSearch.countInRange(recentDocs, path, start, end) : total);
        const isSection = element.classList.contains("section-count");
        element.textContent = isSection ? String(shown) : (filtered ? `${shown} 篇` : `共 ${total} 篇`);
        const box = element.closest(".folder-card");
        // A chosen card stays even with nothing in the range, so it can be chosen off again.
        if (box) box.hidden = filtered && shown === 0 && box.querySelector(".folder-card-link")?.dataset.path !== category;
      });
      document.querySelectorAll(".home-section").forEach(section => {
        const anyCard = Boolean(section.querySelector(".folder-card:not([hidden])"));
        const grid = section.querySelector(".folder-grid");
        const empty = section.querySelector(".section-empty");
        if (grid) grid.hidden = !anyCard;
        if (empty) empty.hidden = anyCard;
      });
    }

    // Choosing a card selects that category as a filter on the list; choosing it again clears it.
    function markCategory() {
      cards.forEach(card => {
        const on = card.dataset.path === category;
        card.setAttribute("aria-pressed", String(on));
        card.closest(".folder-card")?.classList.toggle("selected", on);
      });
    }
    function chooseCategory(path) {
      category = category === path ? "" : path;
      renderRange();
      syncUrl();
    }
    cards.forEach(card => card.addEventListener("click", () => chooseCategory(card.dataset.path)));

    function renderRange() {
      const start = undatedOnly ? "" : (startInput?.value || "");
      const end = undatedOnly ? "" : (endInput?.value || "");
      // 全部 is everything: the dated ones newest first, then the ones with no date.
      const docs = undatedOnly ? DEKSearch.undatedDocuments(recentDocs)
        : (start || end) ? DEKSearch.recentDocumentsInRange(recentDocs, start, end)
        : [...DEKSearch.recentDocumentsInRange(recentDocs, "", ""), ...DEKSearch.undatedDocuments(recentDocs)];
      const shownDocs = category ? docs.filter(doc => DEKSearch.inCategory(doc, category)) : docs;
      renderRecentList(shownDocs);
      updateCounts(start, end);
      markCategory();
      if (summary) {
        const undated = recentDocs.filter(doc => !doc.date).length;
        if (category) {
          const scope = undatedOnly ? "无日期" : (start || end) ? `${start || "最早"} 至 ${end || "今天"}` : "全部";
          summary.textContent = `${category.startsWith("wiki") ? "Wiki" : "Source"} · ${cardName(category)}（${scope}）：共 ${shownDocs.length} 篇。 `;
          const clear = document.createElement("button");
          clear.type = "button";
          clear.className = "link-button";
          clear.textContent = "取消选择分类";
          clear.addEventListener("click", () => chooseCategory(category));
          summary.appendChild(clear);
          return;
        }
        summary.textContent = undatedOnly
          ? `无日期的内容共 ${docs.length} 篇。上方只显示含有无日期内容的分类，数字为无日期的篇数。`
          : (start || end)
            ? `${start || "最早"} 至 ${end || "今天"}：共 ${docs.length} 篇。上方只显示该时段内有内容的分类，数字为该时段的篇数。`
            : `全部 ${recentDocs.length} 篇` + (undated ? `，其中 ${undated} 篇无日期，排在列表最后；也可点“无日期”单独查看。` : "。");
      }
    }

    // `days` is a number of days, 0 for 全部, or "undated" for 无日期.
    function applyDays(days) {
      setCustomOpen(false);
      undatedOnly = days === "undated";
      mode = String(days);
      tabs.forEach(tab => tab.classList.toggle("active", tab.dataset.days === String(days)));
      if (!startInput || !endInput) return;
      if (undatedOnly || Number(days) === 0) {
        startInput.value = "";
        endInput.value = "";
      } else {
        const end = new Date();
        const start = new Date(end.getTime() - (Number(days) - 1) * 86400000);
        endInput.value = isoDay(end);
        startInput.value = isoDay(start);
      }
      renderRange();
    }

    // Show what the address bar says: the range, the page size and the page (clamped to what exists).
    function restoreFromUrl(initial) {
      const state = DEKSearch.listStateFromQuery(location.search);
      pageSize = state.size;
      category = cards.some(card => card.dataset.path === state.category) ? state.category : "";
      if (state.range === "custom") {
        undatedOnly = false;
        mode = "custom";
        tabs.forEach(tab => tab.classList.remove("active"));
        setCustomOpen(true);
        if (startInput) startInput.value = state.start;
        if (endInput) endInput.value = state.end;
        renderRange();
      } else {
        applyDays(state.range === "undated" ? "undated" : Number(state.range));
      }
      page = state.page;
      renderPage();
      if (initial) syncUrl(true);   // tidy an out-of-range page number without adding a history entry
    }
    window.addEventListener("popstate", () => { if (recentDocs.length) restoreFromUrl(false); });

    function loadRecent() {
      recentList.innerHTML = '<div class="muted">正在加载信息速览…</div>';
      loadSharedIndex(indexUrl)
        .then(data => { recentDocs = data; restoreFromUrl(true); })
        .catch(() => {
          recentList.innerHTML = '<div class="muted">信息速览加载失败，<button type="button" id="recent-retry">点击重试</button></div>';
          document.querySelector("#recent-retry")?.addEventListener("click", loadRecent);
        });
    }
    loadRecent();

    tabs.forEach(tab => tab.addEventListener("click", () => {
      applyDays(tab.dataset.days === "undated" ? "undated" : Number(tab.dataset.days));
      syncUrl();
    }));
    customButton?.addEventListener("click", () => {
      const open = rangeBox?.hidden !== false;
      undatedOnly = false;
      tabs.forEach(tab => tab.classList.remove("active"));
      setCustomOpen(open);
      if (open) mode = "custom"; else applyDays(0);
      syncUrl();
    });
    [startInput, endInput].forEach(field => field?.addEventListener("change", () => {
      undatedOnly = false;
      mode = "custom";
      tabs.forEach(tab => tab.classList.remove("active"));
      setCustomOpen(true);
      renderRange();
      syncUrl();
    }));
  }

  if (homeApp) {
    // The page comes from installed code; the release supplies only manifest.json.
    const manifestUrl = new URL(homeApp.dataset.manifest, location.href);
    const loadHome = () => {
      homeApp.textContent = "正在加载首页…";
      fetch(manifestUrl, { credentials: "same-origin", cache: "no-store" })
        .then(response => response.ok ? response.json() : Promise.reject())
        .then(({ tree }) => {
          homeApp.innerHTML = DEKSearch.homeHtml(tree, { indexPath: homeApp.dataset.index });
          initRecent(document.querySelector("#recent-list"));
          makeGridResizable(document.querySelector(".recent-table"));
        })
        .catch(() => {
          homeApp.innerHTML = '<div class="muted">首页加载失败，<button type="button" id="home-retry">点击重试</button></div>';
          document.querySelector("#home-retry")?.addEventListener("click", loadHome);
        });
    };
    loadHome();
  } else {
    // A page built by an older release still carries its own home markup.
    const legacyRecent = document.querySelector("#recent-list");
    if (legacyRecent) initRecent(legacyRecent);
  }

  document.addEventListener("click", event => {
    if (!event.target.closest(".search-wrap")) results?.classList.remove("open");
  });

  function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, character => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    })[character]);
  }
})();
