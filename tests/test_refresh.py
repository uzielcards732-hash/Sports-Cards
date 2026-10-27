"""Run: python3 -I card-vault/tests/test_refresh.py  (no network; APIs are faked)"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jobs"))
import refresh as R  # noqa: E402

TODAY = "2026-11-01"

# CardSight catalog search results (shape from the cardsightai 4.3.0 SDK types)
BURROW = {"type": "card", "id": "c-burrow-151", "name": "Joe Burrow", "year": "2020", "setName": "Base Set",
          "releaseName": "Donruss Optic", "manufacturerName": "Panini", "cardNumber": "151", "relevance": 9}
BURROW_PRIZM = {"type": "card", "id": "c-burrow-307", "name": "Joe Burrow", "year": "2020", "setName": "Base Set",
                "releaseName": "Prizm", "manufacturerName": "Panini", "cardNumber": "307", "relevance": 8}
BURROW_SELECT = {"type": "card", "id": "c-burrow-44", "name": "Joe Burrow", "year": "2020", "setName": "Concourse",
                 "releaseName": "Select", "manufacturerName": "Panini", "cardNumber": "44", "relevance": 7}


def sale(price, date, parallel=None, kind="auction", title=None, tag=True):
    if title is None:
        title = "2020 Panini Donruss Optic Joe Burrow Rated Rookie #151" + (f" {parallel} Prizm" if parallel else "")
    return {"price": price, "date": date + "T18:00:00Z", "source": "ebay", "listing_type": kind, "title": title,
            "parallel_id": ("p-" + parallel.lower().replace(" ", "-")) if parallel and tag else None,
            "parallel_name": parallel if tag else None, "url": "https://www.ebay.com/itm/1"}


def pricing(raw=(), psa10=(), psa9=(), bgs95=()):
    graded = []
    if psa10 or psa9:
        grades = []
        if psa10:
            grades.append({"grade_value": "10", "grade_id": "g-psa-10", "period_days": 183, "count": len(psa10), "records": list(psa10)})
        if psa9:
            grades.append({"grade_value": "9", "grade_id": "g-psa-9", "period_days": 183, "count": len(psa9), "records": list(psa9)})
        graded.append({"company_name": "PSA", "company_id": "co-psa", "grades": grades})
    if bgs95:
        graded.append({"company_name": "Beckett", "company_id": "co-bgs", "grades": [{"grade_value": "9.5", "grade_id": "g-bgs-95", "count": 1, "records": list(bgs95)}]})
    return {"card": {"card_id": "x", "name": "Joe Burrow"}, "raw": {"period_days": 183, "count": len(raw), "records": list(raw)},
            "graded": graded, "meta": {}}


BURROW_SALES = pricing(
    raw=[sale(14, "2026-10-20"), sale(16, "2026-10-12"), sale(80, "2026-10-10", "Purple Shock")],
    psa10=[sale(250, "2026-10-28", "Purple Shock"), sale(230, "2026-10-21", "Purple Shock"), sale(240, "2026-10-14", "Purple Shock"),
           sale(400, "2026-10-07", "Purple Shock"), sale(200, "2026-09-30", "Purple Shock"), sale(245, "2026-10-01", "Purple Shock", "fixed"),
           sale(90, "2026-10-27"), sale(95, "2026-10-20")],
    psa9=[sale(110, "2026-10-25", "Purple Shock")],
)


class FakeClients(R.Clients):
    def __init__(self, search=None, bulk=None, deep=None, certs=None, cs="k", psa="t", unauthorized=False, psa_refusal=None,
                 titles=None):
        super().__init__(cs, psa, get=None, pause=0)
        self._search, self._bulk, self._deep, self._certs, self._unauth = search, bulk or {}, deep or {}, certs or {}, unauthorized
        self._psa_refusal, self._titles = psa_refusal, titles
        self.title_queries = []
        self.bulk_ids = []
        self.deep_calls = []
        self.queries = []

    def cs_search(self, q, year=None):
        self.calls["cardsight"] += 1
        self.queries.append(q)
        if self._unauth:
            raise R.Unauthorized("401")
        if callable(self._search):
            return self._search(q)
        return self._search if self._search is not None else [BURROW, BURROW_PRIZM, BURROW_SELECT]

    def cs_bulk(self, ids):
        self.calls["cardsight"] += 1
        if self._unauth:
            raise R.Unauthorized("401")
        self.bulk_ids.append(list(ids))
        return {i: self._bulk[i] for i in ids if i in self._bulk}

    def cs_card(self, card_id, grade_id=None, parallel_id=None, period="6m"):
        self.calls["cardsight"] += 1
        self.deep_calls.append((card_id, grade_id, parallel_id, period))
        d = self._deep.get(card_id)
        return d.get(period, d) if isinstance(d, dict) and period in d else (d or pricing())

    def cs_title_search(self, q, period="1y"):
        self.calls["cardsight"] += 1
        self.title_queries.append(q)
        if callable(self._titles):
            return self._titles(q)
        return list(self._titles or [])

    def psa_cert(self, cert):
        self.calls["psa"] += 1
        if self._psa_refusal:
            raise R.Unauthorized(self._psa_refusal)
        return self._certs.get(cert, {})


def card(**kw):
    base = {"id": "c1", "status": "owned", "title": "2020 PANINI DONRUSS OPTIC PURPLE SHOCK #151 JOE BURROW ROOKIE RC PSA 10",
            "player": "Joe Burrow", "year": "2020", "set": "Panini Donruss Optic", "parallel": "Purple Shock", "number": "151",
            "grader": "PSA", "grade": 10, "cost_cents": 25951}
    base.update(kw)
    return base


class Matching(unittest.TestCase):
    def test_exact_card_scores_high_others_low(self):
        c = card()
        self.assertGreaterEqual(R.score_candidate(c, BURROW), R.AUTO_ACCEPT)
        self.assertLess(R.score_candidate(c, BURROW_PRIZM), 0.5)
        self.assertLess(R.score_candidate(c, BURROW_SELECT), 0.5)

    def test_wrong_player_is_rejected(self):
        self.assertLessEqual(R.score_candidate(card(), {**BURROW, "name": "Justin Herbert"}), 0.1)

    def test_queries_go_short(self):
        self.assertEqual(R.search_queries(card()), ["Joe Burrow 151", "Joe Burrow Panini Donruss Optic", "Joe Burrow Optic", "Joe Burrow"])
        self.assertEqual(R.search_queries(card(set="Panini Contenders Draft Picks"))[2], "Joe Burrow Contenders")
        self.assertEqual(R.search_queries(card(number=None))[0], "Joe Burrow Panini Donruss Optic")
        q = R.build_query({"title": "2025 Finest Tarik Skubal Pearl PSA 10 GEM MT RC #/60"})
        for word in ("PSA", "GEM", "/60"):
            self.assertNotIn(word, q)

    def test_accents_and_hyphens(self):
        c = card(player="Kasparas Jakucionis", year="2025-26", set="Topps Chrome", number="270", parallel=None, grader=None, grade=None,
                 title="2025-26 Topps Chrome - Kasparas Jakucionis #270 Teal Refractor /299 (RC)")
        r = {"id": "k", "name": "Kasparas Jakučionis", "year": "2025-26", "releaseName": "Topps Chrome", "setName": "Base Set",
             "manufacturerName": "Topps", "cardNumber": "270"}
        self.assertGreaterEqual(R.score_candidate(c, r), R.AUTO_ACCEPT)

    def test_sister_product_is_never_auto_accepted(self):
        c = card(player="Kasparas Jakucionis", year="2025-26", set="Topps Chrome", number="270", parallel=None, grader=None, grade=None, title="2025-26 Topps Chrome Kasparas Jakucionis #270")
        sapphire = {"id": "s", "name": "Kasparas Jakučionis", "year": "2025-26", "releaseName": "Topps Chrome Sapphire", "setName": "Base Set", "manufacturerName": "Topps", "cardNumber": "270"}
        self.assertLess(R.score_candidate(c, sapphire), R.AUTO_ACCEPT)
        black = card(set="Panini Prizm Black", title="2025 Panini Prizm Black Darrelle Revis Silver Auto #105", player="Darrelle Revis", year="2025", number="105", grader=None, grade=None, parallel="Silver", auto=True)
        plain = {"id": "p", "name": "Darrelle Revis", "year": "2025", "releaseName": "Panini Prizm", "setName": "Base Set", "manufacturerName": "Panini", "cardNumber": "105"}
        self.assertLess(R.score_candidate(black, plain), R.AUTO_ACCEPT)

    def test_variation_number_goes_to_review(self):
        c = card(player="Trevor Lawrence", year="2021", set="Panini Mosaic", number="301V", parallel="Silver Prizm Variation", title="Trevor Lawrence 2021 Panini Mosaic RC Silver Prizm Variation PSA 10 #301V")
        r = {"id": "v", "name": "Trevor Lawrence", "year": "2021", "releaseName": "Panini Mosaic", "setName": "Base Set", "manufacturerName": "Panini", "cardNumber": "301"}
        self.assertLess(R.score_candidate(c, r), R.AUTO_ACCEPT)

    def test_serial_is_not_a_card_number(self):
        c = card(player="Cade Klubnik", year="2024", set="Bowman Chrome", number="7", parallel="Green Refractor", auto=True, grader=None, grade=None,
                 title="2024 Bowman Chrome Cade Klubnik Auto Green Refractor #7/99")
        r = {"id": "c", "name": "Cade Klubnik", "year": "2024", "releaseName": "Bowman Chrome", "setName": "Chrome Prospects Autographs Set", "manufacturerName": "Bowman", "cardNumber": "PA-CKL"}
        self.assertGreater(R.score_candidate(c, r), 0.5)

    def test_auto_card_prefers_auto_subset(self):
        c = card(player="Bilal Powell", year="2011", set="Panini Crown Royale", number="229", parallel=None, auto=True, grader=None, grade=None,
                 title="2011 Panini Crown Royale Bilal Powell Auto Patch RC #229 /499 Jets")
        auto = {"id": "a", "name": "Bilal Powell", "year": "2011", "releaseName": "Panini Crown Royale", "setName": "Rookie Die Cut Material Autographs", "manufacturerName": "Panini", "cardNumber": "229"}
        base = {**auto, "id": "b", "setName": "Base Set"}
        self.assertGreater(R.score_candidate(c, auto), R.score_candidate(c, base))


class Comps(unittest.TestCase):
    def test_psa10_parallel_median_drops_high_and_low(self):
        comp = R.comp_value(BURROW_SALES, card(), TODAY)
        # Purple Shock PSA 10 auctions: 250, 230, 240, 400, 200 (the fixed 245 ask is ignored).
        # Five sales, so drop 200 and 400; median of 230, 240, 250 = 240.
        self.assertEqual((comp["cents"], comp["n"], comp["thin"]), (24000, 5, False))
        self.assertEqual(comp["last_sale"], "2026-10-28")
        self.assertEqual(comp["basis"], "Median of 5 sold, PSA 10, last sale Oct 28")

    def test_base_card_ignores_parallel_sales(self):
        comp = R.comp_value(BURROW_SALES, card(parallel=None), TODAY)
        self.assertEqual((comp["cents"], comp["n"], comp["thin"]), (9250, 2, True))

    def test_raw_card_uses_raw_section(self):
        comp = R.comp_value(BURROW_SALES, card(parallel=None, grader=None, grade=None), TODAY)
        self.assertEqual(comp["cents"], 1500)

    def test_old_sales_are_out_of_window(self):
        data = pricing(raw=[sale(500, "2026-01-02")])
        comp = R.comp_value(data, card(parallel=None, grader=None, grade=None), TODAY)
        self.assertIsNone(comp["cents"])

    def test_no_sales_of_this_version_is_no_value_not_a_guess(self):
        comp = R.comp_value(BURROW_SALES, card(parallel="Holo Green"), TODAY)
        self.assertIsNone(comp["cents"])
        self.assertIsNone(comp["parallel_options"])

    def test_untagged_parallel_sales_are_kept_out_of_base(self):
        data = pricing(raw=[sale(14, "2026-10-20"), sale(28, "2026-10-21", "Pink", tag=False), sale(60, "2026-10-22", title="2020 Optic Joe Burrow #151 /99 Holo")])
        comp = R.comp_value(data, card(parallel=None, grader=None, grade=None), TODAY)
        self.assertEqual((comp["cents"], comp["n"]), (1400, 1))

    def test_untagged_sale_counts_when_title_names_the_parallel(self):
        data = pricing(psa10=[sale(270, "2026-10-29", "Purple Shock", tag=False), sale(250, "2026-10-28", "Purple Shock")])
        self.assertEqual(R.comp_value(data, card(), TODAY)["n"], 2)

    def test_auto_and_non_auto_sales_never_mix(self):
        c = card(player="Armand Membou", set="Topps Signature Class", number="163", parallel=None, grader=None, grade=None, auto=True,
                 title="Topps 2025 Signature Class Armand Membou Jets Rookie Pick Auto #163 /275")
        data = pricing(raw=[sale(5.5, "2026-08-22", title="Topps 2025 Signature Class Armand Membou RC Autograph #163"),
                            sale(4.0, "2026-08-06", title="Topps 2025 Signature Class Armand Membou Jets RC Rookie #163")])
        comp = R.comp_value(data, c, TODAY)
        self.assertEqual((comp["cents"], comp["n"]), (550, 1))

    def test_insert_name_in_parallel_field_is_treated_as_subset(self):
        c = card(parallel="Rookie of the Year Autographs", guide={"id": "x", "subset": "Rookie of the Year Contenders Autographs", "release": "Panini Contenders"})
        self.assertIsNone(R.effective_parallel(c))

    def test_sales_list_kept_for_checking(self):
        comp = R.comp_value(BURROW_SALES, card(), TODAY)
        self.assertEqual(comp["sales"][0]["date"], "2026-10-28")
        self.assertTrue(comp["sales"][0]["url"].startswith("https://"))

    def test_beckett_name_maps_to_bgs(self):
        data = pricing(bgs95=[sale(300, "2026-10-01")])
        self.assertEqual(R.comp_value(data, card(parallel=None, grader="BGS", grade=9.5), TODAY)["cents"], 30000)

    def test_parallel_name_rules(self):
        self.assertTrue(R.same_parallel("Purple Shock", "purple shock"))
        self.assertTrue(R.same_parallel("Gold Refractor /50", "Gold Refractor"))
        self.assertFalse(R.same_parallel("Gold Wave Refractor", "Gold"))
        self.assertFalse(R.same_parallel("Silver", None))
        self.assertTrue(R.same_parallel(None, None))


class Run(unittest.TestCase):
    def test_new_card_matched_then_valued_in_one_run(self):
        cl = FakeClients(bulk={"c-burrow-151": BURROW_SALES})
        patches, summary, snap = R.run([card()], cl, TODAY)
        p = patches["c1"]
        self.assertEqual(p["guide"]["id"], "c-burrow-151")
        self.assertEqual(p["value_cents"], 24000)
        self.assertEqual(p["history"], [[TODAY, 24000]])
        self.assertEqual(cl.calls["cardsight"], 2)  # one search, one bulk
        self.assertEqual(snap["value_cents"], 24000)
        self.assertEqual(summary["missing_tokens"], [])

    def test_bulk_batches_of_100(self):
        cards = [card(id=f"c{i}", guide={"id": f"g{i}", "subset": "Base Set"}) for i in range(150)]
        cl = FakeClients(bulk={f"g{i}": BURROW_SALES for i in range(150)})
        R.run(cards, cl, TODAY)
        self.assertEqual([len(b) for b in cl.bulk_ids], [100, 50])
        self.assertEqual(cl.calls["cardsight"], 2)

    def test_ambiguous_match_goes_to_review(self):
        c = card(player="Joe Burrow", year=None, number=None, parallel=None, title="Joe Burrow Donruss Optic rookie")
        patches, summary, _ = R.run([c], FakeClients(), TODAY)
        self.assertTrue(patches["c1"]["needs_review"])
        self.assertEqual(patches["c1"]["guide_candidates"][0]["id"], "c-burrow-151")
        self.assertEqual(summary["new_reviews"], 1)
        self.assertEqual(len(summary["ambiguous"]), 1)

    def test_crowded_out_card_gets_one_deep_pull(self):
        crowded = pricing(raw=[sale(5, "2026-10-20", "Silver")] * 50)
        deep = pricing(psa10=[sale(260, "2026-10-29", "Purple Shock"), sale(240, "2026-10-22", "Purple Shock"), sale(250, "2026-10-15", "Purple Shock")])
        cl = FakeClients(bulk={"c-burrow-151": crowded}, deep={"c-burrow-151": deep})
        patches, _, _ = R.run([card(guide={"id": "c-burrow-151", "subset": "Base Set"})], cl, TODAY)
        self.assertEqual(patches["c1"]["value_cents"], 25000)
        self.assertEqual(cl.calls["cardsight"] - len(cl.title_queries), 3)  # bulk, one deep pull, one rival lookup
        self.assertEqual(len(cl.title_queries), 2)  # 3 sales is still thin: strict then loose title search

    def test_deep_pull_keeps_untagged_sales_named_in_the_title(self):
        # Bulk rows crowded out by other versions; one Purple Shock PSA 10 sale tagged.
        crowded = pricing(raw=[sale(5, "2026-10-20", "Silver")] * 49, psa10=[sale(250, "2026-10-28", "Purple Shock")])
        deep = pricing(psa10=[sale(250, "2026-10-28", "Purple Shock"), sale(240, "2026-10-22", "Purple Shock", tag=False),
                              sale(260, "2026-10-15", "Purple Shock", tag=False), sale(90, "2026-10-14")])
        cl = FakeClients(bulk={"c-burrow-151": crowded}, deep={"c-burrow-151": deep})
        patches, _, _ = R.run([card(guide={"id": "c-burrow-151", "subset": "Base Set"})], cl, TODAY)
        self.assertEqual(cl.deep_calls[0][2], None)  # whole grade, not only tagged sales
        self.assertEqual(patches["c1"]["value_n"], 3)
        self.assertEqual(patches["c1"]["value_cents"], 25000)

    def test_merge_pricing_keeps_each_sale_once(self):
        a = pricing(psa10=[sale(250, "2026-10-28", "Purple Shock")], raw=[sale(14, "2026-10-20")])
        b = pricing(psa10=[sale(250, "2026-10-28", "Purple Shock"), sale(240, "2026-10-22", "Purple Shock")])
        m = R.merge_pricing(a, b)
        self.assertEqual(len(R.section(m, "PSA", 10)[0]), 2)
        self.assertEqual(len(m["raw"]["records"]), 1)

    def test_no_sales_keeps_value_and_marks_stale(self):
        cl = FakeClients(bulk={"g": pricing()})
        patches, _, snap = R.run([card(guide={"id": "g", "subset": "Base Set"}, value_cents=20000, history=[["2026-10-25", 20000]])], cl, TODAY)
        p = patches["c1"]
        self.assertTrue(p["value_stale"])
        self.assertNotIn("value_cents", p)
        self.assertEqual(snap["value_cents"], 20000)

    def test_old_version_question_is_cleared(self):
        cl = FakeClients(bulk={"g": BURROW_SALES})
        patches, _, _ = R.run([card(guide={"id": "g", "subset": "Base Set"}, needs_review=True, review_reason=R.PARALLEL_REASON)], cl, TODAY)
        self.assertFalse(patches["c1"]["needs_review"])
        self.assertEqual(patches["c1"]["value_cents"], 24000)

    def test_year_fallback_when_six_months_is_empty(self):
        older = pricing(psa10=[sale(210, "2026-02-01", "Purple Shock")])
        cl = FakeClients(bulk={"g": pricing()}, deep={"g": {"1y": older}})
        patches, _, _ = R.run([card(guide={"id": "g", "subset": "Base Set"})], cl, TODAY)
        self.assertEqual(patches["c1"]["value_cents"], 21000)
        self.assertIn("last 12 months", patches["c1"]["value_basis"])
        self.assertEqual(cl.deep_calls[-1][3], "1y")

    def test_slab_without_grade_asks_for_it(self):
        cl = FakeClients(bulk={"g": BURROW_SALES})
        patches, _, _ = R.run([card(guide={"id": "g", "subset": "Base Set"}, grade=None)], cl, TODAY)
        self.assertEqual(patches["c1"]["review_reason"], R.GRADE_REASON)
        self.assertNotIn("value_cents", patches["c1"])

    def test_searches_widen_until_a_clear_match(self):
        cl = FakeClients(search=lambda q: [] if ("Donruss" in q or "151" in q) else [BURROW, BURROW_PRIZM], bulk={"c-burrow-151": BURROW_SALES})
        patches, _, _ = R.run([card()], cl, TODAY)
        self.assertEqual(patches["c1"]["guide"]["id"], "c-burrow-151")
        self.assertEqual(cl.queries, ["Joe Burrow 151", "Joe Burrow Panini Donruss Optic", "Joe Burrow Optic"])

    def test_auto_no_match_retried_once_when_searches_change(self):
        cl = FakeClients(bulk={"c-burrow-151": BURROW_SALES})
        patches, _, _ = R.run([card(guide={"none": True, "auto": True}, guide_query="Joe Burrow Panini Donruss Optic | Joe Burrow Optic | Joe Burrow")], cl, TODAY)
        self.assertEqual(patches["c1"]["guide"]["id"], "c-burrow-151")
        self.assertEqual(patches["c1"]["value_cents"], 24000)
        again = FakeClients(search=[])
        key = " | ".join(R.search_queries(card()))
        R.run([card(guide={"none": True, "auto": True}, guide_query=key)], again, TODAY)
        self.assertEqual(again.queries, [])

    def test_player_number_year_exact_is_auto_accepted(self):
        allen = {"id": "c-allen-304", "type": "card", "name": "Josh Allen", "year": "2018", "releaseName": "Donruss", "setName": "Base Set", "manufacturerName": "Panini", "cardNumber": "304"}
        plate = {**allen, "id": "c-allen-plate", "releaseName": "Panini Plates & Patches", "setName": "2018 Donruss Printing Plates"}
        c = card(player="Josh Allen", year="2018", set="Panini Donruss", number="304", parallel=None, title="2018 Panini Donruss Josh Allen #304")
        patches, _, _ = R.run([c], FakeClients(search=[allen, plate]), TODAY)
        self.assertEqual(patches["c1"]["guide"]["id"], "c-allen-304")

    def test_owner_no_match_is_never_retried(self):
        cl = FakeClients()
        R.run([card(guide={"none": True}, guide_query="old")], cl, TODAY)
        self.assertEqual(cl.queries, [])

    def test_not_in_catalog_marks_no_data_without_nagging(self):
        patches, summary, _ = R.run([card(player="Carlos Boozer", set="Press Pass", number="BN", parallel="Big Numbers", grader=None, grade=None,
                                          title="Press Pass 2002 Carlos Boozer Duke Big Numbers")], FakeClients(search=[]), TODAY)
        self.assertEqual(patches["c1"]["guide"], {"none": True, "auto": True})
        self.assertNotIn("needs_review", patches["c1"])
        self.assertEqual(summary["new_reviews"], 0)

    def test_first_version_matches_are_redone(self):
        cl = FakeClients(bulk={"c-burrow-151": BURROW_SALES})
        patches, _, _ = R.run([card(guide={"id": "old", "name": "Joe Burrow"})], cl, TODAY)
        self.assertEqual(patches["c1"]["guide"]["id"], "c-burrow-151")

    def test_existing_value_tracks_previous_and_caps_history(self):
        hist = [[f"2026-01-{i:02d}", i] for i in range(1, 31)]
        cl = FakeClients(bulk={"g": BURROW_SALES})
        patches, _, _ = R.run([card(guide={"id": "g", "subset": "Base Set"}, value_cents=20000, history=hist)], cl, TODAY)
        p = patches["c1"]
        self.assertEqual(p["value_prev_cents"], 20000)
        self.assertEqual(len(p["history"]), R.HISTORY_LEN)
        self.assertEqual(p["history"][-1], [TODAY, 24000])

    def test_psa_cert_fills_fields_and_grade_wins(self):
        cert = {"PSACert": {"CertNumber": "12345678", "Year": "2020", "Brand": "PANINI DONRUSS OPTIC", "Subject": "JOE BURROW",
                            "CardNumber": "151", "Variety": "PURPLE SHOCK", "CardGrade": "GEM MT 10", "TotalPopulation": 41}}
        cl = FakeClients(certs={"12345678": cert}, bulk={"g": BURROW_SALES})
        patches, _, _ = R.run([card(cert="12345678", grade=9, player=None, guide={"id": "g", "subset": "Base Set"})], cl, TODAY)
        p = patches["c1"]
        self.assertEqual(p["grade"], 10.0)
        self.assertEqual(p["player"], "Joe Burrow")
        self.assertEqual(p["psa"]["pop"], 41)
        self.assertNotIn("set", p)  # owner's wording kept
        self.assertEqual(p["value_cents"], 24000)  # valued at the verified grade

    def test_psa_unknown_cert_flags_review(self):
        patches, _, _ = R.run([card(cert="999", guide={"id": "g", "subset": "Base Set"})], FakeClients(bulk={"g": BURROW_SALES}), TODAY)
        self.assertTrue(patches["c1"]["needs_review"])

    def test_missing_keys_reported_not_fatal(self):
        patches, summary, _ = R.run([card(cert="1")], FakeClients(cs=None, psa=None), TODAY)
        self.assertEqual(patches, {})
        self.assertEqual(summary["missing_tokens"], ["cardsight_key_missing", "psa_token_missing"])

    def test_rejected_key_reported(self):
        _, summary, _ = R.run([card()], FakeClients(unauthorized=True), TODAY)
        self.assertEqual(summary["missing_tokens"], ["cardsight_unauthorized"])

    def test_psa_account_not_approved_stops_after_one_call_and_prices_continue(self):
        refusal = '403 {"Message":"Access to this API is limited to approved customers."}'
        cl = FakeClients(bulk={"g": BURROW_SALES}, psa_refusal=refusal)
        cards = [card(id="c1", cert="111", guide={"id": "g", "subset": "Base Set"}), card(id="c2", cert="222", guide={"id": "g", "subset": "Base Set"})]
        patches, summary, _ = R.run(cards, cl, TODAY)
        self.assertEqual(cl.calls["psa"], 1)
        self.assertEqual(summary["missing_tokens"], ["psa_not_approved"])
        self.assertEqual(patches["c2"]["value_cents"], 24000)
        self.assertNotIn("needs_review", patches["c1"])

    def test_psa_bad_token_reported(self):
        _, summary, _ = R.run([card(cert="111", guide={"id": "g", "subset": "Base Set"})], FakeClients(bulk={"g": BURROW_SALES}, psa_refusal="401"), TODAY)
        self.assertEqual(summary["missing_tokens"], ["psa_unauthorized"])

    def test_sold_cards_untouched(self):
        patches, _, _ = R.run([card(status="sold", guide={"id": "g", "subset": "Base Set"})], FakeClients(bulk={"g": BURROW_SALES}), TODAY)
        self.assertEqual(patches, {})

    def test_snapshot_only_when_something_is_priced(self):
        _, summary, _ = R.run([], FakeClients(), TODAY)
        self.assertFalse(summary["write_snapshot"])
        _, summary, _ = R.run([card(guide={"none": True})], FakeClients(), TODAY)
        self.assertFalse(summary["write_snapshot"])


class Portfolio(unittest.TestCase):
    def test_totals_and_unrealized(self):
        cards = [
            {"id": "a", "status": "owned", "value_cents": 10000, "cost_cents": 6000},
            {"id": "b", "status": "owned", "value_cents": 5000},
            {"id": "c", "status": "owned", "cost_cents": 2000},
            {"id": "d", "status": "sold", "value_cents": 99999, "cost_cents": 1},
        ]
        self.assertEqual(R.totals(cards), {"count": 3, "priced": 2, "value_cents": 15000, "cost_cents": 8000, "unrealized_cents": 4000})

    def test_notify_rules(self):
        t = {"value_cents": 100000}
        n, text = R.notify_decision(t, {"value_cents": 99000, "date": "2026-10-25"}, [], [], 0)
        self.assertFalse(n)
        self.assertIn("+1.0%", text)
        self.assertTrue(R.notify_decision(t, None, [{"name": "Joe Burrow", "from": 10000, "to": 15000, "pct": 50.0}], [], 0)[0])
        self.assertFalse(R.notify_decision(t, None, [{"name": "Base", "from": 100, "to": 300, "pct": 200.0}], [], 0)[0])
        self.assertTrue(R.notify_decision(t, None, [], [], 2)[0])


class Files(unittest.TestCase):
    def test_versions_from_listing_text_and_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp, "v.txt")
            f.write_text('- "eb-10-15006-05973-366546977670"  44 bytes  version 7  "/tmp/cv/in/cards/x.json"\n- "m-abc"  1,204 bytes  version 12  "/p"\n')
            self.assertEqual(R.load_versions(f), {"eb-10-15006-05973-366546977670": 7, "m-abc": 12})
            f.write_text('{"a": 3}')
            self.assertEqual(R.load_versions(f), {"a": 3})

    def test_export_roundtrip_and_write_batches(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp, "in", "cards")
            src.mkdir(parents=True)
            for i in range(60):
                body = card(guide={"id": "g", "subset": "Base Set"})
                body.pop("id")
                Path(src, f"c{i}.json").write_text(json.dumps(body))
            cards = R.load_docs(Path(tmp, "in"))
            for c in cards:
                c["_version"] = 3
            patches, summary, snap = R.run(cards, FakeClients(bulk={"g": BURROW_SALES}), TODAY)
            batches = R.write_outputs(Path(tmp, "out"), cards, patches, summary, snap)
            self.assertEqual([len(b) for b in batches], [50, 10])
            self.assertEqual(batches[0][0]["if_version"], 3)
            self.assertTrue(Path(batches[0][0]["file_path"]).exists())
            self.assertEqual(snap["value_cents"], 60 * 24000)


def tsale(title, price=250, date="2026-10-25", **kw):
    return {"title": title, "price": price, "date": date + "T03:00:00Z", "source": "ebay", "listing_type": "auction",
            "url": "https://showme.cards/" + str(abs(hash((title, price, date)))), **kw}


ACE = {"id": "ace", "status": "owned", "player": "Ace Bailey", "year": "2025-26", "set": "Topps Chrome", "number": "255",
       "parallel": "Teal Refractor", "serial": "/299", "title": "2025-26 Topps Chrome - Ace Bailey #255 Teal Refractor /299 (RC)"}
KOBE = {"id": "kobe", "status": "owned", "player": "Kobe Bryant", "year": "1996", "set": "Skybox Premium", "number": "55",
        "grader": "PSA", "grade": 8, "title": "1996 Skybox Premium Kobe Bryant #55"}
LAWRENCE = {"id": "tl", "status": "owned", "player": "Trevor Lawrence", "year": "2021", "set": "Panini Donruss Optic", "number": "201",
            "parallel": "Holo", "grader": "PSA", "grade": 10, "title": "2021 Donruss Optic Trevor Lawrence Holo #201 PSA 10"}
FLAGG = {"id": "cf", "status": "owned", "player": "Cooper Flagg", "year": "2025", "set": "Topps Chrome Silver Pack", "number": "TC-CF",
         "grader": "PSA", "grade": 10, "title": "2025 Topps Chrome Silver Pack Cooper Flagg #TC-CF"}
MCCARTHY = {"id": "jj", "status": "owned", "player": "J.J. McCarthy", "year": "2024", "set": "Panini Donruss Optic Preview",
            "number": "303", "parallel": "Red Wave", "grader": "PSA", "grade": 9,
            "title": "2024 Panini Donruss Optic Preview J.J. McCarthy Red Wave #303"}
RWB = {"id": "rwb", "status": "owned", "player": "Joe Burrow", "year": "2020", "set": "Panini Prizm", "number": "307",
       "parallel": "Red White & Blue", "title": "2020 Panini Prizm - Rookie Joe Burrow #307 Red White & Blue Prizm (RC)"}


class TitleSearch(unittest.TestCase):
    """Verdicts on real listing titles seen in CardSight's title search."""

    def verdict(self, title, c, **kw):
        return R.title_matches(tsale(title, **kw), c)[1]

    def test_same_card_accepted(self):
        self.assertIsNone(self.verdict("ACE BAILEY 2025-26 TOPPS CHROME #255 ROOKIE TEAL REFRACTOR RC 209/299 JAZZ", ACE))
        self.assertIsNone(self.verdict("1996-97 Skybox Premium - Kobe Bryant #55 (RC) PSA 8", KOBE))
        self.assertIsNone(self.verdict("2025-26 Topps Chrome Silver Pack - Cooper Flagg - #TC-CF - PSA 10 - Rookie Card!", FLAGG))
        self.assertIsNone(self.verdict("2024 PANINI DONRUSS OPTIC PREVIEW-RED WAVE #303 JJ MCCARTHY ROOKIE RC PSA 9", MCCARTHY))
        self.assertIsNone(self.verdict("Panini Donruss Optic 2021 Trevor Lawrence Jaguars RC Holo Prizm #201 PSA 10", LAWRENCE))
        self.assertIsNone(self.verdict("2020 Panini Prizm Joe Burrow RWB #307 RC Bengals", RWB))

    def test_other_cards_rejected(self):
        # an insert of the same player and parallel, with no card number in the title
        self.assertEqual(self.verdict("2025-26 Topps Chrome Ace Bailey Go Time RC Teal Refractor Rookie #/299 Jazz", ACE), "no number")
        self.assertEqual(self.verdict("1996-97 Skybox Premium Rubies #55 Kobe Bryant RC Rookie HOF PSA 8 NM-MT", KOBE), "other version")
        self.assertEqual(self.verdict("2021 Panini Donruss Optic TREVOR LAWRENCE #201 RC HOLO VARIATION PSA 10 GEM MINT", LAWRENCE), "other version")
        self.assertEqual(self.verdict("2021 Donruss Optic Trevor Lawrence Blue Scope Holo #201 PSA 10", LAWRENCE), "other version")
        self.assertEqual(self.verdict("2021 Panini Donruss Optic Trevor Lawrence Holo #201 PSA 9", LAWRENCE), "grade")
        self.assertEqual(self.verdict("2022 Panini Donruss Optic Trevor Lawrence Holo #201 PSA 10", LAWRENCE), "year")
        self.assertEqual(self.verdict("2021 Panini Donruss Optic Trevor Lawrence Holo #144 PSA 10", LAWRENCE), "number")
        self.assertEqual(self.verdict("2025-26 Topps Chrome Ace Bailey #255 Teal Refractor /299 PSA 10", ACE), "graded")
        self.assertEqual(self.verdict("2025-26 Topps Chrome Ace Bailey #255 Aqua Refractor /199", ACE), "parallel")
        self.assertEqual(self.verdict("2025-26 Topps Chrome Update Ace Bailey #255 Teal Refractor /299", ACE), "other product")
        self.assertEqual(self.verdict("Lot of 2 2025-26 Topps Chrome Ace Bailey #255 Teal Refractor /299", ACE), "not one card")
        self.assertEqual(self.verdict("2020 Panini Prizm Joe Burrow #307 Red White Blue RC Damaged corner", RWB), "not one card")
        self.assertEqual(self.verdict("2020 Panini Donruss Joe Burrow #151 Purple Shock RC PSA 10", card()), "set")  # paper, not Optic
        self.assertEqual(self.verdict("2020 Bowman Chrome Joe Burrow #151 Purple Shock RC PSA 10", card()), "set")

    def test_team_colors_are_not_a_parallel(self):
        love = {"player": "Jordan Love", "year": "2020", "set": "Panini Prizm", "number": "325", "title": "2020 Prizm Jordan Love #325"}
        self.assertIsNone(self.verdict("2020 Panini Prizm Jordan Love #325 RC Green Bay Packers", love))
        self.assertEqual(self.verdict("2020 Panini Prizm Jordan Love #325 RC Green Prizm Packers", love), "other version")

    def test_retro_year_and_short_season(self):
        self.assertIsNone(self.verdict("2025-26 Topps Cooper Flagg 1980-81 Chrome Rookie RC #TC-CF Mavericks PSA 10", FLAGG))
        mj = {"player": "Michael Jordan", "year": "1998", "set": "Upper Deck MJ23", "number": "M19", "parallel": "Quantum - Bronze",
              "serial": "2042/2300", "grader": "PSA", "grade": 8}
        self.assertIsNone(self.verdict("Michael Jordan 98/99 Upper Deck MJ23 Quantum Bronze #QM-M19 1483/2300 PSA 8", mj))

    def test_number_in_text(self):
        self.assertTrue(R.number_in_text("280", "2018 Prizm Luka RC 280 BGS 9.5"))
        self.assertTrue(R.number_in_text("TC-CF", "Flagg TC CF PSA 10"))
        self.assertFalse(R.number_in_text("99", "Klubnik green /99"))
        self.assertFalse(R.number_in_text("99", "Klubnik green 99/150"))

    def test_queries_skip_initials_and_go_loose(self):
        self.assertEqual(R.title_queries(MCCARTHY), ["McCarthy 2024 Donruss Optic 303 red wave PSA 9", "McCarthy Donruss Optic red wave"])

    def test_outlier_left_out(self):
        recs = [{"price": p} for p in (102.5, 120, 90, 7655.33, 108)]
        self.assertEqual([r["price"] for r in R.drop_outliers(recs)], [102.5, 120, 90, 108])


