#!/usr/bin/env python3
"""Card Vault weekly refresh: PSA cert checks, sales data matching, values.

Reads the card documents the scheduled session exported from the artifact
database, calls the CardSight and PSA APIs, and writes patch files plus a list
of database writes for the session to apply. Standard library only.

    python3 -I refresh.py --cards DIR --out DIR [--versions FILE] [--prev-snapshot FILE] [--today YYYY-MM-DD]

Env: CARDSIGHT_API_KEY (cardsight.ai), PSA_TOKEN (PSA public API bearer token),
optional CARDSIGHT_MAX_CALLS (per run, default 150) and CARDSIGHT_HOST.
Either key may be missing; that step is skipped and reported.

Value rule: completed auction sales of the same card, parallel and grade in
the last 6 months, most recent 10, median (single high and low dropped at 5+).
No sales means the last value is kept and marked stale, never guessed.
"""

import argparse
import datetime as dt
import json
import os
import re
import statistics
import sys
import time
import unicodedata
import urllib.error
import zipfile
import io
import urllib.parse
import urllib.request
from pathlib import Path

CS_HOST = os.environ.get("CARDSIGHT_HOST", "https://api.cardsight.ai").rstrip("/")
CS_MAX_CALLS = int(os.environ.get("CARDSIGHT_MAX_CALLS", "150"))
PSA_URL = "https://api.psacard.com/publicapi/cert/GetByCertNumber/"
PSA_DAILY_BUDGET = 90  # PSA allows 100 calls a day on a free account; keep headroom.
HISTORY_LEN = 26  # about six months of weekly points
AUTO_ACCEPT = 0.8  # match confidence at or above this is taken without review
BATCH_MAX = 50  # ArtifactData batch limit
BULK_SIZE = 100  # cards per CardSight bulk pricing call
BULK_LIMIT = 50  # most recent sales per card in a bulk call (CardSight's max)
DEEP_MAX = 40  # single-card pulls per run when bulk rows were crowded out
DEEP_LIMIT = 100  # rows a single-card pull returns (cs_card's limit)
WINDOW_DAYS = 183
COMP_N = 10
MATCH_REASONS = ("Pick the sales data match", "No sales data match found")
PARALLEL_REASON = "Which version is this?"
GRADE_REASON = "Add the grade for this slab"


class Unauthorized(Exception):
    pass


class OutOfBudget(Exception):
    pass


# ---------------------------------------------------------------- text

STOP = {"the", "and", "of", "card", "cards", "rc", "rookie", "gem", "mt", "mint", "nm", "psa", "bgs", "sgc", "cgc",
        "sp", "ssp", "hot", "invest", "w", "with", "new", "a"}
SPORTS = {"baseball", "basketball", "football", "hockey", "soccer", "racing", "wrestling", "mma", "golf"}
GRADE_WORDS = re.compile(r"\b(PSA|BGS|SGC|CGC|BVG|CSG)\s*(GEM\s*(MT|MINT)\s*)?\d+(\.5)?\b|\bGEM\s*(MT|MINT)\b|\bPOP\s*\d+\b", re.I)
GENERIC_SET = {"base", "set", "parallels", "parallel", "insert", "inserts"}
MAKERS = {"panini", "topps", "bowman", "upper", "deck", "leaf", "fleer", "donruss", "the"}
AUTO_WORDS = {"auto", "autos", "autograph", "autographs", "autographed", "signature", "signatures", "signed", "ink"}
# Words that mark a parallel or finish in a listing title. A sale whose title
# carries one the card itself doesn't is a different version of the card.
PAR_WORDS = {"pink", "purple", "blue", "red", "green", "gold", "orange", "silver", "black", "white", "bronze", "teal",
             "aqua", "yellow", "holo", "shock", "wave", "scope", "velocity", "pulsar", "mojo", "refractor", "xfractor",
             "fractor", "ice", "lazer", "laser", "hyper", "disco", "camo", "tiger", "zebra", "snakeskin", "cracked",
             "sparkle", "shimmer", "neon", "pulse", "raywave", "logofractor", "sepia", "negative", "atomic",
             "superfractor", "prizms", "foil", "rainbow", "mosaic", "fluorescent", "lime", "magenta", "vinyl", "fotl",
             "printing", "plate", "geometric", "speckle", "ruby", "sapphire", "emerald", "platinum", "mirror", "variation"}
SERIAL = re.compile(r"(?:#\s*'?d?\s*|\b\d{1,4}\s*|\s)/\s*(\d{1,4})\b", re.I)


def norm(s):
    s = unicodedata.normalize("NFKD", str(s or ""))
    return "".join(ch for ch in s if not unicodedata.combining(ch)).lower()


def toks(s):
    s = re.sub(r"[^a-z0-9#/\s-]", " ", norm(s))
    out = []
    for t in re.split(r"[\s-]+", s):
        t = t.lstrip("#")
        if t and t not in STOP:
            out.append(t)
    return out


def card_text(c):
    return " ".join(str(c.get(k) or "") for k in ("title", "player", "year", "set", "parallel", "number"))


def clean_field(s):
    """A set or parallel without notes in brackets, as photo fill-in sometimes
    adds ("Cracked Ice (appears to be a prismatic parallel)")."""
    s = re.sub(r"\([^)]*\)?|\[[^\]]*\]?", " ", str(s or ""))
    return re.sub(r"\s+", " ", s).strip()


def set_core(c):
    """The card's set without years or grading words, e.g. "Panini Donruss Optic"."""
    words = [w for w in re.split(r"\s+", clean_field(c.get("set"))) if w and not re.fullmatch(r"\d{4}(-\d{2})?", w)]
    return " ".join(words)


def search_queries(c):
    """Short searches first: CardSight wants every word to match, so long
    queries come back empty. Scoring picks the card from the results."""
    player = c.get("player")
    if not player:
        q = GRADE_WORDS.sub(" ", c.get("title") or "")
        q = re.sub(r"\b(rc|rookie|ssp|sp|hot|invest|lot)\b|#?\s*\d*\s*/\s*\d+", " ", q, flags=re.I)
        return [re.sub(r"\s+", " ", q).strip()]
    core = set_core(c)
    key = next((w for w in core.split() if len(w) > 2 and w.lower() not in MAKERS), "")
    # Player plus card number is the sharpest search: a popular base rookie
    # otherwise sinks under dozens of newer inserts for the same player.
    num = re.sub(r"^#", "", str(c.get("number") or "")).strip()
    qs = ([f"{player} {num}"] if num else []) + [f"{player} {core}".strip(), f"{player} {key}".strip(), player]
    seen, out = set(), []
    for q in qs:
        if q.lower() not in seen:
            seen.add(q.lower())
            out.append(q)
    return out


build_query = lambda c: search_queries(c)[0]


def _has(tokens, t):
    return t in tokens or (len(t) >= 4 and any(x.startswith(t) or t.startswith(x) for x in tokens if len(x) >= 4))


def _norm_num(n):
    return re.sub(r"[\s#]", "", norm(n))


def score_candidate(c, r):
    """0..1 confidence that a CardSight catalog card is this card. Weighs the
    player, card number, year, release (Prizm vs Prizm Black, Chrome vs Chrome
    Sapphire), sub-set (Base Set vs Rookie Autographs) and whether it's an auto."""
    have = set(toks(card_text(c)))
    if not have:
        return 0.0
    name_t = toks(r.get("name"))
    s = 0.0
    player = toks(c.get("player") or "")
    if player:
        if all(_has(name_t, p) for p in player):
            s += 0.35
        elif _has(name_t, player[-1]):
            s += 0.2
        else:
            return 0.1
    else:
        s += 0.35 * (len(set(name_t) & have) / max(1, len(set(name_t))))

    cap = 1.0  # close calls are capped below auto-accept so David picks
    num, cnum = _norm_num(c.get("number")), _norm_num(r.get("cardNumber"))
    if num and re.search(r"#?\s*" + re.escape(str(c.get("number") or "")) + r"/\d+", str(c.get("title") or "")):
        num = ""  # "#7/99" is a serial number, not the card number
    if num and cnum:
        if num == cnum:
            s += 0.3
        elif re.sub(r"\D", "", num) and re.sub(r"\D", "", num) == re.sub(r"\D", "", cnum):
            s += 0.15
            cap = min(cap, 0.75)  # 301 vs 301V: a variation is a different card
        else:
            s -= 0.35

    year, cyear = (c.get("year") or "")[:4], str(r.get("year") or "")[:4]
    if year and cyear:
        s += 0.15 if year == cyear else -0.35

    rel = {t for t in toks(r.get("releaseName")) if t not in SPORTS and t not in MAKERS and not re.fullmatch(r"\d{4}", t)}
    if rel:
        hit = {t for t in rel if _has(have, t)}
        s += 0.2 * len(hit) / len(rel)
        if rel - hit:
            s -= 0.1
            cap = min(cap, 0.7)  # Contenders vs Contenders Optic, Chrome vs Chrome Sapphire
    cand = set(toks(f"{r.get('releaseName')} {r.get('setName')} {r.get('manufacturerName')} {r.get('parallelName') or ''}"))
    missing = {t for t in toks(set_core(c)) if len(t) > 1 and t not in SPORTS and not _has(cand, t)}
    if missing:
        s -= min(0.3, 0.12 * len(missing))
        cap = min(cap, 0.75)  # Prizm Black vs Prizm, Gold Label vs Topps

    sub = [t for t in toks(r.get("setName")) if t not in GENERIC_SET]
    if sub:
        hit = [t for t in set(sub) if _has(have, t) or (t in AUTO_WORDS and have & AUTO_WORDS)]
        f = len(hit) / len(set(sub))
        s += 0.15 * f - 0.15 * (1 - f)

    r_auto = bool(set(toks(f"{r.get('setName')} {r.get('parallelName') or ''}")) & AUTO_WORDS)
    c_auto = bool(c.get("auto")) or bool(have & AUTO_WORDS)
    if r_auto and not c_auto:
        s -= 0.3
    elif c_auto and not r_auto:
        s -= 0.15
    # Rounded so .35 + .3 + .15 is 0.8, not 0.7999..., at the auto-accept line.
    return round(max(0.0, min(cap, s)), 4)


