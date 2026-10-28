// Parses eBay buyer/seller notification emails (Gmail plain-text bodies) into
// card purchase, sale and refund records. Pure functions, no I/O: the page
// feeds it Gmail results, and tests/test_ebay_parse.mjs feeds it fixtures.
(function (root) {
  "use strict";

  const cents = (s) => {
    if (s == null) return null;
    const m = String(s).replace(/,/g, "").match(/(-?\d+(?:\.\d{1,2})?)/);
    return m ? Math.round(parseFloat(m[1]) * 100) : null;
  };

  const clean = (s) => String(s || "").replace(/\s+/g, " ").trim();
  const stripEllipsis = (s) => clean(s).replace(/\s*(\.\.\.|…)$/, "");
  const isTruncated = (s) => /(\.\.\.|…)\s*$/.test(String(s || ""));

  const MONTHS = { jan: 1, feb: 2, mar: 3, apr: 4, may: 5, jun: 6, jul: 7, aug: 8, sep: 9, oct: 10, nov: 11, dec: 12 };
  const pad = (n) => String(n).padStart(2, "0");
  function ymdFromText(s) {
    const m = String(s || "").match(/([A-Z][a-z]{2})\w*\s+(\d{1,2}),\s*(\d{4})/);
    if (!m || !MONTHS[m[1].toLowerCase()]) return null;
    return `${m[3]}-${pad(MONTHS[m[1].toLowerCase()])}-${pad(m[2])}`;
  }
  function ymdFromIso(iso) {
    const d = new Date(iso);
    return isNaN(d) ? null : d.toISOString().slice(0, 10);
  }

  // A table cell "| Label | $1.23 |". eBay sometimes renders the label as a
  // heading inside the cell ("| #### Sold: | $7.00 |"), so allow that too.
  const cellMoney = (text, label) => cents((String(text).match(new RegExp("\\|\\s*(?:#+\\s*)?" + label + "\\s*\\|\\s*\\$\\s*([\\d,]+\\.\\d{2})", "i")) || [])[1]);
  const cellText = (text, label) => ((String(text).match(new RegExp("\\|\\s*(?:#+\\s*)?" + label + "\\s*\\|\\s*([^|\\n]+?)\\s*\\|", "i")) || [])[1]) || null;

  // ---- subject classification ------------------------------------------

  function classify(subject) {
    const s = clean(subject).replace(/^[^A-Za-z0-9]+/u, "");
    if (/^refund issued for order/i.test(s)) return "refund";
    if (/you made the sale for/i.test(s)) return "sale";
    if (/^order confirmed:/i.test(s) || /order(?:[’']s| is) confirmed/i.test(s)) return "purchase";
    return null;
  }

  // ---- title heuristics ------------------------------------------------

  const SUPPLY = /\b(top ?loaders?|penny sleeves?|sleeves|binders?|magnetic|one[- ]touch|card ?savers?|storage box|display case|team bags?|graded card (?:case|box)|slab case)\b/i;
  const CARD_HINT = /\b(topps|panini|bowman|prizm|donruss|optic|select|mosaic|chrome|finest|upper deck|fleer|score|leaf|press pass|hoops|contenders|national treasures|flawless|immaculate|spectra|obsidian|heritage|stadium club|ginter|sp authentic|o-pee-chee|skybox|pinnacle|playoff|absolute|certified|prestige|phoenix|origins|psa|bgs|sgc|cgc|bvg|rookie|rc|refractor|auto|autograph|patch|relic|parallel|prospects?|1st)\b/i;
  const LOT = /\b(lot|bundle|collection of|\d+\s*cards?)\b/i;

  function looksLikeCard(title) {
    const t = clean(title);
    if (!t || SUPPLY.test(t)) return false;
    if (CARD_HINT.test(t)) return true;
    return /\b(19[5-9]\d|20[0-4]\d)\b/.test(t) && /#\s*\w*\d/.test(t);
  }

  function parseTitle(title) {
    const t = clean(title);
    const out = { year: null, grader: null, grade: null, serial: null, number: null, rc: false, auto: false, lot: LOT.test(t) };
    const y = t.match(/\b(19[5-9]\d|20[0-4]\d)(?:[-\/](\d{2}))?\b/);
    if (y) out.year = y[2] ? `${y[1]}-${y[2]}` : y[1];
    const g = t.match(/\b(PSA|BGS|SGC|CGC|BVG|CSG|HGA|TAG)\s*(?:GEM\s*(?:MT|MINT)\s*|MINT\s*|NM-?MT\s*)?(10|9\.5|9|8\.5|8|7\.5|7|6\.5|6|5\.5|5|4|3|2|1\.5|1)\b/i);
    if (g) { out.grader = g[1].toUpperCase(); out.grade = parseFloat(g[2]); }
    const s = t.match(/(?:#\s*(?:'?d)?\s*|\b\d{1,4}\s*)\/\s*(\d{1,4})\b/i);
    if (s) out.serial = "/" + s[1];
    // Card numbers: "#111", "#BDC-150", "#BCPA-JW". Needs a digit or a hyphen,
    // so "#D /1000" (numbered-to) and "#/60" (serial) are not taken for one.
    const n = t.match(/#\s*((?=[A-Z0-9-]*[\d-])[A-Z0-9]{1,6}(?:-[A-Z0-9]{1,6})?)(?![\w\/]|\s*\/)/i);
    if (n) out.number = n[1].toUpperCase();
    out.rc = /\b(RC|ROOKIE)\b/i.test(t);
    out.auto = /\b(AUTO|AUTOGRAPH|AUTOGRAPHED|SIGNED)\b/i.test(t);
    return out;
  }

  // ---- purchase emails -------------------------------------------------

  function parsePurchase(body) {
    const text = String(body || "");
    const start = text.search(/Your order details/i);
    let region = start >= 0 ? text.slice(start) : text;
    const end = region.search(/Popular in your interests|Sponsored items|Order total/i);
    if (end > 0) region = region.slice(0, end);

    const re = /\[\s*###\s+([\s\S]+?)\s*\]\(https:\/\/www\.ebay\.com\/itm\/(\d+)/g;
    const hits = [];
    let m;
    while ((m = re.exec(region))) hits.push({ title: clean(m[1]), item_id: m[2], at: m.index });

    const items = hits.map((h, i) => {
      const seg = region.slice(h.at, i + 1 < hits.length ? hits[i + 1].at : region.length);
      const price = cents((seg.match(/Price:\s*\$\s*([\d,]+\.\d{2})/i) || [])[1]);
      const qty = parseInt((seg.match(/Quantity:\s*(\d+)/i) || [])[1] || "1", 10);
      const order = (seg.match(/Order number:\s*([\d-]+)/i) || [])[1] || null;
      return { title: h.title, item_id: h.item_id, price_cents: price, qty, order };
    }).filter((it) => it.price_cents != null);

    const total = cents((text.match(/Total charged[^|\n]*\|\s*\$\s*([\d,]+\.\d{2})/i) || [])[1]);
    const subtotal = cellMoney(text, "Subtotal");
    const shipping = cellMoney(text, "Shipping") || 0;
    const tax = cellMoney(text, "Sales tax") || 0;
    const orderAll = (text.match(/Order number:\s*([\d-]+)/i) || [])[1] || null;
    const seller = clean((text.match(/Seller:\s*([^\[\|\n]+)/i) || [])[1]) || null;

    // Spread shipping and tax across items by price, so each card carries its
    // true landed cost. The last item absorbs rounding so the parts sum to the
    // amount charged.
    const lineSum = items.reduce((a, it) => a + it.price_cents * it.qty, 0);
    const charged = total != null ? total : lineSum + shipping + tax;
    let allocated = 0;
    items.forEach((it, i) => {
      const line = it.price_cents * it.qty;
      const share = i === items.length - 1 ? charged - allocated : Math.round((charged * line) / (lineSum || 1));
      allocated += share;
      it.cost_cents = share;
      it.order = it.order || orderAll;
    });

    return { items, total_cents: total, subtotal_cents: subtotal, shipping_cents: shipping, tax_cents: tax, order: orderAll, seller, vault: /PSA Vault/i.test(region) };
  }

  // ---- sale emails -----------------------------------------------------

  function parseSale(body, subject) {
    const text = String(body || "");
    const subj = clean(subject).replace(/^.*?made the sale for\s*/i, "");
    const bm = text.match(/\[\s*###\s+([\s\S]+?)\s*\]\(https:\/\/www\.ebay\.com\/mesh\/ord\/details/i);
    const bodyTitle = bm ? clean(bm[1]) : "";
    // Prefer whichever title eBay did not cut short.
    let title = subj;
    if (!subj || (isTruncated(subj) && bodyTitle && !isTruncated(bodyTitle)) || (bodyTitle && stripEllipsis(bodyTitle).length > stripEllipsis(subj).length)) title = bodyTitle || subj;
    const orderCell = cellText(text, "Order:");
    return {
      title: stripEllipsis(title),
      truncated: isTruncated(title),
      gross_cents: cellMoney(text, "Sold:"),
      ship_cents: cellMoney(text, "Shipping:"),
      order: (/^[\d-]+$/.test(orderCell || "") ? orderCell : (text.match(/orderid=([\d-]+)/i) || [])[1]) || null,
      item_id: (text.match(/itemid=(\d+)/i) || [])[1] || null,
      date: ymdFromText(cellText(text, "Date sold:")),
      qty: parseInt(cellText(text, "Quantity sold:") || "1", 10) || 1,
      paid: !/hasn[’']?t paid yet/i.test(text),
    };
  }

  function parseRefund(subject) {
    const m = clean(subject).match(/refund issued for order\s+([\d-]+)/i);
    return { order: m ? m[1] : null };
  }

  // ---- title matching for sales ----------------------------------------

  const STOP = new Set(["the", "and", "of", "a", "card", "cards", "rc", "rookie", "gem", "mt", "mint", "nm", "new", "hot", "invest", "sp", "ssp", "lot", "w", "with", "1"]);
  function tokens(s) {
    return clean(s).toLowerCase().replace(/[^a-z0-9#\/.\s-]/g, " ").split(/[\s-]+/).map((x) => x.replace(/^#/, "")).filter((x) => x && !STOP.has(x));
  }
  // Share of the sale title's words found in the card's title. Sale titles are
  // often cut short, so this measures containment, not overlap.
  function containment(saleTitle, cardTitle) {
    const a = tokens(saleTitle);
    if (!a.length) return 0;
    const bList = tokens(cardTitle);
    const b = new Set(bList);
    // A cut-off word ("PROSP") still counts when a card word starts with it.
    const hit = (x) => b.has(x) || (x.length >= 4 && bList.some((y) => y.startsWith(x)));
    return a.filter(hit).length / a.length;
  }

  // A sale only links to a card when the player's name is in the sale title.
  // Shared brand, year and color words alone ("2025 Topps Chrome Refractor")
  // say nothing about which card it was.
  function samePlayer(saleTitle, c) {
    const sale = tokens(saleTitle);
    const name = tokens(c.player || "");
    if (!name.length) return false;
    const last = name[name.length - 1];
    return sale.some((t) => t === last || (t.length >= 4 && last.startsWith(t)));
  }

  // Top candidates for a sale that matched nothing confidently, for the review queue.
  function rankForSale(sale, cards, n = 3) {
    return cards
      .filter((c) => c.status === "owned" && samePlayer(sale.title, c))
      .map((c) => ({ id: c.id, score: containment(sale.title, [c.title, c.player, c.year, c.set, c.parallel, c.number].filter(Boolean).join(" ")) }))
      .filter((x) => x.score >= 0.5)
      .sort((x, y) => y.score - x.score)
      .slice(0, n);
  }

  function matchSale(sale, cards) {
    let best = null;
    for (const c of cards) {
      if (c.status === "refunded") continue;
      if (c.status === "sold") {
        // Second email for a sale already recorded (unpaid notice, then paid).
        const s = c.sold || {};
        const sameDay = s.at && sale.date && Math.abs(Date.parse(s.at) - Date.parse(sale.date)) <= 3 * 864e5;
        if (sameDay && s.gross_cents === sale.gross_cents && containment(sale.title, c.title || "") >= 0.6) return { card: c, score: 1, already: true };
        continue;
      }
      if (c.player && !samePlayer(sale.title, c)) continue;
      const score = containment(sale.title, [c.title, c.player, c.year, c.set, c.parallel, c.number].filter(Boolean).join(" "));
      if (!best || score > best.score) best = { card: c, score };
    }
    return best && best.score >= 0.75 ? best : null;
  }

  // ---- listing photos -------------------------------------------------

  // Item photos in an email's HTML, keyed by item id. eBay wraps each image in
  // its image proxy (imageUrl=<i.ebayimg.com url>) inside the link to the item.
  // Recommendations carry their own item ids, so callers keep only their items'.
  // The returned url is the full size version of the same photo.
  function imagesFromHtml(html) {
    const out = {};
    const h = String(html || "");
    const unesc = (s) => s.replace(/&amp;/g, "&").replace(/&quot;/g, '"').replace(/&#39;/g, "'");
    const re = /<img\b[^>]*\bsrc="([^"]+)"[^>]*>/gi;
    let m;
    while ((m = re.exec(h))) {
      let src = unesc(m[1]);
      if (/imageser/.test(src)) {
        const q = src.match(/[?&]imageUrl=([^&]+)/);
        if (!q) continue;
        try { src = decodeURIComponent(q[1]); } catch (e) { continue; }
      }
      if (!/^https:\/\/i\.ebayimg\.com\//.test(src)) continue;
      const a = h.lastIndexOf("<a ", m.index);
      const close = h.lastIndexOf("</a>", m.index);
      if (a < 0 || close > a) continue; // the image must sit inside a link
      const href = unesc((h.slice(a, m.index).match(/href="([^"]+)"/) || [])[1] || "");
      const id = (href.match(/\/itm\/(?:[^/?"]+\/)?(\d{9,})/) || href.match(/[?&]itemid=(\d{9,})/i) || [])[1];
      if (!id || out[id]) continue;
      const key = (src.match(/\/z\/([^/]+)\//) || src.match(/\/images\/g\/([^/]+)\//) || [])[1];
      out[id] = { url: key ? `https://i.ebayimg.com/images/g/${key}/s-l1600.jpg` : src.replace(/\?.*$/, ""), fallback: src.replace(/\?.*$/, "") };
    }
    return out;
  }

  // ---- whole-message entry point --------------------------------------

  function parseMessage(msg) {
    const kind = classify(msg.subject);
    const date = ymdFromIso(msg.date);
    const imgs = msg.images || (msg.htmlBody ? imagesFromHtml(msg.htmlBody) : {});
    if (kind === "purchase") {
      const p = parsePurchase(msg.plaintextBody);
      for (const it of p.items) if (it.item_id && imgs[it.item_id]) it.image_url = imgs[it.item_id].url;
      return { kind, id: msg.id, date, ...p };
    }
    if (kind === "sale") {
      const s = parseSale(msg.plaintextBody, msg.subject);
      return { kind, id: msg.id, ...s, image_url: (s.item_id && imgs[s.item_id] && imgs[s.item_id].url) || null, date: s.date || date };
    }
    if (kind === "refund") return { kind, id: msg.id, date, ...parseRefund(msg.subject) };
    return { kind: null, id: msg.id };
  }

  // Bumped with every change to how emails are read; the page re-reads eBay
  // history once when its own PARSER_VERSION is newer than the last sync's.
  const VERSION = 4;
  const api = { VERSION, classify, imagesFromHtml, looksLikeCard, parseTitle, parsePurchase, parseSale, parseRefund, parseMessage, matchSale, rankForSale, samePlayer, containment, cents };
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.EbayParse = api;
})(typeof window !== "undefined" ? window : globalThis);
