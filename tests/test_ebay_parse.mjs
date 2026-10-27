// Run: node card-vault/tests/test_ebay_parse.mjs
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import assert from "node:assert/strict";

const require = createRequire(import.meta.url);
const P = require("../ebay-parse.js");
const fx = (n) => readFileSync(new URL(`./fixtures/${n}`, import.meta.url), "utf8");

let passed = 0;
const test = (name, fn) => { fn(); passed++; console.log("ok  " + name); };

test("classify subjects", () => {
  assert.equal(P.classify("Order confirmed: 2004 Press Pass - Bi..."), "purchase");
  assert.equal(P.classify("Your order’s confirmed for 2020 PANINI DONRUSS ..."), "purchase");
  assert.equal(P.classify("🎉 You made the sale for 2025 Finest Tarik Skubal"), "sale");
  assert.equal(P.classify("You made the sale for 2022 Bowman Draft S/E Coby Mayo"), "sale");
  assert.equal(P.classify("Refund issued for order 11-14921-14930"), "refund");
  assert.equal(P.classify("📦ORDER DELIVERED: 2026 Topps Football ..."), null);
  assert.equal(P.classify("🚚 Order update: Genuine Battery for Ring ..."), null);
  assert.equal(P.classify("David, share feedback on your recent purchase"), null);
});

test("single purchase: landed cost includes shipping and tax", () => {
  const p = P.parsePurchase(fx("purchase_single.txt"));
  assert.equal(p.items.length, 1);
  const it = p.items[0];
  assert.equal(it.title, "2001-02 Upper Deck Pros & Prospects #D /1000 Shane Battier #111 Rookie RC");
  assert.equal(it.item_id, "366546977670");
  assert.equal(it.order, "10-15006-05973");
  assert.equal(it.price_cents, 350);
  assert.equal(it.cost_cents, 518); // 3.50 + 1.36 + 0.32
});

test("multi item purchase: total split by price and sums exactly", () => {
  const p = P.parsePurchase(fx("purchase_multi.txt"));
  assert.equal(p.items.length, 2);
  assert.equal(p.vault, true);
  const [a, b] = p.items;
  // 35951 * 23740 / 33740 = 25295.6 -> 25296; remainder 10655
  assert.equal(a.cost_cents, 25296);
  assert.equal(b.cost_cents, 10655);
  assert.equal(a.cost_cents + b.cost_cents, 35951);
});

test("recommended and sponsored items are ignored", () => {
  const p = P.parsePurchase(fx("purchase_single.txt"));
  assert.ok(p.items.every((i) => !i.title.includes("…")));
});

test("non card purchases are filtered", () => {
  const p = P.parsePurchase(fx("purchase_battery.txt"));
  assert.equal(P.looksLikeCard(p.items[0].title), false);
  assert.equal(P.looksLikeCard("Ultra Pro 3x4 Top Loaders 25 count card saver"), false);
  assert.equal(P.looksLikeCard("2020 PANINI DONRUSS OPTIC PURPLE SHOCK #151 JOE BURROW ROOKIE RC PSA 10"), true);
  assert.equal(P.looksLikeCard("Mike Dunleavy Duke Blue Devils 2002 #12"), true);
});

test("title parsing pulls year, grade, serial, number, flags", () => {
  const t = P.parseTitle("2025 Finest Tarik Skubal Pearl Checkerboard PSA 10 #/60 Dodgers Tigers GEM MT");
  assert.deepEqual([t.year, t.grader, t.grade, t.serial, t.number], ["2025", "PSA", 10, "/60", null]);
  const u = P.parseTitle("2001-02 Upper Deck Pros & Prospects #D /1000 Shane Battier #111 Rookie RC");
  assert.deepEqual([u.year, u.serial, u.number, u.rc], ["2001-02", "/1000", "111", true]);
  const v = P.parseTitle("2023 Bowman Chrome Gold Refractor Auto 25/50 #BCPA-JW BGS 9.5");
  assert.deepEqual([v.serial, v.number, v.grader, v.grade, v.auto], ["/50", "BCPA-JW", "BGS", 9.5, true]);
  assert.equal(P.parseTitle("BUNDLE - 3 League Tags").lot, true);
});