# ---------------------------------------------------------------- comps

def _company(name):
    n = (name or "").upper()
    if n in ("BVG", "BECKETT") or n.startswith("BECKETT") or n.startswith("BGS"):
        return "BGS"
    if n in ("CSG",) or n.startswith("CGC"):
        return "CGC"
    return n.split()[0] if n else n


def _num(v):
    m = re.search(r"(\d+(?:\.\d+)?)\s*$", str(v or ""))
    return float(m.group(1)) if m else None


def grade_label(grader, grade):
    if not grader or grade is None:
        return "raw"
    return f"{grader} {float(grade):g}"


def section(data, grader, grade):
    """(records, grade_id) for the card's grade: raw for raw, else company+grade."""
    if not grader or grade is None:
        return (data.get("raw") or {}).get("records") or [], None
    want_c, want_g = _company(grader), float(grade)
    for comp in data.get("graded") or []:
        if _company(comp.get("company_name")) != want_c:
            continue
        for g in comp.get("grades") or []:
            if _num(g.get("grade_value")) == want_g:
                return g.get("records") or [], g.get("grade_id")
    return [], None


def _ptoks(s):
    return {t for t in toks(s) if not re.fullmatch(r"/?\d+|d|numbered|parallel|prizm|refractor|variation", t)}


def same_parallel(rec_name, want):
    """True when a sale's parallel is the card's parallel. Base on both sides
    matches; otherwise the names must cover each other closely."""
    base = lambda n: not n or str(n).strip().lower() in ("base", "base card")
    if base(want):
        return base(rec_name)
    if base(rec_name):
        return False
    a, b = _ptoks(want), _ptoks(rec_name)
    if not a or not b:
        return norm(want).strip() == norm(rec_name).strip()
    common = len(a & b)
    return common / len(a) >= 0.75 and common / len(b) >= 0.6


def effective_parallel(card):
    """The card's parallel, or None when the "parallel" the card carries is
    really its sub-set ("Rookie of the Year Autographs", "Big Numbers")."""
    p = clean_field(card.get("parallel"))
    if not p:
        return None
    g = card.get("guide") or {}
    subset = set(toks(f"{g.get('subset') or ''} {g.get('release') or ''} {card.get('set') or ''}"))
    pt = _ptoks(p) - AUTO_WORDS - {"rookie", "rookies", "patch", "patches", "jersey", "insert"}
    if not pt or all(_has(subset, t) for t in pt):
        return None
    return p


def serial_of(text):
    return {m for m in SERIAL.findall(str(text or ""))}


def sale_matches(rec, card, want):
    """Does this sale belong to this exact version of the card? CardSight's own
    parallel tag is incomplete, so the listing title is checked as well."""
    title = rec.get("title") or ""
    tt = set(toks(title))
    g = card.get("guide") or {}
    own = set(toks(f"{card_text(card)} {g.get('subset') or ''} {g.get('release') or ''} {g.get('name') or ''}"))
    want_serial = serial_of(card.get("serial")) or serial_of(card.get("title"))
    rec_serial = serial_of(title)
    if rec_serial and not (rec_serial & want_serial):
        return False
    if not want:
        if rec.get("parallel_name"):
            return False
    else:
        tagged = rec.get("parallel_name") and same_parallel(rec.get("parallel_name"), want)
        named = all(_has(tt, t) for t in _ptoks(want))
        if not (tagged or named):
            return False
    if (tt & PAR_WORDS) - own:
        return False
    # "Signature Class" or "Rookie Ink" in a set name says nothing about an auto.
    set_words = set(toks(f"{set_core(card)} {g.get('subset') or ''} {g.get('release') or ''}"))
    autos = AUTO_WORDS - {"ink"} - set_words
    c_auto = bool(card.get("auto")) or bool(set(toks(card.get("title"))) & autos)
    t_auto = bool(tt & autos)
    if c_auto != t_auto:
        return False  # an auto and the same card without one sell for different money
    if not card.get("grader") and GRADE_WORDS.search(title):
        return False  # a slab listed in the raw section
    return True


def _date(r):
    try:
        return dt.date.fromisoformat(str(r.get("date") or "")[:10])
    except ValueError:
        return None


def merge_pricing(a, b):
    """Union of two pricing responses for one card, each sale once."""
    a, b = a or {}, b or {}
    key = lambda r: (r.get("url"), r.get("price"), r.get("date"), r.get("title"))
    def merged(x, y):
        seen, out = set(), []
        for r in (x or []) + (y or []):
            if key(r) not in seen:
                seen.add(key(r))
                out.append(r)
        return out
    out = {**a, "raw": {**(a.get("raw") or {}), "records": merged((a.get("raw") or {}).get("records"), (b.get("raw") or {}).get("records"))}}
    comps = {}
    for comp in (a.get("graded") or []) + (b.get("graded") or []):
        c = comps.setdefault(comp.get("company_name"), {**comp, "grades": {}})
        for g in comp.get("grades") or []:
            have = c["grades"].get(g.get("grade_value"))
            c["grades"][g.get("grade_value")] = {**g, "records": merged((have or {}).get("records"), g.get("records"))}
    out["graded"] = [{**c, "grades": list(c["grades"].values())} for c in comps.values()]
    return out


def total_rows(data):
    n = len((data.get("raw") or {}).get("records") or [])
    for comp in data.get("graded") or []:
        for g in comp.get("grades") or []:
            n += len(g.get("records") or [])
    return n


def _completed(recs, today, window_days):
    cutoff = dt.date.fromisoformat(today) - dt.timedelta(days=window_days)
    return [r for r in recs
            if r.get("listing_type") in (None, "auction") and isinstance(r.get("price"), (int, float)) and r["price"] > 0
            and (_date(r) or cutoff) >= cutoff]


def drop_outliers(match):
    """Leave out a sale at more than 3x, or under a third of, the median of the
    others: a mislabeled listing (a Rubies parallel titled as the base card)
    rather than the market. Needs 4 sales to judge."""
    if len(match) < 4:
        return match
    keep = []
    for i, r in enumerate(match):
        rest = statistics.median(x["price"] for j, x in enumerate(match) if j != i)
        if rest / 3 <= r["price"] <= rest * 3:
            keep.append(r)
    return keep


def comp_value(data, card, today, window_days=WINDOW_DAYS, extra=()):
    """Value from completed auction sales. Returns a dict: cents (or None), n,
    basis, last_sale, thin, grade_id, parallel_id, parallel_options, records.
    `extra` adds sales found by title search (already checked by title_matches)."""
    recs, grade_id = section(data or {}, card.get("grader"), card.get("grade"))
    want = effective_parallel(card)
    out = {"cents": None, "n": 0, "basis": None, "last_sale": None, "thin": False, "grade_id": grade_id,
           "parallel_id": None, "parallel_options": None, "records": []}
    match = [r for r in _completed(recs, today, window_days) if sale_matches(r, card, want)]
    seen = {_sale_key(r) for r in match}
    for r in _completed(list(extra), today, window_days):
        if _sale_key(r) not in seen:
            seen.add(_sale_key(r))
            match.append(r)
    out["records"] = match
    if not match:
        return out
    match.sort(key=lambda r: _date(r) or dt.date.min, reverse=True)
    match = drop_outliers(match[:COMP_N])
    prices = sorted(r["price"] for r in match)
    if len(prices) >= 5:
        prices = prices[1:-1]
    med = statistics.median(prices)
    last = max((_date(r) for r in match if _date(r)), default=None)
    n = len(match)
    out.update({
        "cents": int(round(med * 100)), "n": n, "thin": n < 3,
        "last_sale": last.isoformat() if last else None,
        "parallel_id": next((r.get("parallel_id") for r in match if r.get("parallel_id")), None),
        "basis": f"Median of {n} sold, {grade_label(card.get('grader'), card.get('grade'))}"
                 + (", last 12 months" if window_days > WINDOW_DAYS else "")
                 + (f", last sale {last.strftime('%b')} {last.day}" if last else ""),
        "sales": [{"price": r["price"], "date": str(r.get("date") or "")[:10], "url": r.get("url")} for r in match[:5]],
        "title_n": sum(1 for r in match if r.get("via") == "title"),
    })
    return out


