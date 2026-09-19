(() => {
  const root = document.documentElement;
  const savedTheme = localStorage.getItem("dek-theme");
  if (savedTheme) root.dataset.theme = savedTheme;

  document.querySelector("#theme-toggle")?.addEventListener("click", () => {
    root.dataset.theme = root.dataset.theme === "dark" ? "light" : "dark";
    localStorage.setItem("dek-theme", root.dataset.theme);
  });

  if (document.querySelector(".recent-filters")) document.querySelector("main.document")?.classList.add("home-page");

  const sidebar = document.querySelector(".sidebar");
  document.querySelector("#menu-toggle")?.addEventListener("click", () => sidebar?.classList.toggle("open"));

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
    const choose = row => { input.value = row.dataset.path; close(); };
    const render = () => {
      // Matching is by folder name only; the row shows the folder and the file name.
      const query = input.value.trim().toLowerCase();
      const matches = query ? options.filter(([label]) => label.toLowerCase().includes(query)) : options;
      list.innerHTML = matches.slice(0, 30).map(([label, path], i) =>
        `<div class="combo-option" role="option" id="combo-opt-${i}" data-path="${escapeHtml(path)}" title="${escapeHtml(path)}"><span class="combo-folder">${escapeHtml(label)}</span><small class="combo-file">${escapeHtml(path.split("/").pop())}</small></div>`
      ).join("");
      list.classList.toggle("open", matches.length > 0);
      input.setAttribute("aria-expanded", matches.length > 0 ? "true" : "false");
      setActive(-1);
    };
    input.addEventListener("focus", render);
    input.addEventListener("input", render);
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
        ? `<div class="result-count">找到 ${hits.length} 条相关结果</div>${hits.map(doc => `<a class="result" href="${DEKSearch.resultUrl(doc, indexUrl)}"><strong>${escapeHtml(doc.title)}</strong><small>${doc.kind.toUpperCase()} · ${escapeHtml(doc.path)}</small><span class="result-snippet">${escapeHtml(DEKSearch.resultSnippet(doc, query))}</span></a>`).join("")}`
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
      nav.innerHTML = homeLink + `<div role="tree" aria-label="知识库目录">${tree.map(node => renderNode(node)).join("")}</div>`;
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

  const recentList = document.querySelector("#recent-list");
  if (recentList) {
    const indexUrl = new URL(recentList.dataset.index, location.href);
    const tabs = [...document.querySelectorAll(".recent-tab")];
    const startInput = document.querySelector("#recent-start");
    const endInput = document.querySelector("#recent-end");
    let recentDocs = [];

    function isoDay(value) {
      const year = value.getFullYear();
      const month = String(value.getMonth() + 1).padStart(2, "0");
      const day = String(value.getDate()).padStart(2, "0");
      return `${year}-${month}-${day}`;
    }

    function renderRecentList(docs) {
      if (!docs.length) {
        recentList.innerHTML = '<div class="muted">该时间段内没有内容</div>';
        return;
      }
      recentList.innerHTML = docs.map(doc => {
        const href = DEKSearch.resultUrl(doc, indexUrl);
        const dateLabel = doc.date || "日期待确认";
        return `<a class="recent-item" href="${href}"><span class="recent-date">${escapeHtml(dateLabel)}</span><strong>${escapeHtml(doc.title)}</strong><small>${escapeHtml(doc.kind.toUpperCase())} · ${escapeHtml(doc.path)}</small></a>`;
      }).join("");
    }

    function renderRange() {
      renderRecentList(DEKSearch.recentDocumentsInRange(recentDocs, startInput?.value || "", endInput?.value || ""));
    }

    function applyDays(days) {
      tabs.forEach(tab => tab.classList.toggle("active", tab.dataset.days === String(days)));
      if (!startInput || !endInput) return;
      if (days === 0) {
        startInput.value = "";
        endInput.value = "";
      } else {
        const end = new Date();
        const start = new Date(end.getTime() - (days - 1) * 86400000);
        endInput.value = isoDay(end);
        startInput.value = isoDay(start);
      }
      renderRange();
    }

    function loadRecent() {
      recentList.innerHTML = '<div class="muted">正在加载最近信息…</div>';
      loadSharedIndex(indexUrl)
        .then(data => { recentDocs = data; applyDays(7); })
        .catch(() => {
          recentList.innerHTML = '<div class="muted">最近信息加载失败，<button type="button" id="recent-retry">点击重试</button></div>';
          document.querySelector("#recent-retry")?.addEventListener("click", loadRecent);
        });
    }
    loadRecent();

    tabs.forEach(tab => tab.addEventListener("click", () => applyDays(Number(tab.dataset.days))));
    [startInput, endInput].forEach(field => field?.addEventListener("change", () => {
      tabs.forEach(tab => tab.classList.remove("active"));
      renderRange();
    }));
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
