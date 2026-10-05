"""S1a — RIO ingest: RIO_Location_Export.xlsx -> data/master_schools.csv + data/ingest_report.json.

Tier-0 (client-attested) ground truth. This loader validates, normalizes and
flags; it never invents or silently drops data. Every anomaly becomes a row
flag (semicolon-joined `flags` column) plus an entry in the ingest report.

Row flags emitted:
    DATA_DUP_URL                another school shares this Website URL
    BRAND_MISSING               RIO brand cell empty/None
    BRAND_INFERRED_FROM_DOMAIN  brand filled from the URL domain (only with BRAND_MISSING)
    BRAND_UNKNOWN               brand string not in config/brand_defaults.json
    MISSING_LATLNG              no coordinates
    CITY_URL_MISMATCH           city slug in the URL != RIO City (e.g. RIO "Lacey North" vs slug "lacey")
    STATE_URL_MISMATCH          state slug in the URL != RIO Region
    ID_URL_MISMATCH             school number embedded in the URL != School #
    URL_PATTERN_OTHER           URL does not match the /your-local-school/{city}-{st}-{id}/ pattern
                                (informational; e.g. Montessori template)

Usage:
    python -m src.ingest.rio_loader [--source data/source/RIO_Location_Export.xlsx]
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import openpyxl

from src.matching.normalize import fold_text, norm_key, norm_url, norm_zip, slug_to_key

DEFAULT_SOURCE = Path("data/source/RIO_Location_Export.xlsx")
MASTER_CSV = Path("data/master_schools.csv")
REPORT_JSON = Path("data/ingest_report.json")
BRAND_CONFIG = Path("config/brand_defaults.json")

# Verified path prefixes across the 11 brand sites (2026-10-05):
#   /your-local-school/   La Petite, Childtime, Tutor Time, Children's Courtyard,
#                         Gilden Woods, Pathways, U-GRO, Young School
#   /our-schools/         Everbrook Academy
#   /montessori-schools/  Montessori Unlimited
#   /schools/             Creative Kids
URL_SLUG_RX = re.compile(
    r"/(?:your-local-school|our-schools|montessori-schools|schools)"
    r"/(?P<city>[a-z0-9\-']+)-(?P<st>[a-z]{2})-(?P<num>\d+)/{0,2}$",
    re.I,
)

MASTER_COLUMNS = [
    "school_id", "brand", "brand_display", "school_name", "url",
    "street_address", "address2", "suite", "city", "state", "zip",
    "lat", "lng", "google_place_id", "optimal_radius", "flags",
]


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_brand_config() -> dict:
    return json.loads(BRAND_CONFIG.read_text(encoding="utf-8"))


def _domain_brand_index(brand_cfg: dict) -> dict:
    return {
        info["domain"].lower(): name
        for name, info in brand_cfg.get("brands", {}).items()
        if info.get("domain")
    }


def load_rio_rows(source: Path) -> list[dict]:
    wb = openpyxl.load_workbook(source, read_only=True, data_only=True)
    ws = wb[wb.sheetnames[0]]
    rows_iter = ws.iter_rows(values_only=True)
    header = [str(h) if h is not None else "" for h in next(rows_iter)]
    idx = {h: i for i, h in enumerate(header) if h}
    required = ["School #", "Location Name", "Address 1", "City", "Region", "Post Code", "Website", "brand"]
    missing = [c for c in required if c not in idx]
    if missing:
        raise SystemExit(f"RIO source is missing expected columns: {missing}")
    radius_col = next((h for h in idx if h.startswith("Optimal Radius")), None)

    rows = []
    for r in rows_iter:
        if r is None or all(c is None for c in r):
            continue
        def cell(name):
            return r[idx[name]] if name in idx else None
        rows.append({
            "school_id": str(cell("School #")).strip(),
            "school_name": fold_text(cell("Location Name")),
            "street_address": fold_text(cell("Address 1")),
            "address2": fold_text(cell("Address 2")),
            "suite": fold_text(cell("Suite")),
            "city": fold_text(cell("City")),
            "state": fold_text(cell("Region")).upper(),
            "zip": norm_zip(cell("Post Code")),
            "lat": cell("Lat"),
            "lng": cell("Lng"),
            "url": fold_text(cell("Website")),
            "brand_raw": fold_text(cell("brand")),
            "google_place_id": fold_text(cell("Google Place ID")),
            "optimal_radius": fold_text(cell(radius_col)) if radius_col else "",
        })
    wb.close()
    return rows


def validate_and_flag(rows: list[dict], brand_cfg: dict) -> list[dict]:
    brands = brand_cfg.get("brands", {})
    domain_index = _domain_brand_index(brand_cfg)

    url_counts = Counter(norm_url(r["url"]) for r in rows if r["url"])

    out = []
    for r in rows:
        flags = []

        # --- brand ---
        brand = r["brand_raw"]
        if not brand or brand.lower() in ("none", "null", "n/a"):
            flags.append("BRAND_MISSING")
            host = urlparse(r["url"]).netloc.lower().removeprefix("www.")
            inferred = domain_index.get(host)
            if inferred:
                brand = inferred
                flags.append("BRAND_INFERRED_FROM_DOMAIN")
            else:
                brand = ""
        if brand and brand not in brands:
            flags.append("BRAND_UNKNOWN")
        brand_display = brands.get(brand, {}).get("display_name", brand)

        # --- url ---
        if r["url"] and url_counts[norm_url(r["url"])] > 1:
            flags.append("DATA_DUP_URL")
        parsed = urlparse(r["url"])
        m = URL_SLUG_RX.search(parsed.path)
        if m:
            # space-insensitive compare: slugs write "highpoint" / "pinebrook"
            slug_city = slug_to_key(m.group("city")).replace(" ", "")
            rio_city = norm_key(r["city"]).replace(" ", "")
            if slug_city != rio_city:
                flags.append("CITY_URL_MISMATCH")
            if m.group("st").upper() != r["state"]:
                flags.append("STATE_URL_MISMATCH")
            # URLs zero-pad low school numbers ("fairport-ny-0002" for School # 2)
            if int(m.group("num")) != int(r["school_id"]):
                flags.append("ID_URL_MISMATCH")
        else:
            flags.append("URL_PATTERN_OTHER")

        # --- coordinates ---
        if r["lat"] is None or r["lng"] is None:
            flags.append("MISSING_LATLNG")

        out.append({
            "school_id": r["school_id"],
            "brand": brand,
            "brand_display": brand_display,
            "school_name": r["school_name"],
            "url": r["url"],
            "street_address": r["street_address"],
            "address2": r["address2"],
            "suite": r["suite"],
            "city": r["city"],
            "state": r["state"],
            "zip": r["zip"],
            "lat": "" if r["lat"] is None else r["lat"],
            "lng": "" if r["lng"] is None else r["lng"],
            "google_place_id": r["google_place_id"],
            "optimal_radius": r["optimal_radius"],
            "flags": ";".join(flags),
        })
    return out


def write_outputs(rows: list[dict], source: Path) -> dict:
    rows = sorted(rows, key=lambda x: int(x["school_id"]))
    MASTER_CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(MASTER_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=MASTER_COLUMNS)
        w.writeheader()
        w.writerows(rows)

    flag_counter = Counter()
    for r in rows:
        for fl in r["flags"].split(";"):
            if fl:
                flag_counter[fl] += 1

    dup_urls = sorted({r["url"] for r in rows if "DATA_DUP_URL" in r["flags"]})
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_file": str(source),
        "source_sha256": _sha256_file(source),
        "row_count": len(rows),
        "brand_counts": dict(Counter(r["brand"] for r in rows).most_common()),
        "flag_counts": dict(flag_counter.most_common()),
        "duplicate_urls": dup_urls,
        "flagged_rows": [
            {"school_id": r["school_id"], "city": r["city"], "state": r["state"],
             "url": r["url"], "flags": r["flags"]}
            for r in rows
            if r["flags"] and r["flags"] != "URL_PATTERN_OTHER"
        ],
    }
    REPORT_JSON.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main():
    ap = argparse.ArgumentParser(description="Ingest RIO location export into master_schools.csv")
    ap.add_argument("--source", default=str(DEFAULT_SOURCE))
    args = ap.parse_args()
    source = Path(args.source)
    if not source.exists():
        raise SystemExit(f"Source not found: {source}")

    brand_cfg = load_brand_config()
    rows = load_rio_rows(source)
    flagged = validate_and_flag(rows, brand_cfg)
    report = write_outputs(flagged, source)

    print(f"master_schools.csv rows: {report['row_count']}")
    print(f"brands: {report['brand_counts']}")
    print(f"flags:  {report['flag_counts']}")
    print(f"duplicate urls: {len(report['duplicate_urls'])}")
    print(f"report: {REPORT_JSON}")


if __name__ == "__main__":
    main()