# ---------------------------------------------------------------- title search
# CardSight's title search returns completed auction sales by listing title,
# including many it never tied to a catalog card. Used when the catalog finds
# too few sales. A sale counts only when its title agrees with the card on
# player, year, product, number, version, serial, auto and grade.

TITLE_MIN_N = 5  # catalog sales below this trigger a title search
TITLE_MAX = 60  # title searches per run
PRODUCT_WORDS = {"optic", "prizm", "mosaic", "select", "chrome", "finest", "contenders", "phoenix", "certified", "origins",
                 "obsidian", "spectra", "immaculate", "flawless", "national", "treasures", "sapphire", "platinum",
                 "heritage", "stadium", "cosmic", "draft", "update", "absolute", "illusions", "chronicles", "luminance",
                 "zenith", "prestige", "score", "playbook", "elite", "crown", "royale", "revolution", "hoops", "noir",
                 "clearly", "donruss", "bowman", "topps", "fleer", "skybox", "leaf", "upper", "ud", "exquisite", "encore",
                 "ultra", "metal", "dynasty", "tribute", "museum", "sterling", "inception", "definitive", "ginter",
                 "archives", "triple", "threads", "transcendent", "stellar", "university", "u", "sage", "wild", "panini",
                 "updates"}
# Panini products whose parallels are named "... Prizm" in listings.
PRIZM_FINISH = {"optic", "mosaic", "select", "phoenix", "chronicles", "obsidian", "spectra", "illusions", "certified",
                "contenders", "origins", "absolute", "luminance", "zenith"}
# Set-name words that describe how a card was sold, not which card it is.
SET_OPTIONAL = {"panini", "the", "set", "base", "edition", "series", "silver", "pack", "packs", "box", "hobby", "retail",
                "blaster", "mega", "football", "basketball", "baseball", "soccer", "hockey"}
# Version words beyond PAR_WORDS: a title with one the card lacks is another card.
TITLE_VERSION_WORDS = PAR_WORDS | {"press", "proof", "stained", "glass", "genesis", "nebula", "galaxy", "dragon", "scale",
                                   "kaboom", "downtown", "die", "cut", "canvas", "ssp", "lava", "reptilian", "checkerboard",
                                   "pearl", "aurora", "choice", "fast", "break", "lunar", "galactic", "photon", "swirl",
                                   "flash", "error", "parallel", "numbered", "jersey", "patch", "relic", "booklet",
                                   "dual", "rpa", "short", "rubies", "gems", "pmg", "members", "credentials",
                                   "masterpiece", "artist", "medallion", "electric", "holofoil", "row", "spectrum",
                                   "essential", "credential", "exclusive", "proof"}
# Team and nickname phrases whose colors are not a parallel ("Green Bay", "Red Sox").
TEAM_PHRASES = re.compile(r"\b(green bay|red sox|white sox|blue jays|red wings|blue jackets|blue devils|red raiders"
                          r"|red storm|black knights|black mamba|golden state|blue hens|green bay packers)\b")
NOT_ONE_CARD = re.compile(r"\blots?\b|\bx\s?[2-9]\b|\b[2-9]\s?x\b|\(\s*[2-9]\s*\)|you pick|pick your|\bbreaks?\b|\bspot\b"
                          r"|reprint|\brp\b|custom|digital|complete set|team set|\bset of\b|\bsealed\b|\bblaster\b"
                          r"|hobby box|\bdamaged?\b|\bcrease|\bas is\b|\bread\b|\bpoor\b|\bwear\b|off[\s-]?cent|\bmiscut"
                          r"|\bstain\b|\bdings?\b|\bdinged\b", re.I)
GRADE_IN_TITLE = re.compile(r"\b(PSA|BGS|BECKETT|BVG|SGC|CGC|CSG|TAG|HGA)\s*(?:GEM\s*(?:MT|MINT)\s*|MINT\s*|NM-?MT\s*|NM\s*|PRISTINE\s*)?"
                            r"(10|[1-9](?:\.5)?)\b", re.I)
SLAB_WORDS = re.compile(r"\b(PSA|BGS|BVG|SGC|CGC|CSG|BECKETT|HGA|graded|slabbed|slab)\b", re.I)
SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}


def _title_norm(s):
    s = norm(s)
    s = re.sub(r"\bx[\s-]?fractor", "xfractor", s)
    s = re.sub(r"\bray[\s-]?wave", "raywave", s)
    s = re.sub(r"\blogo[\s-]?fractor", "logofractor", s)
    s = re.sub(r"\brwb\b", "red white blue", s)
    s = re.sub(r"\b(19|20)(\d{2})\s*/\s*(\d{2})\b", r"\1\2-\3", s)  # 2025/26 is a season, not a serial
    s = re.sub(r"(?<![\d/#])'?(\d{2})\s*[/-]\s*(\d{2})(?![\d/])",  # 98/99 or '24-25: a season in short form
               lambda m: (f"{'19' if int(m.group(1)) >= 50 else '20'}{m.group(1)}-{m.group(2)}"
                          if (int(m.group(1)) + 1) % 100 == int(m.group(2)) else m.group(0)), s)
    return TEAM_PHRASES.sub(" ", s)


def _ttoks(s):
    return set(toks(_title_norm(s)))


def _grader(name):
    n = (name or "").upper()
    return {"BECKETT": "BGS", "BVG": "BGS", "CSG": "CGC"}.get(n, n)


def _num_key(n):
    k = re.sub(r"[^a-z0-9]", "", norm(n))
    return k.lstrip("0") or k


def title_numbers(title):
    """Card numbers a title states with '#', leaving out serials like #07/99."""
    out = set()
    for m in re.finditer(r"#\s*([A-Za-z0-9]+(?:-[A-Za-z0-9]+)*)", title or ""):
        if not title[m.end():].lstrip().startswith("/"):
            out.add(_num_key(m.group(1)))
    return out


def number_in_text(number, title):
    """Is the card number written in the title, with or without '#' ("RC 280",
    "TC CF")? A serial such as /99 or 99/150 doesn't count."""
    groups = re.findall(r"[a-z]+|\d+", norm(number))
    if not groups:
        return False
    pat = r"[\s-]*".join(("0*" + g) if g.isdigit() else g for g in groups)
    return bool(re.search(r"(?<![a-z0-9/])#?\s*" + pat + r"(?![a-z0-9])(?!\s*/)", norm(title)))


def _names_ok(tt, player):
    for person in re.split(r"\s*(?:/|&|\band\b)\s*", player):
        parts = [t for t in toks(person) if t not in SUFFIXES]
        if not parts:
            continue
        if not _has(tt, parts[-1]):
            return False
        first = parts[:-1]
        if first and all(len(t) == 1 for t in first):
            if "".join(first) not in tt and not all(t in tt for t in first):
                return False
        elif first and not _has(tt, first[0]):
            return False
    return True


def card_number(card):
    """The card's number, or the confirmed catalog card's when it was left blank."""
    g = card.get("guide") or {}
    return card.get("number") or (g.get("number") if g.get("id") else None)


