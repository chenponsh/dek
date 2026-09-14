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

  function resultSnippet(document, query, length = 140) {
    const text = String(document.text || "").replace(/\s+/g, " ").trim();
    if (!text) return "";
    const terms = normalize(query).split(" ").filter(Boolean);
    const normalizedText = normalize(text);
    let position = -1;
    for (const term of terms) {
      position = normalizedText.indexOf(term);
      if (position >= 0) break;
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

  return { normalize, scoreDocument, searchDocuments, resultSnippet, resultUrl, recentDocuments };
});