class TitleSearchRun(unittest.TestCase):
    THIN = pricing(psa10=[sale(250, "2026-10-28", "Purple Shock", title="2020 Panini Donruss Optic Joe Burrow Rated Rookie #151 Purple Shock PSA 10")])
    GOOD = [tsale("2020 Panini Donruss Optic Joe Burrow Purple Shock #151 RC PSA 10", 240, "2026-10-25"),
            tsale("Joe Burrow 2020 Optic Rated Rookie Purple Shock Prizm #151 PSA 10 Bengals", 260, "2026-10-18"),
            tsale("2020 Donruss Optic Joe Burrow #151 Purple Shock RC PSA 10", 230, "2026-10-11"),
            tsale("2020 Donruss Optic Joe Burrow Purple Shock Rated Rookie #151 PSA 10", 270, "2026-10-04")]
    JUNK = [tsale("2020 Donruss Optic Joe Burrow Purple Shock #151 PSA 9", 110),
            tsale("Lot of 2 2020 Donruss Optic Joe Burrow Purple Shock #151 PSA 10", 480),
            tsale("2021 Donruss Optic Joe Burrow Purple Shock #151 PSA 10", 90),
            tsale("2020 Donruss Optic Joe Burrow Purple Shock Auto #151 PSA 10", 900),
            # the catalog sale again, as title search returns it
            tsale("2020 Panini Donruss Optic Joe Burrow Rated Rookie #151 Purple Shock PSA 10", 250, "2026-10-28")]

    def test_thin_card_filled_from_sale_titles(self):
        cl = FakeClients(bulk={"g": self.THIN}, titles=self.GOOD + self.JUNK)
        patches, _, _ = R.run([card(guide={"id": "g", "subset": "Base Set"})], cl, TODAY)
        p = patches["c1"]
        self.assertEqual(p["value_n"], 5)  # 1 catalog + 4 by title, the repeat counted once
        self.assertEqual(p["value_cents"], 25000)  # 230 240 250 260 270, high and low dropped
        self.assertEqual(p["value_source"], "cardsight+titles")
        self.assertEqual(cl.title_queries, ["Joe Burrow 2020 Donruss Optic 151 purple shock PSA 10"])  # 5 known: no loose search

    def test_card_with_plenty_of_catalog_sales_skips_title_search(self):
        cl = FakeClients(bulk={"g": BURROW_SALES}, titles=self.GOOD)
        R.run([card(guide={"id": "g", "subset": "Base Set"})], cl, TODAY)
        self.assertEqual(cl.title_queries, [])

    def test_card_waiting_on_a_match_pick_is_not_searched(self):
        c = card(needs_review=True, review_reason=R.MATCH_REASONS[0], guide_candidates=[{"id": "a"}, {"id": "b"}],
                 guide_query=" | ".join(R.search_queries(card())))
        cl = FakeClients(titles=self.GOOD)
        R.run([c], cl, TODAY)
        self.assertEqual(cl.title_queries, [])

    def test_card_not_in_catalog_valued_by_titles(self):
        c = card(guide={"none": True, "auto": True}, guide_query=" | ".join(R.search_queries(card())))
        patches, _, _ = R.run([c], FakeClients(titles=self.GOOD), TODAY)
        self.assertEqual(patches["c1"]["value_cents"], 25000)
        self.assertEqual(patches["c1"]["value_n"], 4)

    def test_card_not_in_catalog_without_title_sales_is_left_alone(self):
        c = card(guide={"none": True, "auto": True}, guide_query=" | ".join(R.search_queries(card())))
        patches, _, _ = R.run([c], FakeClients(titles=self.JUNK[:-1]), TODAY)  # every one a different card or grade
        self.assertNotIn("c1", patches)