def title_matches(rec, card):
    """(ok, reason): does a title search sale belong to this exact card?"""
    title = rec.get("title") or ""
    if not title or NOT_ONE_CARD.search(title):
        return False, "not one card"
    tt = _ttoks(title)
    if not card.get("player") or not _names_ok(tt, card["player"]):
        return False, "player"
    g = card.get("guide") or {}
    own_text = f"{card_text(card)} {g.get('subset') or ''} {g.get('release') or ''}"
    own = _ttoks(own_text)
    for a, b in (({"upper", "deck"}, {"ud"}), ({"u"}, {"university"}), ({"university"}, {"u"}), ({"nscc"}, {"national", "convention"})):
        if a <= own:
            own |= b
    # Year: the card's year must be there, and no other year
    year = str(card.get("year") or "")[:4]
    if not year:
        return False, "no year on card"
    t_years = set(re.findall(r"\b(19[4-9]\d|20[0-4]\d)\b", _title_norm(title)))
    allowed = {year, str(int(year) + 1)} | set(re.findall(r"\b(19[4-9]\d|20[0-4]\d)\b", _title_norm(own_text)))
    # Years well before the card's are retro designs ("1980-81 Topps Chrome"), not its year.
    if year not in t_years or any(int(y) > int(year) - 4 for y in t_years - allowed):
        return False, "year"
    # Product: the product words of the card's set (Donruss, Optic, Chrome) all
    # there, and no product the card isn't
    alias = {"u": {"u", "university"}, "university": {"u", "university"}, "upper": {"upper", "ud"}, "deck": {"deck", "ud"},
             "nscc": {"nscc", "national"}}
    set_t = _ttoks(set_core(card)) - SET_OPTIONAL
    if set_t - MAKERS:
        set_t -= MAKERS  # "2020 Optic" is Donruss Optic; a wrong brand fails the product check below
    if any(not (alias.get(t, {t}) & tt) and not _has(tt, t) for t in set_t):
        return False, "set"
    other = (tt & PRODUCT_WORDS) - own
    if own & PRIZM_FINISH:
        other.discard("prizm")  # "Holo Prizm", "Red Wave Prizm": Panini's name for the finish
    if other:
        return False, "other product"
    # Card number
    num = _num_key(card_number(card) or "")
    nums = title_numbers(title)
    prefixed = lambda n: len(num) >= 2 and re.search(r"\d", num) and n.endswith(num) and n[:-len(num)].isalpha()
    if num and nums and num not in nums and not any(prefixed(n) for n in nums):
        return False, "number"
    # A title without the card's number may be an insert of the same player and
    # parallel ("Go Time Teal Refractor"), so the number has to be there.
    if num and not (num in nums or any(prefixed(n) for n in nums) or number_in_text(card_number(card), title)):
        return False, "no number"
    mc = rec.get("matched_card") or {}
    if num and mc.get("number") and _num_key(mc["number"]) != num:
        return False, "number"
    if (set(g.get("rivals") or []) - own) & tt:
        return False, "other card"
    if g.get("id") and mc.get("card_id") and str(mc["card_id"]) != str(g["id"]):
        # CardSight tied the sale to another catalog card. It lists some cards
        # twice, so only a card that differs in number, year or set counts.
        ms = mc.get("set") or {}
        same = (_num_key(mc.get("number") or "") == _num_key(g.get("number") or "")
                and str(ms.get("year") or "")[:4] == str(g.get("year") or "")[:4]
                and norm(ms.get("release") or "") == norm(g.get("release") or "")
                and norm(ms.get("name") or "") == norm(g.get("subset") or ""))
        if not same:
            return False, "other card"
    # Version: the card's parallel words present, no version word it lacks
    want = effective_parallel(card)
    if want and any(not _has(tt, t) for t in _ptoks(_title_norm(want)) - {"base"}):
        return False, "parallel"
    # "Refractor" and "Variation" are versions of their own: required when the card is one
    want_t = set(toks(_title_norm(f"{want or ''} {card.get('title') or ''}")))
    if "refractor" in want_t and not ({"refractor", "refractors", "ref", "refrac"} & tt):
        return False, "parallel"
    if "variation" in want_t and not ({"variation", "variations", "var"} & tt):
        return False, "parallel"
    singular = lambda ts: ts | {t[:-3] + "y" for t in ts if t.endswith("ies")} | {t[:-1] for t in ts if t.endswith("s")}
    if (singular(tt) & TITLE_VERSION_WORDS) - singular(own):
        return False, "other version"
    # Serial numbering
    want_s = serial_of(card.get("serial")) | serial_of(card.get("title"))
    rec_s = serial_of(_title_norm(title))
    if rec_s and not (rec_s & want_s):
        return False, "serial"
    # Auto
    set_words = set(toks(f"{set_core(card)} {g.get('subset') or ''} {g.get('release') or ''}"))
    autos = (AUTO_WORDS | {"rpa"}) - {"ink"} - set_words
    c_auto = bool(card.get("auto")) or bool(set(toks(card.get("title"))) & autos)
    if c_auto != bool(tt & autos):
        return False, "auto"
    # Grade
    grades = {(_grader(m.group(1)), float(m.group(2))) for m in GRADE_IN_TITLE.finditer(title)}
    rg = rec.get("grade") or {}
    if card.get("grader"):
        want_g = (_grader(_company(card["grader"])), float(card["grade"])) if card.get("grade") is not None else None
        if not want_g or want_g not in grades or len(grades) > 1:
            return False, "grade"
        if rg.get("company_name") and (_company(rg["company_name"]), _num(rg.get("grade_value"))) != want_g:
            return False, "grade"
    elif grades or SLAB_WORDS.search(title) or rg.get("company_name"):
        return False, "graded"
    return True, None


def _query_name(player, last_only=False):
    """Searchable name: initials ("J.J.") and suffixes left out, as titles vary."""
    out = []
    for person in re.split(r"\s*(?:/|&|\band\b)\s*", player or ""):
        parts = [p for p in re.split(r"\s+", person.strip()) if p and norm(p).strip(".") not in SUFFIXES]
        parts = [p for i, p in enumerate(parts) if i == len(parts) - 1 or len(re.sub(r"[^a-z]", "", norm(p))) > 2]
        out += parts[-1:] if last_only else parts
    return " ".join(out)


def title_queries(card):
    """Searches for a card's sales. The search wants every word in a title, so
    only words title_matches requires anyway. The strict one first (name, year,
    product, number, version, grade); a looser one for thin cards (last name,
    product, version)."""
    product = [w for w in re.split(r"\s+", set_core(card)) if _ttoks(w) & PRODUCT_WORDS and norm(w) != "panini"]
    want = effective_parallel(card)
    version = [w for w in re.split(r"[\s/&()-]+", _title_norm(want)) if w and _ptoks(w) - {"base"} - AUTO_WORDS] if want else []
    grade = [grade_label(_company(card["grader"]), card["grade"])] if card.get("grader") and card.get("grade") is not None else []
    strict = [_query_name(card.get("player")), str(card.get("year") or "")[:4]] + product + [card_number(card)] + version + grade
    loose = [_query_name(card.get("player"), last_only=True)] + product[:2] + version
    qs = []
    for words in (strict, loose):
        q = re.sub(r"\s+", " ", " ".join(str(w) for w in words if w)).strip()[:300]
        if q and q not in qs:
            qs.append(q)
    return qs


title_query = lambda card: title_queries(card)[0]


def title_sales(results, card):
    """Title search results that are this card's completed auction sales."""
    return [r for r in results or [] if r.get("listing_type") == "auction" and title_matches(r, card)[0]]


def title_search_ok(card):
    """Search titles only for a card whose identity is settled: a player and a
    year, and no open question about which catalog card it is."""
    return bool(card.get("player") and card.get("year") and card_number(card)) and not (
        card.get("needs_review") and card.get("review_reason") in MATCH_REASONS)


def title_search(card, cl, left, notes, state, have=()):
    """Completed auction sales of this card found by title: the strict search,
    then the loose one while fewer than TITLE_MIN_N sales are known (counting
    `have`, the catalog's). Returns (sales, calls)."""
    found, used = [], 0
    seen = {_sale_key(r) for r in have}
    for q in title_queries(card):
        if used >= left or len(seen) >= TITLE_MIN_N:
            break
        try:
            res = cl.cs_title_search(q)
        except Unauthorized:
            state["cs_off"] = "cardsight_unauthorized"
            break
        except OutOfBudget:
            notes.append("cardsight_budget")
            break
        except Exception as e:
            notes.append(f"cardsight_error:{type(e).__name__}")
            break
        used += 1
        for r in title_sales(res, card):
            if _sale_key(r) not in seen:
                seen.add(_sale_key(r))
                found.append({**r, "via": "title"})
    return found, used


def _sale_key(r):
    return (round(float(r.get("price") or 0), 2), str(r.get("date") or "")[:10], " ".join(toks(r.get("title"))))


# ---------------------------------------------------------------- http

def http_json(url, headers=None, body=None, tries=3):
    data = json.dumps(body).encode() if body is not None else None
    h = {"User-Agent": "card-vault/2", "Accept": "application/json", **(headers or {})}
    if data is not None:
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=h, method="POST" if data is not None else "GET")
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                try:
                    detail = e.read(500).decode("utf-8", "replace")
                except Exception:
                    detail = ""
                raise Unauthorized(f"{e.code} {detail}".strip())
            if e.code in (429, 500, 502, 503, 504) and i + 1 < tries:
                time.sleep(2 ** (i + 1))
                continue
            raise
        except urllib.error.URLError:
            if i + 1 < tries:
                time.sleep(2 ** i)
                continue
            raise


