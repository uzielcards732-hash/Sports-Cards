# Sports Cards

Card Vault, a private sports card tracker. Live page: https://claude.ai/artifact/8L4MaE29JoU3X5UzVGbwUu

## How it stays current

| What | How | Your effort |
|---|---|---|
| eBay purchases (landed cost incl. shipping and tax) | Page reads Gmail on open (or the Sync eBay button) and parses order emails | None |
| eBay sales | Same sync; matched to the card you own, or a one tap pick | Occasional tap |
| Refunds | Same sync; card marked refunded | None |
| Show, shop and trade cards | Add card, snap a photo, Claude fills the fields | About 15 seconds |
| Values | Scheduled task, Sundays 7:37 AM ET: PSA cert check, CardSight match, value from recent sold prices, weekly snapshot | None |
| Sales data match not certain, or parallel unclear | Shows under "Needs a tap" with the choices | One tap |
| Card photos | Same weekly run: CardSight Card Magic straightens and squares each new photo (slabs keep the whole slab and label), and Identify fills empty fields | None, or one tap on "Is this the card?" |

Deleting a card records its eBay order and sale in `meta/sync.dismissed`, so no later sync or re-read adds it back. Gmail search returns at most 50 emails per query, so sync reads history in 90 day windows and splits any window that fills up.

## One time setup (blocks prices until done)

1. CardSight: create a free API key at cardsight.ai (no card needed; 750 calls a month free, paid plans from $14.95).
2. PSA: sign in at psacard.com/publicapi and generate an API token (100 calls a day free). Optional; prices work without it. As of Oct 2026 PSA answers 403 "Access to this API is limited to approved customers" for this account on every endpoint, so cert checks and pop counts stay off until PSA approves API access. The token is in place; once approved, the next weekly run verifies every slab with no change here. Until then each PSA slab links to its PSA cert page. CardSight's pop endpoint returned 0 for all 11 matched slabs, so it isn't a substitute yet.
3. Cloud environment `env_012eYRwBQs1pgh7YiNne3T2f` settings:
   - Environment variables: `CARDSIGHT_API_KEY`, `PSA_TOKEN` (the job reads these names)
   - Network access, Allowed domains: `api.cardsight.ai`, `api.psacard.com`
4. Open the page and tap Sync eBay once; allow Gmail and Claude when asked. The first sync reads 3 years of eBay email.

## Files

- `index.html` page; `ebay-parse.js` eBay email parser (also used by tests)
- `jobs/refresh.py` weekly pricing job (stdlib only); `jobs/RUNBOOK.md` steps the scheduled session follows
- `tests/` run `node tests/test_ebay_parse.mjs` and `python3 -I tests/test_refresh.py`

## Known estimates

- Realized P&L subtracts an estimated eBay fee: 13.25% of sale plus buyer shipping, plus $0.40 per order ($0.30 at $10 or less). Change `fee_pct` / `fee_fixed_cents` in the `meta/config` document if your rate differs.
- Card value = median of the most recent 10 completed auction sales of the same card, parallel and grade in the last 6 months, from CardSight (eBay, Fanatics Collect, COMC and others). Single high and low dropped at 5+ sales; a sale at more than 3x (or under a third of) the others' median is left out. 1 or 2 sales: shown with an asterisk. No sales: last value kept and marked, never guessed. Buy It Now asking prices are ignored.
- Title search: CardSight never ties many sales to a catalog card (for Josh Allen's 2018 Donruss #304, about 1 in 5). When a card has fewer than 5 catalog sales, the job also searches completed sales by listing title and keeps only titles that agree on player, year, product, card number, parallel, serial, auto and grade, and that name no sister product. Needs the card number (typed, or from the confirmed catalog match) and the year. Values that use it show `value_source: cardsight+titles`; "Sales used" links go to the listings.
- Coverage: only completed auctions count, so low volume cards (numbered refractors, older inserts) often show "No sales in the last year" instead of a number. When 6 months is empty the job looks back 12 months before giving up.
- Matching: CardSight splits each release into sub-sets that share card numbers. A clear match is taken automatically; close calls (sister products like Chrome vs Chrome Sapphire, variations like #301 vs #301V, auto vs non-auto sub-sets) go to "Needs a tap". Cards CardSight doesn't carry are marked "no sales data" without asking.
- CardSight lists some cards twice (2020 Prizm #307 Burrow has two catalog entries); title search picks up sales filed under either. Sister products that share a player and number (the 2003 Upper Deck LeBron box set vs Phenomenal Beginning) are learned once per card and kept in `guide.rivals`.
- Photos: up to 30 new photos a run, 2 CardSight calls each. The original photo is never deleted; the cleaned copy is stored as `photo_clean`. A photo with more than one card in it (booklets) keeps the original. Identify only fills fields you left empty, and only fills card details on a High confidence read that agrees with your player, number and year; any other read goes to "Is this the card?". Slab labels (grader, grade, cert) fill at High or Medium.
- eBay listing photos: after each sync the page reads the full order or sale email once per card and keeps the item's photo URL (matched by item id, so "you may also like" images are ignored). The weekly job downloads up to 30, stores them as the card's photo, and cleans them with Card Magic only (1 call each; the listing title already describes the card). Needs `i.ebayimg.com` in the environment's allowed domains.
- Call budget: about 200 one time catalog lookups, then each week 2 bulk calls, up to 40 deeper single card pulls and up to 60 title searches (2 per thin card at most). Usage is at `GET /v1/subscription/` (calendar month). Cap per run: `CARDSIGHT_MAX_CALLS` (default 150).
- Your CardSight key returns sales data (tested Oct 26). If it is ever turned down, the page says so; the $14.95 plan raises the call limit.
