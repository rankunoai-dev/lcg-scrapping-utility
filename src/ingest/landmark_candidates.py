"""Landmark Candidate Register builder (Google Places API, human-gated).

For every audited school with verified coordinates (RIO lat/lng), queries
Places Nearby Search and distills the top landmark CANDIDATES — scored by the
landmark styles the network's own copy proves work (malls, big-box/grocery,
recognizable chains, parks, schools/colleges) — into:

    data/landmark_candidate_register.csv   (approval columns, like G1)
    LCG_Landmark_Candidates.xlsx           (analyst review workbook)

Trust rules (ARCHITECTURE §2/§3):
  * Candidates are Tier-2 DISCOVERED data: usable in drafts/recommendations
    ONLY after a human approves the row. The content writer keeps emitting
    [CLIENT INPUT NEEDED: Landmark] until an approved row exists.
  * Phrasing is conservative ("near X") — directional claims ("across from")
    require eyes-on confirmation and are left to the approver.
  * Raw API responses are cached per school (data/places_cache/) so re-runs
    and re-scoring are free.

CLI:
    python -m src.ingest.landmark_candidates [--limit N] [--schools ids]
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import os
import time
import urllib.request
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

CACHE_DIR = Path("data/places_cache")
REGISTER_CSV = Path("data/landmark_candidate_register.csv")
WORKBOOK = Path("LCG_Landmark_Candidates.xlsx")
RADIUS_M = 1000.0
TOP_N = 5

INCLUDED_TYPES = [
    "shopping_mall", "supermarket", "department_store", "home_improvement_store",
    "park", "school", "university", "library", "stadium",
    "fast_food_restaurant", "coffee_shop", "pharmacy", "gym", "movie_theater",
]

# category weights mirror the landmark styles found in live LCG copy
TYPE_WEIGHTS = {
    "shopping_mall": 5.0, "department_store": 4.5, "supermarket": 4.0,
    "home_improvement_store": 4.0, "park": 4.0, "university": 4.0,
    "school": 3.0, "library": 3.5, "stadium": 4.5, "movie_theater": 3.5,
    "pharmacy": 2.5, "fast_food_restaurant": 2.0, "coffee_shop": 2.0, "gym": 2.0,
}

# nationally recognizable anchors get a boost — matches "tucked behind Sonic",
# "across from Denny's" style in the live copy
KNOWN_CHAINS = {
    "walmart", "target", "costco", "home depot", "lowe's", "lowes", "kroger",
    "publix", "safeway", "aldi", "heb", "h-e-b", "meijer", "cvs", "walgreens",
    "mcdonald's", "mcdonalds", "chick-fil-a", "starbucks", "denny's", "dennys",
    "sonic", "waffle house", "ihop", "panera", "wendy's", "burger king",
    "taco bell", "dunkin", "whataburger", "dairy queen",
}


def haversine_m(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def fetch_nearby(key: str, school_id: str, lat: float, lng: float,
                 radius: float = RADIUS_M) -> dict:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = CACHE_DIR / (f"{school_id}.json" if radius == RADIUS_M
                         else f"{school_id}_r{int(radius)}.json")
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    body = {
        "includedTypes": INCLUDED_TYPES,
        "maxResultCount": 20,
        "rankPreference": "POPULARITY",
        "locationRestriction": {"circle": {
            "center": {"latitude": lat, "longitude": lng}, "radius": radius}},
    }
    req = urllib.request.Request(
        "https://places.googleapis.com/v1/places:searchNearby",
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": key,
            "X-Goog-FieldMask": (
                "places.displayName,places.types,places.location,places.rating,"
                "places.userRatingCount,places.googleMapsUri,places.primaryType"),
        }, method="POST")
    last_err = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.load(resp)
            cache.write_text(json.dumps(data), encoding="utf-8")
            return data
        except urllib.error.HTTPError as e:
            detail = e.read().decode()[:300]
            if e.code in (429, 500, 503):
                last_err = f"HTTP {e.code}"
                time.sleep(2 * (attempt + 1))
                continue
            raise SystemExit(f"Places API error HTTP {e.code} (not retryable): {detail}")
        except OSError as e:
            last_err = str(e)
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Places lookup failed for {school_id}: {last_err}")


# never landmark-worthy, or awkward (the school itself / competitor daycares)
EXCLUDED_PRIMARY = {"child_care_agency", "preschool", "day_care_center",
                    "auto_parts_store", "manufacturer", "business_center",
                    "corporate_office", "insurance_agency", "real_estate_agency"}


def score_candidates(data: dict, lat: float, lng: float,
                     school_name: str = "", brand: str = "") -> list[dict]:
    from src.matching.normalize import norm_key
    school_key = norm_key(school_name)
    brand_key = norm_key(brand)
    out = []
    for p in data.get("places", []):
        name = p.get("displayName", {}).get("text", "")
        if not name:
            continue
        primary = p.get("primaryType", "")
        types = p.get("types", [])
        name_key = norm_key(name)
        loc = p.get("location", {})
        dist = haversine_m(lat, lng, loc.get("latitude", lat), loc.get("longitude", lng))

        # exclusions: the school itself, same-brand sites, competitor childcare,
        # non-landmark business types
        if dist < 40:
            continue
        if brand_key and brand_key in name_key:
            continue
        if school_key and (school_key in name_key or name_key in school_key):
            continue
        if primary in EXCLUDED_PRIMARY:
            continue

        chain = any(c in name_key for c in KNOWN_CHAINS)
        tw = TYPE_WEIGHTS.get(primary) or max(
            (TYPE_WEIGHTS.get(t, 0.0) for t in types), default=0.0)
        if tw == 0.0 and not chain:
            continue
        ratings = int(p.get("userRatingCount", 0) or 0)
        score = (tw or 2.0) + math.log10(ratings + 1) + (1.5 if chain else 0.0) \
            - (dist / 400.0)
        out.append({
            "candidate_name": name,
            "primary_type": primary or (types[0] if types else ""),
            "distance_m": round(dist),
            "rating": p.get("rating", ""),
            "rating_count": ratings,
            "known_chain": "yes" if chain else "",
            "maps_link": p.get("googleMapsUri", ""),
            "suggested_phrase": f"near {name}",
            "score": round(score, 2),
        })
    out.sort(key=lambda x: -x["score"])

    # one candidate per brand family ("Walmart Neighborhood Market" vs
    # "Walmart Pharmacy" is the same complex)
    deduped, seen_brands = [], set()
    for c in out:
        token = norm_key(c["candidate_name"]).split()[0] if c["candidate_name"] else ""
        if token in seen_brands:
            continue
        seen_brands.add(token)
        deduped.append(c)
    return deduped[:TOP_N]


APPROVAL_FIELDS = ["approved", "approved_by", "approved_date", "analyst_notes",
                   "approved_phrase"]


def load_existing_approvals() -> dict:
    if not REGISTER_CSV.exists():
        return {}
    with open(REGISTER_CSV, newline="", encoding="utf-8") as f:
        return {(r["school_id"], r["candidate_name"]):
                {k: r.get(k, "") for k in APPROVAL_FIELDS}
                for r in csv.DictReader(f)}


def main():
    from dotenv import load_dotenv
    load_dotenv()
    key = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()
    if not key:
        raise SystemExit("GOOGLE_MAPS_API_KEY missing from .env")

    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="max schools this run (0 = all)")
    ap.add_argument("--schools", help="comma-separated school ids")
    ap.add_argument("--output", default=str(WORKBOOK), help="analyst workbook path")
    args = ap.parse_args()

    master = list(csv.DictReader(open("data/master_schools.csv", newline="", encoding="utf-8")))
    audited = {p.stem.replace("_eval", "") for p in Path("data/observations").glob("*_eval.json")}
    targets = [r for r in master if r["school_id"] in audited
               and r.get("lat") and r.get("lng")]
    if args.schools:
        wanted = {x.strip() for x in args.schools.split(",")}
        targets = [r for r in targets if r["school_id"] in wanted]
    if args.limit:
        targets = targets[: args.limit]

    existing = load_existing_approvals()
    rows, api_calls = [], 0
    lookup_failures: list[str] = []
    source_counts = {"Welcome section": 0, "Whole page content": 0,
                     "Google Places API": 0}

    def page_landmark_rows(s: dict, obs: dict) -> list[dict]:
        """The school's OWN page outranks any API lookup: if a landmark is
        already written there, that text IS the landmark — verbatim, with
        the section it came from. Own-page text needs no analyst approval
        (surfacing a page's existing claim is a MOVE, never an invention)."""
        out = []
        for pid, label in (("GEO_06", "Landmark 1"), ("GEO_07", "Landmark 2")):
            p = obs.get("parameters", {}).get(pid, {})
            if p.get("welcome_status") in ("PRESENT", "PARTIAL"):
                text, src = p.get("welcome_evidence"), "Welcome section"
            elif p.get("page_status") in ("PRESENT", "PARTIAL"):
                text, src = p.get("page_evidence"), "Whole page content"
            else:
                continue
            if not text:
                continue
            text = text.strip().strip(".").replace("...", " ").strip()
            out.append({
                "school_id": s["school_id"], "school_name": s["school_name"],
                "school_url": s["url"], "city": s["city"], "state": s["state"],
                "rank": 1 if pid == "GEO_06" else 2, "source": src,
                "candidate_name": text, "primary_type": label.lower(),
                "distance_m": "", "rating": "", "rating_count": "",
                "known_chain": "", "maps_link": "",
                "suggested_phrase": text, "score": "",
                "approved": "yes", "approved_by": "auto (school's own page)",
                "approved_date": "", "analyst_notes": f"verbatim from {src}",
                "approved_phrase": text,
            })
        return out
    for i, s in enumerate(targets, 1):
        sid = s["school_id"]

        # the school's own page wins: landmark already written there means
        # no API candidates for this school at all
        obs_path = Path(f"data/observations/{sid}_eval.json")
        if obs_path.exists():
            own = page_landmark_rows(s, json.loads(obs_path.read_text(encoding="utf-8")))
            if own:
                source_counts[own[0]["source"]] += 1
                rows.extend(own)
                continue

        lat, lng = float(s["lat"]), float(s["lng"])
        try:
            cached = (CACHE_DIR / f"{sid}.json").exists()
            data = fetch_nearby(key, sid, lat, lng)
            if not cached:
                api_calls += 1
                time.sleep(0.05)
            cands = score_candidates(data, lat, lng, s["school_name"], s["brand"])
            if len(cands) < 3:  # sparse area: widen the ring once
                cached2 = (CACHE_DIR / f"{sid}_r2500.json").exists()
                data2 = fetch_nearby(key, sid, lat, lng, radius=2500.0)
                if not cached2:
                    api_calls += 1
                    time.sleep(0.05)
                merged = {"places": data.get("places", []) + data2.get("places", [])}
                cands = score_candidates(merged, lat, lng, s["school_name"], s["brand"])
        except (RuntimeError, OSError) as e:
            # transient network failure must not kill the batch — the cache
            # makes a rerun of just the failed schools free
            logger.warning("[%s] lookup failed, will retry on next run: %s", sid, e)
            lookup_failures.append(sid)
            continue
        source_counts["Google Places API"] += 1
        for rank, c in enumerate(cands, 1):
            row = {"school_id": sid, "school_name": s["school_name"],
                   "school_url": s["url"], "city": s["city"], "state": s["state"],
                   "rank": rank, "source": "Google Places API", **c,
                   "approved": "", "approved_by": "", "approved_date": "",
                   "analyst_notes": "", "approved_phrase": ""}
            prior = existing.get((sid, c["candidate_name"]))
            if prior:
                row.update(prior)
            rows.append(row)
        if i % 100 == 0:
            logger.info("progress: %d/%d schools (%d fresh API calls)", i, len(targets), api_calls)

    cols = ["school_id", "school_name", "school_url", "city", "state", "rank",
            "source", "candidate_name", "primary_type", "distance_m", "rating",
            "rating_count", "known_chain", "maps_link", "suggested_phrase",
            "score"] + APPROVAL_FIELDS
    with open(REGISTER_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    # analyst workbook
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Landmark_Candidates"
    ws.append(["School ID", "School Name", "School URL", "City", "State", "Rank",
               "Source", "Candidate Landmark", "Type", "Distance (m)", "Rating",
               "# Ratings", "Known Chain", "Google Maps Link", "Suggested Phrase",
               "Approve? (yes/no)", "Approved Phrase (edit if needed)", "Notes"])
    fill = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
    for c in ws[1]:
        c.fill = fill
        c.font = Font(bold=True, color="FFFFFF")
    for r in rows:
        ws.append([r["school_id"], r["school_name"], r["school_url"], r["city"],
                   r["state"], r["rank"], r["source"], r["candidate_name"],
                   r["primary_type"], r["distance_m"], r["rating"],
                   r["rating_count"], r["known_chain"], r["maps_link"],
                   r["suggested_phrase"], r["approved"], r["approved_phrase"],
                   r["analyst_notes"]])
    out_path = Path(args.output)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    for letter, width in (("A", 9), ("B", 30), ("C", 40), ("D", 14), ("E", 6),
                          ("F", 5), ("G", 19), ("H", 38), ("I", 20), ("J", 11),
                          ("K", 7), ("L", 9), ("M", 10), ("N", 40), ("O", 34),
                          ("P", 14), ("Q", 30), ("R", 24)):
        ws.column_dimensions[letter].width = width
    wb.save(out_path)

    logger.info("schools processed: %d | candidate rows: %d | fresh API calls: %d "
                "(~$%.2f list price)", len(targets) - len(lookup_failures),
                len(rows), api_calls, api_calls * 0.032)
    if lookup_failures:
        logger.warning("lookup failures (rerun the command to retry just these "
                       "%d): %s", len(lookup_failures), lookup_failures[:20])
    logger.info("landmark source split: %s", source_counts)
    logger.info("register: %s | analyst workbook: %s", REGISTER_CSV, out_path)


if __name__ == "__main__":
    main()