def http_post_image(url, headers, file, tries=3):
    """POST an image file. Returns (status, headers with lowercase names, body).
    422 (no card in the photo) comes back as a result, not an exception."""
    data = Path(file).read_bytes()
    ctype = {".png": "image/png", ".webp": "image/webp", ".heic": "image/heic"}.get(Path(file).suffix.lower(), "image/jpeg")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"User-Agent": "card-vault/2", "Content-Type": ctype, **(headers or {})})
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise Unauthorized(f"{e.code} {e.read(500).decode('utf-8', 'replace')}".strip())
            if e.code == 422:
                return 422, {k.lower(): v for k, v in e.headers.items()}, e.read()
            if e.code in (429, 500, 502, 503, 504) and i + 1 < tries:
                time.sleep(2 ** (i + 1))
                continue
            raise
        except urllib.error.URLError:
            if i + 1 < tries:
                time.sleep(2 ** i)
                continue
            raise


class Clients:
    """Thin API wrappers; tests swap the methods for canned responses."""

    def __init__(self, cs_key, psa_token, get=http_json, pause=1.1, max_calls=CS_MAX_CALLS, post=None):
        self.cs_key, self.psa_token, self.get, self.pause, self.max_calls = cs_key, psa_token, get, pause, max_calls
        self.post = post or http_post_image
        self.calls = {"cardsight": 0, "psa": 0}

    def _cs(self, path, params=None, body=None):
        if self.calls["cardsight"] >= self.max_calls:
            raise OutOfBudget()
        self.calls["cardsight"] += 1
        time.sleep(self.pause)
        url = CS_HOST + path + ("?" + urllib.parse.urlencode(params) if params else "")
        return self.get(url, {"X-API-Key": self.cs_key}, body)

    def cs_search(self, q, year=None):
        params = {"q": q, "type": "card", "take": 50}
        if year:
            params["year"] = year
        return (self._cs("/v1/catalog/search", params) or {}).get("results") or []

    def cs_bulk(self, ids):
        r = self._cs("/v1/pricing/", body={"card_ids": ids, "listing_type": "auction", "period": "6m", "limit": BULK_LIMIT})
        return {x.get("card_id"): x.get("data") for x in (r or {}).get("results") or [] if x.get("success") and x.get("data")}

    def cs_card(self, card_id, grade_id=None, parallel_id=None, period="6m"):
        params = {"listing_type": "auction", "period": period, "limit": DEEP_LIMIT}
        if grade_id:
            params["grade_id"] = grade_id
        if parallel_id is not None:
            params["parallel_id"] = parallel_id
        return self._cs(f"/v1/pricing/{urllib.parse.quote(str(card_id))}", params)

    def cs_title_search(self, q, period="1y"):
        r = self._cs("/v1/pricing/search", {"q": q, "listing_type": "auction", "period": period, "limit": 500})
        return (r or {}).get("results") or []

    def _cs_image(self, path, params, file):
        if self.calls["cardsight"] >= self.max_calls:
            raise OutOfBudget()
        self.calls["cardsight"] += 1
        time.sleep(self.pause)
        url = CS_HOST + path + ("?" + urllib.parse.urlencode(params) if params else "")
        return self.post(url, {"X-API-Key": self.cs_key}, file)

    def cs_identify(self, file):
        status, _, body = self._cs_image("/v1/identify/card", None, file)
        return json.loads(body.decode("utf-8")) if status == 200 and body else {}

    def cs_cardmagic(self, file, slab):
        """(status, headers, image bytes): the card straightened, squared and
        color-corrected; a slab comes back whole with its label."""
        return self._cs_image("/v1/cardmagic/process", {"graded": "slab" if slab else "card", "paddingPercent": 0,
                                                         "longEdge": CLEAN_LONG_EDGE, "outputFormat": "jpeg"}, file)

    def psa_cert(self, cert):
        self.calls["psa"] += 1
        return self.get(PSA_URL + urllib.parse.quote(str(cert)), {"Authorization": f"bearer {self.psa_token}"})


# ---------------------------------------------------------------- per card

def psa_patch(cert_resp, today):
    """Card fields from a PSA cert lookup, or None when the cert is not found."""
    pc = (cert_resp or {}).get("PSACert") or {}
    if not pc.get("CertNumber"):
        return None
    patch = {
        "grader": "PSA",
        "psa": {"verified_at": today, "label": pc.get("CardGrade"), "pop": pc.get("TotalPopulation"),
                "pop_higher": pc.get("PopulationHigher"), "spec_id": pc.get("SpecID")},
    }
    g = _num(pc.get("CardGrade"))
    if g is not None:
        patch["grade"] = g
    for src, dst in (("Subject", "player"), ("Year", "year"), ("Brand", "set"), ("CardNumber", "number"), ("Variety", "parallel")):
        v = pc.get(src)
        if v not in (None, ""):
            patch[dst] = str(v).strip().title() if dst == "player" else str(v).strip()
    return patch


def check_psa(view, cl, today, budget, patch, notes):
    if (view.get("grader") or "").upper() != "PSA" or not view.get("cert") or (view.get("psa") or {}).get("verified_at"):
        return
    if not cl.psa_token or budget.get("psa_off"):
        return
    if budget["psa"] <= 0:
        notes.append("psa_budget")
        return
    budget["psa"] -= 1
    try:
        p = psa_patch(cl.psa_cert(view["cert"]), today)
    except Unauthorized as e:
        # PSA answers 403 "limited to approved customers" when the account has
        # no API access; a fresh token from the same account won't help.
        # Either way, stop asking for the rest of this run.
        budget["psa_off"] = "psa_not_approved" if "approved customers" in str(e).lower() else "psa_unauthorized"
        notes.append(budget["psa_off"])
        return
    except Exception as e:  # network; try again next run
        notes.append(f"psa_error:{type(e).__name__}")
        return
    if p is None:
        patch.update({"needs_review": True, "review_reason": "PSA has no record of this cert number"})
        return
    # PSA's label is the source of truth for grade; keep the owner's own
    # wording for descriptive fields they already filled in.
    for k, v in p.items():
        if k in ("grade", "grader", "psa") or not view.get(k):
            patch[k] = v
    view.update(patch)


def match_card(view, cl, patch, notes, state):
    """Find the CardSight catalog card, trying short searches in turn. A clear
    winner is taken; close calls go to David with up to 3 choices; nothing
    found marks the card as having no sales data (no nagging)."""
    guide = view.get("guide") or {}
    if guide.get("id") and "subset" not in guide and not guide.get("picked"):
        guide = {}  # matched by the first version of this script; match again
        view["guide"] = None
        patch["guide"] = None
    if guide.get("id") or (guide.get("none") and not guide.get("auto")) or not cl.cs_key or state["cs_off"]:
        return
    queries = search_queries(view)
    key = " | ".join(queries)
    # A card found nothing for gets one more try whenever its searches change
    # (edited fields or a better search here); David's own "no match" stands.
    if not queries or key == view.get("guide_query"):
        return
    found = {}
    ranked = []
    for q in queries:
        try:
            results = cl.cs_search(q)
        except Unauthorized:
            state["cs_off"] = "cardsight_unauthorized"
            return
        except OutOfBudget:
            notes.append("cardsight_budget")
            return
        except Exception as e:
            notes.append(f"cardsight_error:{type(e).__name__}")
            return
        for r in results:
            if r.get("type", "card") == "card" and r.get("id"):
                found[r["id"]] = r
        ranked = sorted(((score_candidate(view, r), r) for r in found.values()), key=lambda t: -t[0])
        if clear_winner(ranked):
            break
    patch["guide_query"] = key
    if clear_winner(ranked):
        conf, best = ranked[0]
        patch["guide"] = {**guide_ref(best, round(conf, 2)), "rivals": rival_words(best, found.values(), card_text(view))}
        if view.get("guide_candidates"):
            patch["guide_candidates"] = {"__delete__": True}
        if view.get("review_reason") in MATCH_REASONS:
            patch.update({"needs_review": False, "review_reason": None})
    elif ranked and ranked[0][0] >= 0.5:
        patch["guide_candidates"] = [{**guide_ref(r, None), "score": round(s, 2), "rivals": rival_words(r, found.values(), card_text(view))}
                                     for s, r in ranked[:3] if s >= 0.4]
        patch["needs_review"] = True
        patch["review_reason"] = MATCH_REASONS[0]
        if guide.get("none"):
            patch["guide"] = None  # an earlier "no match" no longer holds
    else:
        patch["guide"] = {"none": True, "auto": True}
        if view.get("guide_candidates"):
            patch["guide_candidates"] = {"__delete__": True}
        if view.get("review_reason") in MATCH_REASONS:
            patch.update({"needs_review": False, "review_reason": None})
    view.update({k: v for k, v in patch.items() if not (isinstance(v, dict) and v.get("__delete__"))})


# Words too common in listing titles to mark a sister product.
RIVAL_SKIP = {"rated", "rookies", "rookie", "insert", "inserts", "retail", "hobby", "edition", "series", "collection"}