class Rivals(unittest.TestCase):
    LEBRON = {"type": "card", "id": "lj-box", "name": "LeBron James", "year": "2003", "releaseName": "Upper Deck LeBron James",
              "setName": "Base Set", "manufacturerName": "Upper Deck", "cardNumber": "19"}
    PB = {"type": "card", "id": "lj-pb", "name": "LeBron James", "year": "2003", "releaseName": "Upper Deck LeBron James",
          "setName": "Phenomenal Beginning Base Set", "manufacturerName": "Upper Deck", "cardNumber": "19"}
    OTHER = {"type": "card", "id": "lj-x", "name": "LeBron James", "year": "2003", "releaseName": "Topps Chrome",
             "setName": "Base Set", "manufacturerName": "Topps", "cardNumber": "111"}
    LATER = {"type": "card", "id": "lj-p", "name": "LeBron James", "year": "2012", "releaseName": "Panini Prizm",
             "setName": "Base Set", "manufacturerName": "Panini", "cardNumber": "19"}
    CARD = {"id": "lj", "status": "owned", "player": "LeBron James", "year": "2003", "set": "Upper Deck Box Set", "number": "19",
            "grader": "PSA", "grade": 9, "title": "2003 Upper Deck Box Set LeBron James #19"}

    def test_sister_product_words_learned(self):
        self.assertEqual(R.rival_words(self.LEBRON, [self.LEBRON, self.PB, self.OTHER, self.LATER]), ["beginning", "phenomenal"])
        # a word of the card's own set is never a rival, even when its catalog entry is missing
        self.assertEqual(R.rival_words({"id": "x", "name": "LeBron James", "cardNumber": "19", "year": "2003"}, [self.PB], "Phenomenal Beginning"), [])

    def test_sister_product_sale_rejected(self):
        c = {**self.CARD, "guide": {"id": "lj-box", "rivals": ["beginning", "phenomenal"]}}
        self.assertEqual(R.title_matches(tsale("2003 Upper Deck Phenomenal Beginning #19 LeBron James RC PSA 9"), c)[1], "other card")
        self.assertIsNone(R.title_matches(tsale("2003 Upper Deck LeBron James Box Set #19 PSA 9"), c)[1])

    def test_rivals_learned_once_before_title_search(self):
        c = {**self.CARD, "guide": {"id": "lj-box", "name": "LeBron James", "number": "19", "picked": True, "none": True}}
        sales = [tsale("2003 Upper Deck Phenomenal Beginning #19 LeBron James RC PSA 9", 35),
                 tsale("2003-04 Upper Deck LeBron James Phenomenal Beginning RC #19 Cavaliers PSA 9", 88)]
        cl = FakeClients(search=[self.LEBRON, self.PB], titles=sales)
        patches, _, _ = R.run([c], cl, TODAY)
        self.assertEqual(patches["lj"]["guide"]["rivals"], ["beginning", "phenomenal"])
        self.assertEqual(patches["lj"]["guide"]["none"], {"__delete__": True})  # the old "no match" goes
        self.assertNotIn("value_cents", patches["lj"])  # both sales were the other box set
        self.assertEqual(cl.queries, ["LeBron James 19"])

    def test_number_from_the_catalog_match_when_left_blank(self):
        c = card(number=None, guide={"id": "g", "number": "151", "subset": "Base Set", "rivals": []})
        cl = FakeClients(bulk={"g": TitleSearchRun.THIN}, titles=TitleSearchRun.GOOD)
        patches, _, _ = R.run([c], cl, TODAY)
        self.assertEqual(patches["c1"]["value_n"], 5)
        self.assertIn("151", cl.title_queries[0])

    def test_no_title_search_without_a_card_number(self):
        cl = FakeClients(titles=TitleSearchRun.GOOD)
        R.run([card(number=None, guide={"none": True, "auto": True}, guide_query="x")], cl, TODAY)
        self.assertEqual(cl.title_queries, [])

    def test_notes_in_brackets_left_out(self):
        self.assertEqual(R.clean_field("Cracked Ice (appears to be a Cracked Ice style prismatic parallel)"), "Cracked Ice")
        self.assertEqual(R.clean_field("Foil (Topps logo p"), "Foil")
        clark = {"player": "Caitlin Clark", "year": "2024", "set": "Panini Prizm WNBA", "number": "145",
                 "parallel": "Cracked Ice (appears to be a Cracked Ice style prismatic parallel)"}
        self.assertEqual(R.title_query(clark), "Caitlin Clark 2024 Prizm 145 cracked ice")

    def test_retry_that_lands_in_review_drops_old_no_match(self):
        c = card(guide={"none": True, "auto": True}, guide_query="old", year=None, number=None, parallel=None,
                 title="Joe Burrow Donruss Optic rookie")
        patches, _, _ = R.run([c], FakeClients(), TODAY)
        self.assertTrue(patches["c1"]["needs_review"])
        self.assertIsNone(patches["c1"]["guide"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