test("paid sale email", () => {
  const s = P.parseSale(fx("sale_paid.txt"), "You made the sale for 2022 Bowman Draft S/E Coby Mayo GOLD SAPPHIRE PROSP...");
  assert.equal(s.title, "2022 Bowman Draft S/E Coby Mayo GOLD SAPPHIRE PROSP");
  assert.equal(s.truncated, true);
  assert.equal(s.gross_cents, 8000);
  assert.equal(s.ship_cents, 597);
  assert.equal(s.order, "11-15140-35686");
  assert.equal(s.date, "2026-09-08");
  assert.equal(s.paid, true);
});

test("unpaid sale email keeps full title from subject", () => {
  const subj = "🎉 You made the sale for 2025 Finest Tarik Skubal Pearl Checkerboard PSA 10 #/60 Dodgers Tigers GEM MT";
  const s = P.parseSale(fx("sale_unpaid.txt"), subj);
  assert.equal(s.title, "2025 Finest Tarik Skubal Pearl Checkerboard PSA 10 #/60 Dodgers Tigers GEM MT");
  assert.equal(s.gross_cents, 4851);
  assert.equal(s.item_id, "398241806343");
  assert.equal(s.order, null);
  assert.equal(s.date, "2026-08-05");
  assert.equal(s.paid, false);
});

test("sale matches the owned card, not a different one", () => {
  const cards = [
    { id: "a", status: "owned", title: "2022 Bowman Draft Sapphire Edition Coby Mayo Gold Sapphire Prospect #BDC-20 /50" },
    { id: "b", status: "owned", title: "2022 Bowman Draft Chrome Coby Mayo Base #BDC-20" },
    { id: "c", status: "owned", title: "2020 Panini Prizm Joe Burrow RC #307" },
  ];
  const sale = P.parseSale(fx("sale_paid.txt"), "You made the sale for 2022 Bowman Draft S/E Coby Mayo GOLD SAPPHIRE PROSP...");
  const m = P.matchSale(sale, cards);
  assert.equal(m.card.id, "a");
});

test("second email for the same sale is recognized", () => {
  const cards = [{ id: "a", status: "sold", title: "2025 Finest Tarik Skubal Pearl Checkerboard PSA 10 #/60", sold: { at: "2026-08-05", gross_cents: 4851 } }];
  const sale = { title: "2025 Finest Tarik Skubal Pearl Checkerboard PSA 10", gross_cents: 4851, date: "2026-08-06" };
  const m = P.matchSale(sale, cards);
  assert.equal(m.already, true);
});

test("no confident match returns null", () => {
  const cards = [{ id: "c", status: "owned", title: "2020 Panini Prizm Joe Burrow RC #307" }];
  const sale = { title: "2019 Panini Passing Torch Rookie Evolution Autograph", gross_cents: 1000, date: "2026-08-12" };
  assert.equal(P.matchSale(sale, cards), null);
});

test("refund subject gives order number", () => {
  assert.equal(P.parseRefund("Refund issued for order 11-14921-14930").order, "11-14921-14930");
});

test("parseMessage routes by subject and falls back to message date", () => {
  const r = P.parseMessage({ id: "m1", subject: "Order confirmed: 2001-02 Upper Deck P...", date: "2026-08-09T00:37:09Z", plaintextBody: fx("purchase_single.txt") });
  assert.equal(r.kind, "purchase");
  assert.equal(r.date, "2026-08-09");
  assert.equal(r.items[0].cost_cents, 518);
});

test("sale email with heading markers inside table cells", () => {
  const s = P.parseSale(fx("sale_heading_cells.txt"), "You made the sale for 2025-26 Bowman Cooper Flagg RC Rookie #1 Mavericks");
  assert.deepEqual([s.gross_cents, s.ship_cents, s.order, s.date, s.qty, s.paid], [700, 132, "11-14540-09792", "2026-04-22", 1, true]);
});

test("sale never links to a different player's card on shared brand words", () => {
  const owned = [{ id: "ace", status: "owned", player: "Ace Bailey", title: "2025-26 Topps Chrome - Ace Bailey #255 Teal Refractor /299 (RC)" }];
  const sale = { title: "2025 Topps Chrome Tez Johnson Neon Pulse Refractor", gross_cents: 1000, date: "2026-05-17" };
  assert.equal(P.matchSale(sale, owned), null);
  assert.deepEqual(P.rankForSale(sale, owned), []);
  const same = { title: "2025-26 Topps Chrome Ace Bailey Teal Refractor /299 RC", gross_cents: 9000, date: "2026-09-01" };
  assert.equal(P.matchSale(same, owned).card.id, "ace");
});

console.log(`\n${passed} passed`);