def rival_words(best, results, own_text=""):
    """Words naming sister products that list the same player and number
    ("Phenomenal Beginning" beside the 2003 Upper Deck LeBron box set). A sale
    title with one of them is the other card. Never a word of the card's own."""
    mine = set(toks(f"{best.get('releaseName') or ''} {best.get('setName') or ''} {best.get('manufacturerName') or ''} {own_text}"))
    out = set()
    year = str(best.get("year") or "")[:4]
    for r in results:
        if r.get("id") == best.get("id") or _num_key(r.get("cardNumber") or "") != _num_key(best.get("cardNumber") or ""):
            continue
        ry = str(r.get("year") or "")[:4]
        if not (year.isdigit() and ry.isdigit() and abs(int(ry) - int(year)) <= 1):
            continue  # a sister product comes out the same season
        if set(toks(r.get("name"))) != set(toks(best.get("name"))):
            continue
        words = set(toks(f"{r.get('releaseName') or ''} {r.get('setName') or ''}")) - mine - set(toks(r.get("name")))
        out |= {w for w in words if len(w) > 2 and not re.fullmatch(r"\d+", w)
                and w not in GENERIC_SET | MAKERS | SPORTS | AUTO_WORDS | RIVAL_SKIP}
    return sorted(out)


def learn_rivals(card, cl, notes, state):
    """Rival words for a card matched before they were kept: one catalog search
    on player and number. None when the search couldn't run."""
    g = card.get("guide") or {}
    try:
        results = cl.cs_search(f"{_query_name(card.get('player'))} {card.get('number')}")
    except Unauthorized:
        state["cs_off"] = "cardsight_unauthorized"
        return None
    except OutOfBudget:
        notes.append("cardsight_budget")
        return None
    except Exception as e:
        notes.append(f"cardsight_error:{type(e).__name__}")
        return None
    best = next((r for r in results if str(r.get("id")) == str(g.get("id"))), None) or {
        "id": g.get("id"), "name": g.get("name") or card.get("player"), "cardNumber": g.get("number") or card.get("number"),
        "releaseName": g.get("release"), "setName": g.get("subset") or g.get("set"),
        "year": g.get("year") or str(card.get("year") or "")[:4]}
    return rival_words(best, results, f"{g.get('set') or ''} {card_text(card)}")


def clear_winner(ranked):
    return bool(ranked) and ranked[0][0] >= AUTO_ACCEPT and (len(ranked) == 1 or ranked[0][0] - ranked[1][0] >= 0.08)


def guide_ref(r, conf):
    set_name = " ".join(x for x in (str(r.get("year") or ""), r.get("releaseName") or r.get("manufacturerName"), r.get("setName")) if x)
    ref = {"source": "cardsight", "id": str(r.get("id")), "name": r.get("name"), "set": set_name or None, "number": r.get("cardNumber"),
           "release": r.get("releaseName"), "subset": r.get("setName"), "year": r.get("year")}
    if conf is not None:
        ref["confidence"] = conf
    return ref


def value_patch(c, comp, today):
    """Patch for a card's value from comp_value output."""
    p = {"value_checked_at": today}
    if comp["cents"] is not None:
        old = c.get("value_cents")
        hist = [h for h in (c.get("history") or []) if h and h[0] != today][-(HISTORY_LEN - 1):]
        p.update({
            "value_cents": comp["cents"], "value_prev_cents": old if old is not None else comp["cents"],
            "value_at": today, "value_basis": comp["basis"], "value_n": comp["n"], "value_last_sale": comp["last_sale"],
            "value_thin": comp["thin"], "value_stale": False, "value_sales": comp.get("sales"),
            "value_source": "cardsight+titles" if comp.get("title_n") else "cardsight",
            "history": hist + [[today, comp["cents"]]],
        })
        if c.get("review_reason") == PARALLEL_REASON:
            p.update({"needs_review": False, "review_reason": None, "parallel_options": None})
    else:
        p.update({"value_stale": True})
        if c.get("review_reason") == PARALLEL_REASON:
            p.update({"needs_review": False, "review_reason": None, "parallel_options": None})
        if c.get("value_cents") is None:
            version = " ".join(x for x in (effective_parallel(c), grade_label(c.get("grader"), c.get("grade"))) if x)
            p["value_basis"] = f"No {version} sales in the last year"
    return p


# ---------------------------------------------------------------- photos
# Card Magic turns David's phone photos into clean card images (straightened,
# squared, color-corrected; a slab keeps its label). Identify reads the same
# photo: the catalog card, and a slab's grade and cert. Identify only fills
# fields left empty; where it disagrees with what David entered, or isn't
# sure, he gets the question instead.

PHOTO_MAX = 30  # photos per run, 2 CardSight requests each
CLEAN_LONG_EDGE = 1400
PHOTO_REASON = "Is this the card?"
GRADERS = {"PSA", "BGS", "SGC", "CGC", "TAG"}


def photos_needed(cards, limit=PHOTO_MAX):
    """Cards whose photo hasn't been cleaned and read yet: owned first, newest first."""
    todo = [c for c in cards if c.get("photo") and not c.get("photo_checked_at") and c.get("status") in ("owned", "sold")]
    todo.sort(key=lambda c: str(c.get("created_at") or ""), reverse=True)
    todo.sort(key=lambda c: c.get("status") != "owned")
    return todo[:limit]


def _set_name(k):
    rel, maker = k.get("releaseName") or "", k.get("manufacturer") or ""
    return rel if not maker or norm(maker) in norm(rel) else f"{maker} {rel}".strip()


def identify_patch(card, res):
    """Card fields from an Identify result. High confidence and consistent with
    what David entered: fill the empty fields and take the catalog match.
    Otherwise keep the reading for a one-tap question."""
    dets = (res or {}).get("detections") or []
    if not dets:
        return {}
    d, patch = dets[0], {}
    gr = d.get("grading") or {}
    if gr.get("confidence") in ("High", "Medium"):
        company = _company((gr.get("company") or {}).get("name"))
        company = company if company in GRADERS else None
        grade = _num((gr.get("grade") or {}).get("value"))
        if company and not card.get("grader"):
            patch["grader"] = company
        if grade is not None and card.get("grade") is None:
            patch["grade"] = grade
        cert = re.sub(r"\D", "", str(gr.get("certNumber") or ""))
        if cert and not card.get("cert"):
            patch["cert"] = cert
    k = d.get("card") or {}
    if not k.get("id"):
        return patch
    sugg = (k.get("parallelSuggestions") or [{}])[0]
    reading = {"id": k["id"], "name": k.get("name"), "number": k.get("number"), "year": k.get("year"),
               "set": _set_name(k), "subset": k.get("setName"), "release": k.get("releaseName"),
               "serial": f"/{k['numberedTo']}" if k.get("numberedTo") else None,
               "parallel": sugg.get("name") if sugg.get("confidence") == "High" else None,
               "rc": any(norm(a) in ("rc", "rookie") for a in k.get("attributes") or []),
               "auto": any("auto" in norm(a) for a in k.get("attributes") or []),
               "confidence": d.get("confidence")}
    patch["photo_reading"] = reading
    player_ok = not card.get("player") or not k.get("name") or _names_ok(set(toks(k["name"])), card["player"]) \
        or _names_ok(set(toks(card["player"])), k["name"])
    number_ok = not card.get("number") or _num_key(card["number"]) == _num_key(k.get("number") or "")
    year_ok = not card.get("year") or str(card["year"])[:4] == str(k.get("year") or "")[:4]
    if d.get("confidence") == "High" and player_ok and number_ok and year_ok:
        patch.update(photo_fields(card, reading))
    elif card.get("needs_review") and card.get("review_reason") != PHOTO_REASON:
        pass  # one question at a time; this reading stays on the card
    elif not (player_ok and number_ok and year_ok) or not card.get("number") or not card.get("year"):
        # A reading that disagrees, or one that would supply a missing number or year: ask.
        patch.update({"needs_review": True, "review_reason": PHOTO_REASON})
    return patch


def photo_fields(card, reading):
    """The fields a photo reading supplies, without touching anything David entered.
    Also used by the page when he answers yes to "Is this the card?"."""
    out = {}
    for k in ("number", "year", "set", "serial", "parallel"):
        if reading.get(k) and not card.get(k):
            out[k] = reading[k]
    if reading.get("name") and not card.get("player"):
        out["player"] = reading["name"]
    for k in ("rc", "auto"):
        if reading.get(k) and not card.get(k):
            out[k] = True
    g = card.get("guide") or {}
    if not g.get("id") and not (g.get("none") and not g.get("auto")):
        out["guide"] = {"source": "cardsight", "id": reading["id"], "name": reading.get("name"), "number": reading.get("number"),
                        "set": " ".join(x for x in (reading.get("year"), reading.get("release"), reading.get("subset")) if x) or None,
                        "release": reading.get("release"), "subset": reading.get("subset"), "year": reading.get("year"),
                        "confidence": 0.9, "via": "photo"}
        if g.get("none"):
            out["guide"].update({"none": {"__delete__": True}, "auto": {"__delete__": True}})
    return out


def photo_step(cards, cl, photo_dir, out_dir, today, errors, state):
    """Clean and read each pending photo found in photo_dir (saved as <photo id>.<ext>).
    Returns patches; cleaned images land in out_dir/photos/<card id>.jpg."""
    patches = {}
    if not photo_dir or not cl.cs_key or state["cs_off"]:
        return patches
    pdir, odir = Path(photo_dir), Path(out_dir) / "photos"
    for c in photos_needed(cards):
        src = next(iter(sorted(pdir.glob(f"{c['photo']}*"))), None)
        if not src:
            continue
        notes = errors.setdefault(c["id"], [])
        try:
            reading = cl.cs_identify(src)
            slab = bool(c.get("grader")) or any(d.get("grading") for d in (reading or {}).get("detections") or [])
            status, headers, body = cl.cs_cardmagic(src, slab)
        except Unauthorized:
            state["cs_off"] = "cardsight_unauthorized"
            break
        except OutOfBudget:
            notes.append("cardsight_budget")
            break
        except Exception as e:  # network; this photo waits for the next run
            notes.append(f"photo_error:{type(e).__name__}")
            continue
        finally:
            if not notes:
                errors.pop(c["id"], None)
        patch = identify_patch(c, reading)
        patch["photo_checked_at"] = today
        image = body if status == 200 else None
        several = False
        if image and "zip" in (headers.get("content-type") or ""):
            # Several cards: often an open booklet read as two. The original photo shows it better.
            with zipfile.ZipFile(io.BytesIO(image)) as z:
                several = sum(1 for n in z.namelist() if re.fullmatch(r"card_\d+\.\w+", n)) > 1
                first = next((n for n in sorted(z.namelist()) if re.fullmatch(r"card_0\.\w+", n)), None)
                image = None if several or not first else z.read(first)
        if image:
            odir.mkdir(parents=True, exist_ok=True)
            (odir / f"{c['id']}.jpg").write_bytes(image)
            patch.update({"photo_kind": headers.get("x-cardmagic-subject") or ("slab" if slab else "card"), "photo_pending": True})
        elif several:
            patch["photo_error"] = "More than one card in the photo, so the original stays"
        else:
            patch["photo_error"] = "No card found in the photo" if status == 422 else f"Photo cleanup failed ({status})"
        patches[c["id"]] = patch
    return patches


def attach_photos(out_dir, uploaded):
    """After the session uploads out/photos/<card id>.jpg, put each new asset id
    into that card's patch. A card whose upload didn't happen is left to retry."""
    for f in sorted((Path(out_dir) / "cards").glob("*.json")):
        patch = json.loads(f.read_text())
        if not patch.pop("photo_pending", None):
            continue
        asset = uploaded.get(f.stem)
        if asset:
            patch["photo_clean"] = str(asset).rsplit("/", 1)[-1]
        else:
            for k in ("photo_checked_at", "photo_kind", "photo_reading"):
                patch.pop(k, None)
        f.write_text(json.dumps(patch, indent=1))


def grade_values(data, card, today):
    """For a raw card: what the same card sells for as a PSA 10 and PSA 9 in the
    catalog sales already fetched (no extra calls). None when neither has sales."""
    out = {}
    for grade in (10, 9):
        comp = comp_value(data or {}, {**card, "grader": "PSA", "grade": grade}, today)
        if comp["cents"] is not None:
            out[f"PSA {grade}"] = {"cents": comp["cents"], "n": comp["n"], "last": comp["last_sale"]}
    return out or None


# ---------------------------------------------------------------- portfolio

def totals(cards):
    owned = [c for c in cards if c.get("status") == "owned"]
    value = sum(c["value_cents"] for c in owned if c.get("value_cents") is not None)
    cost = sum(c["cost_cents"] for c in owned if c.get("cost_cents") is not None)
    both = [c for c in owned if c.get("value_cents") is not None and c.get("cost_cents") is not None]
    return {
        "count": len(owned),
        "priced": sum(1 for c in owned if c.get("value_cents") is not None),
        "value_cents": value,
        "cost_cents": cost,
        "unrealized_cents": sum(c["value_cents"] - c["cost_cents"] for c in both),
    }


def movers(cards, n=5):
    rows = []
    for c in cards:
        v, p = c.get("value_cents"), c.get("value_prev_cents")
        if c.get("status") == "owned" and v is not None and p:
            rows.append({"id": c["id"], "name": c.get("player") or c.get("title"), "from": p, "to": v, "pct": round((v - p) / p * 100, 1)})
    up = sorted([r for r in rows if r["to"] > r["from"]], key=lambda r: -(r["to"] - r["from"]))[:n]
    down = sorted([r for r in rows if r["to"] < r["from"]], key=lambda r: r["to"] - r["from"])[:n]
    return up, down


def usd(cents):
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(cents) / 100:,.0f}"


def notify_decision(t, prev, up, down, new_reviews):
    """(notify, one-line text). Quiet unless something moved or needs a tap."""
    bits, notify = [], False
    line = f"Collection {usd(t['value_cents'])}"
    if prev and prev.get("value_cents"):
        pct = (t["value_cents"] - prev["value_cents"]) / prev["value_cents"] * 100
        line += f" ({pct:+.1f}% since {prev.get('date', 'last run')})"
        notify |= abs(pct) >= 5
    bits.append(line)
    big = [m for m in up + down if abs(m["pct"]) >= 20 and abs(m["to"] - m["from"]) >= 1000]
    if big:
        notify = True
        bits.append("; ".join(f"{m['name']} {usd(m['to'] - m['from'])} ({m['pct']:+.0f}%)" for m in big[:3]))
    if new_reviews:
        notify = True
        bits.append(f"{new_reviews} card{'s need' if new_reviews != 1 else ' needs'} a tap in Card Vault")
    return notify, ". ".join(bits) + "."


# ---------------------------------------------------------------- run

def run(cards, cl, today, prev=None, photo_dir=None, out_dir=".", photos_only=False):
    budget = {"psa": PSA_DAILY_BUDGET, "psa_off": None}
    state = {"cs_off": None if cl.cs_key else "cardsight_key_missing"}
    patches, errors, views = {}, {}, {}
    had_review = {c["id"] for c in cards if c.get("needs_review")}

    # 0. Clean and read new photos first, so what they reveal is matched and priced this run
    patches.update(photo_step(cards, cl, photo_dir, out_dir, today, errors, state))
    if patches:
        cards = [{**c, **{k: v for k, v in patches[c["id"]].items() if not (isinstance(v, dict) and v.get("__delete__"))}}
                 if c["id"] in patches else c for c in cards]

    # 1. PSA check and catalog match, card by card
    for c in ([] if photos_only else cards):
        if c.get("status") != "owned":
            continue
        view, patch, notes = dict(c), {}, []
        check_psa(view, cl, today, budget, patch, notes)
        match_card(view, cl, patch, notes, state)
        views[c["id"]] = view
        if patch:
            patches.setdefault(c["id"], {}).update(patch)
        if notes:
            errors.setdefault(c["id"], []).extend(notes)

    # 2. Recent sales for every matched card, 100 per call
    priced = [v for v in views.values() if (v.get("guide") or {}).get("id")]
    bulk = {}
    if priced and not state["cs_off"]:
        ids = sorted({v["guide"]["id"] for v in priced})
        for i in range(0, len(ids), BULK_SIZE):
            try:
                bulk.update(cl.cs_bulk(ids[i:i + BULK_SIZE]))
            except Unauthorized:
                state["cs_off"] = "cardsight_unauthorized"
                break
            except OutOfBudget:
                errors.setdefault("_run", []).append("cardsight_budget")
                break
            except Exception as e:
                errors.setdefault("_run", []).append(f"cardsight_bulk_error:{type(e).__name__}")

    # 3. Value each card from its catalog sales. Pull one card deeper when other
    # versions crowded out its sales; search sale titles when it is still thin.
    deep_left, title_left = DEEP_MAX, TITLE_MAX
    for v in (views[c["id"]] for c in cards if c["id"] in views):
        g = v.get("guide") or {}
        if state["cs_off"] or not (g.get("id") or g.get("none")):
            continue  # not matched yet, or CardSight is off this run
        if v.get("grader") and v.get("grade") is None:
            if v.get("review_reason") != GRADE_REASON:
                gp = {"needs_review": True, "review_reason": GRADE_REASON}
                patches.setdefault(v["id"], {}).update(gp)
                v.update(gp)
            continue
        data = {}
        if g.get("id"):
            data = bulk.get(g["id"])
            if data is None:
                errors.setdefault(v["id"], []).append("no_sales_data")
                data = {}
        comp = comp_value(data, v, today)
        if g.get("id") and comp["n"] < 3 and total_rows(data) >= BULK_LIMIT and deep_left > 0 and not state["cs_off"]:
            deep_left -= 1
            try:
                # Pull the whole grade, not just sales tagged with the parallel:
                # CardSight leaves many parallel sales untagged, and the title
                # check in sale_matches finds them. Only when other versions
                # still crowd the grade out, add the tagged pull as well.
                gid = comp["grade_id"] or (None if v.get("grader") else "null")
                pid = None if effective_parallel(v) else "null"
                deep = cl.cs_card(g["id"], gid, pid)
                full = comp_value(deep, v, today)
                if full["n"] < 3 and comp["parallel_id"] and total_rows(deep or {}) >= DEEP_LIMIT and deep_left > 0:
                    deep_left -= 1
                    deep = merge_pricing(deep, cl.cs_card(g["id"], gid, comp["parallel_id"]))
                    full = comp_value(deep, v, today)
                data, comp = deep, full
            except (OutOfBudget, Unauthorized):
                deep_left = 0
            except Exception as e:
                errors.setdefault(v["id"], []).append(f"cardsight_error:{type(e).__name__}")
        titles = []
        if comp["n"] < TITLE_MIN_N and title_left > 0 and title_search_ok(v):
            if g.get("id") and "rivals" not in g:
                rivals = learn_rivals(v, cl, errors.setdefault(v["id"], []), state)
                title_left -= 1
                if rivals is not None:
                    stale = {k: {"__delete__": True} for k in ("none", "auto") if k in g}  # a pick over an old "no match"
                    g = {**{k: x for k, x in g.items() if k not in stale}, "rivals": rivals}
                    v["guide"] = g
                    patches.setdefault(v["id"], {})["guide"] = {**g, **stale}
            titles, used = title_search(v, cl, title_left, errors.setdefault(v["id"], []), state, have=comp["records"])
            title_left -= used
            if titles:
                comp = comp_value(data, v, today, extra=titles)
        if comp["n"] == 0 and g.get("id") and deep_left > 0 and not state["cs_off"]:
            # Nothing in 6 months: look back a year before calling it unsold.
            deep_left -= 1
            try:
                gid = comp["grade_id"] or (None if v.get("grader") else "null")
                pid = None if effective_parallel(v) else "null"
                comp = comp_value(cl.cs_card(g["id"], gid, pid, period="1y"), v, today, window_days=366, extra=titles)
            except (OutOfBudget, Unauthorized):
                deep_left = 0
            except Exception as e:
                errors.setdefault(v["id"], []).append(f"cardsight_error:{type(e).__name__}")
        elif comp["n"] == 0 and titles:
            comp = comp_value({}, v, today, window_days=366, extra=titles)
        if not errors.get(v["id"]):
            errors.pop(v["id"], None)
        if not v.get("grader") and g.get("id"):
            gv = grade_values(bulk.get(g["id"]), v, today)
            if gv != v.get("grade_values"):
                patches.setdefault(v["id"], {})["grade_values"] = gv
        if not g.get("id") and comp["n"] == 0:
            continue  # no catalog card and no sales by title: nothing new to say
        vp = value_patch(v, comp, today)
        patches.setdefault(v["id"], {}).update(vp)
        v.update(vp)

    merged = []
    for c in cards:
        m = dict(views.get(c["id"], c))
        merged.append({k: val for k, val in m.items() if not (isinstance(val, dict) and val.get("__delete__"))})
    t = totals(merged)
    up, down = movers(merged)
    new_reviews = sum(1 for m in merged if m.get("needs_review") and m["id"] not in had_review)
    notify, text = notify_decision(t, prev, up, down, new_reviews)
    missing = [x for x in (state["cs_off"], None if cl.psa_token else "psa_token_missing", budget["psa_off"]) if x]
    summary = {
        "date": today, "totals": t, "up": up, "down": down, "new_reviews": new_reviews,
        "review_total": sum(1 for m in merged if m.get("needs_review")),
        "ambiguous": [{"id": m["id"], "title": m.get("title"), "candidates": m.get("guide_candidates")} for m in merged
                      if m["id"] in patches and isinstance(patches[m["id"]].get("guide_candidates"), list)],
        "calls": dict(cl.calls), "errors": errors, "notify": notify, "notify_text": text,
        "missing_tokens": missing, "write_snapshot": t["priced"] > 0,
        "photos": {"cleaned": sum(1 for p in patches.values() if p.get("photo_pending")),
                   "unreadable": sum(1 for p in patches.values() if p.get("photo_error"))},
    }
    return patches, summary, {"date": today, **t}


# ---------------------------------------------------------------- io

def load_doc(f):
    """One doc from an ArtifactData out_dir export. Accepts the bare body or a
    wrapper carrying the body under `data` beside `id`/`version`."""
    f = Path(f)
    raw = json.loads(f.read_text())
    if isinstance(raw.get("data"), dict) and ("version" in raw or "id" in raw or "doc_id" in raw):
        body = dict(raw["data"])
        body["id"] = raw.get("id") or raw.get("doc_id") or f.stem
        body["_version"] = raw.get("version")
    else:
        body = dict(raw)
        body.setdefault("id", f.stem)
        body["_version"] = raw.get("version") or raw.get("_version") or raw.get("__version__")
    return body


def load_docs(d):
    return [load_doc(f) for f in sorted(Path(d).rglob("*.json"))]


def load_versions(f):
    """Doc versions from the ArtifactData listing text saved verbatim, whose
    lines read `- "<id>"  612 bytes  version 3  "<path>"`, or from a JSON map."""
    text = Path(f).read_text()
    try:
        return {str(k): int(v) for k, v in json.loads(text).items()}
    except (ValueError, AttributeError):
        return {m.group(1): int(m.group(2)) for m in re.finditer(r'"([^"]+)"\s+[\d,]+\s+bytes\s+version\s+(\d+)', text)}


def write_outputs(out, cards, patches, summary, snapshot):
    out = Path(out)
    (out / "cards").mkdir(parents=True, exist_ok=True)
    versions = {c["id"]: c.get("_version") for c in cards}
    writes = []
    for cid, patch in patches.items():
        p = out / "cards" / f"{cid}.json"
        p.write_text(json.dumps(patch, indent=1))
        w = {"op": "update", "collection": "cards", "doc_id": cid, "file_path": str(p.resolve())}
        if versions.get(cid):
            w["if_version"] = versions[cid]
        writes.append(w)
    (out / "snapshot.json").write_text(json.dumps(snapshot, indent=1))
    (out / "summary.json").write_text(json.dumps(summary, indent=1))
    batches = [writes[i:i + BATCH_MAX] for i in range(0, len(writes), BATCH_MAX)]
    (out / "writes.json").write_text(json.dumps(batches, indent=1))
    return batches


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--cards")
    ap.add_argument("--out")
    ap.add_argument("--prev-snapshot")
    ap.add_argument("--versions", help="ArtifactData listing text (or JSON map) giving each card's version")
    ap.add_argument("--today", default=dt.date.today().isoformat())
    ap.add_argument("--photo-dir", help="folder holding the downloaded photos listed by --list-photos")
    ap.add_argument("--list-photos", action="store_true", help="print the cards whose photo needs cleaning, then stop")
    ap.add_argument("--photos-only", action="store_true", help="clean and read photos; skip matching and prices")
    ap.add_argument("--attach-photos", help="JSON map of card id to uploaded asset id; adds photo_clean to the patches in --out, then stop")
    a = ap.parse_args(argv)
    if not (a.cards or a.attach_photos) or not (a.out or a.list_photos):
        ap.error("--cards and --out are needed (only --out with --attach-photos, only --cards with --list-photos)")
    if a.attach_photos:
        attach_photos(a.out, json.loads(Path(a.attach_photos).read_text()))
        print(json.dumps({"attached": True}))
        return
    cards = load_docs(a.cards)
    if a.list_photos:
        print(json.dumps([{"card_id": c["id"], "photo": c["photo"]} for c in photos_needed(cards)], indent=1))
        return
    if a.versions:
        vers = load_versions(a.versions)
        for c in cards:
            c["_version"] = vers.get(c["id"], c.get("_version"))
        missing = [c["id"] for c in cards if not c.get("_version")]
        if missing:
            print(f"warning: no version for {len(missing)} cards: {missing[:5]}", file=sys.stderr)
    prev = load_doc(a.prev_snapshot) if a.prev_snapshot and Path(a.prev_snapshot).is_file() else None
    cl = Clients(os.environ.get("CARDSIGHT_API_KEY"), os.environ.get("PSA_TOKEN"))
    patches, summary, snapshot = run(cards, cl, a.today, prev, photo_dir=a.photo_dir, out_dir=a.out, photos_only=a.photos_only)
    batches = write_outputs(a.out, cards, patches, summary, snapshot)
    print(json.dumps({"cards": len(cards), "patched": len(patches), "batches": len(batches),
                      "notify": summary["notify"], "notify_text": summary["notify_text"],
                      "missing_tokens": summary["missing_tokens"], "calls": summary["calls"]}, indent=1))


if __name__ == "__main__":
    sys.exit(main())
