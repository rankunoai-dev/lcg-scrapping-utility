"""S1b — Verified Locality Register builder (GEO_04 ground truth, Tier 2).

Distills the loc-intel batch workbooks into the only locality signal this
project is allowed to consume (see ARCHITECTURE.md §1-§3):

  * LOCATED_IN rows only  — measured 100% stable across the tool's two runs.
    NEARBY rows (all >5 mi) go to a diagnostics file; they earn no scoring
    credit and are never prescribed.
  * latest run wins       — per school, rows from the newest workbook timestamp.
  * joined against Tier-0 — a locality equal to the school's RIO city is the
    tool's documented city-fallback, NOT a real sub-city locality: those
    schools go to the city-duplicates list (GEO_04 = N/A).

Outputs (all under data/):
  verified_locality_register.csv   true sub-city localities, one row per school,
                                   with approval columns for Gate G1. Existing
                                   approvals are PRESERVED on regeneration.
  locality_city_duplicates.csv     GEO_04 = N/A cohort
  locality_unresolved.csv          RIO schools with no LOCATED_IN + orphan IDs
  nearby_diagnostic_set.json       detection diagnostics only (never prescribed)
  locality_register_report.json    split counts, snapshot hash, approval drift

The snapshot hash covers the candidate fields only (not approvals), sorted by
school_id — regeneration from the same sources must reproduce it exactly.

Usage:
    python -m src.ingest.locality_register [--batch-dir data/source/batch_results]
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import openpyxl

from src.matching.normalize import fold_text, norm_key

BATCH_DIR = Path("data/source/batch_results")
MASTER_CSV = Path("data/master_schools.csv")
REGISTER_CSV = Path("data/verified_locality_register.csv")
CITY_DUP_CSV = Path("data/locality_city_duplicates.csv")
UNRESOLVED_CSV = Path("data/locality_unresolved.csv")
NEARBY_JSON = Path("data/nearby_diagnostic_set.json")
REPORT_JSON = Path("data/locality_register_report.json")

FILE_TS_RX = re.compile(r"_(\d{8})_(\d{6})\.xlsx$", re.I)

REGISTER_COLUMNS = [
    "school_id", "locality", "locality_aliases", "rio_city", "state",
    "relationship", "distance_mi", "source_run_date", "source_workbook",
    "seo_flags", "auto_review_flags", "snapshot_hash",
    "approved", "approved_by", "approved_date", "analyst_notes",
]
APPROVAL_FIELDS = ["approved", "approved_by", "approved_date", "analyst_notes", "locality_aliases"]


def load_master() -> dict[str, dict]:
    if not MASTER_CSV.exists():
        raise SystemExit(f"{MASTER_CSV} not found — run src.ingest.rio_loader first (S1a before S1b).")
    with open(MASTER_CSV, newline="", encoding="utf-8") as f:
        return {r["school_id"]: r for r in csv.DictReader(f)}


def scan_workbooks(batch_dir: Path):
    """Return (located_in, nearby) with latest-run-wins semantics per school.

    located_in: school_id -> {"rows": [row dicts], "ts": str, "workbook": str}
    nearby:     school_id -> {"rows": [...], "ts": str}
    """
    files = sorted(p for p in batch_dir.glob("*.xlsx") if not p.name.startswith("~$"))
    if not files:
        raise SystemExit(f"No workbooks found in {batch_dir}")

    located_in: dict[str, dict] = {}
    nearby: dict[str, dict] = {}

    for path in files:
        m = FILE_TS_RX.search(path.name)
        ts = (m.group(1) + m.group(2)) if m else "00000000000000"
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        if "School-Neighbourhood" not in wb.sheetnames:
            wb.close()
            continue
        ws = wb["School-Neighbourhood"]
        rows_iter = ws.iter_rows(values_only=True)
        header = [str(h) for h in next(rows_iter)]
        col = {h: i for i, h in enumerate(header)}

        per_school: dict[str, list[dict]] = defaultdict(list)
        for r in rows_iter:
            if not r or r[0] is None:
                continue
            per_school[str(r[0]).strip()].append({
                "locality": fold_text(r[col["Neighbourhood"]]),
                "city": fold_text(r[col["City"]]),
                "state": fold_text(r[col["State"]]),
                "relationship": fold_text(r[col["Relationship"]]),
                "distance_mi": r[col["Distance (mi)"]],
                "is_border": fold_text(r[col.get("Is Border Area", -1)]) if "Is Border Area" in col else "",
                "seo_flag": fold_text(r[col.get("SEO Flag", -1)]) if "SEO Flag" in col else "",
            })
        wb.close()

        for sid, rows in per_school.items():
            loc_rows = [x for x in rows if x["relationship"] == "LOCATED_IN"]
            near_rows = [x for x in rows if x["relationship"] != "LOCATED_IN"]
            if loc_rows and (sid not in located_in or ts > located_in[sid]["ts"]):
                located_in[sid] = {"rows": loc_rows, "ts": ts, "workbook": path.name}
            if near_rows and (sid not in nearby or ts > nearby[sid]["ts"]):
                nearby[sid] = {"rows": near_rows, "ts": ts}

    return located_in, nearby, [p.name for p in files]


def auto_review_flags(locality: str, rio_city: str, seo_flags: str) -> str:
    """Deterministic pre-screen hints for the G1 analyst pass (never block detection)."""
    flags = []
    lkey, ckey = norm_key(locality), norm_key(rio_city)
    if len(locality.split()) >= 3:
        flags.append("REVIEW_SUBDIVISION_LIKE")
    if lkey != ckey and (lkey in ckey.split() or ckey in lkey.split() or lkey in ckey or ckey in lkey):
        flags.append("REVIEW_CITY_VARIANT")  # e.g. locality "Lacey" vs RIO city "Lacey North"
    if seo_flags:
        flags.append("REVIEW_SEO_FLAG")
    return ";".join(flags)


def load_existing_approvals() -> dict[tuple, dict]:
    if not REGISTER_CSV.exists():
        return {}
    with open(REGISTER_CSV, newline="", encoding="utf-8") as f:
        return {
            (r["school_id"], norm_key(r["locality"])): {k: r.get(k, "") for k in APPROVAL_FIELDS}
            for r in csv.DictReader(f)
        }


def build(batch_dir: Path):
    master = load_master()
    located_in, nearby, files_scanned = scan_workbooks(batch_dir)
    existing = load_existing_approvals()

    register, city_dups, unresolved = [], [], []
    multi_located = []

    for sid, entry in sorted(located_in.items(), key=lambda kv: kv[0]):
        school = master.get(sid)
        if school is None:
            for row in entry["rows"]:
                unresolved.append({"school_id": sid, "reason": "NOT_IN_RIO",
                                   "detail": f"locality={row['locality']} ({entry['workbook']})"})
            continue

        names = {norm_key(r["locality"]): r for r in entry["rows"]}
        if len(names) > 1:
            multi_located.append(sid)
        for _, row in sorted(names.items()):
            seo = ";".join(x for x in [row["seo_flag"], "BORDER_AREA" if row["is_border"] == "Yes" else ""] if x)
            if norm_key(row["locality"]) == norm_key(school["city"]):
                city_dups.append({
                    "school_id": sid, "locality": row["locality"], "rio_city": school["city"],
                    "state": school["state"], "source_run_date": entry["ts"][:8],
                    "note": "city-fallback — no verified sub-city locality; GEO_04 = N/A",
                })
            else:
                register.append({
                    "school_id": sid,
                    "locality": row["locality"],
                    "locality_aliases": "",
                    "rio_city": school["city"],
                    "state": school["state"],
                    "relationship": "LOCATED_IN",
                    "distance_mi": "" if row["distance_mi"] is None else row["distance_mi"],
                    "source_run_date": entry["ts"][:8],
                    "source_workbook": entry["workbook"],
                    "seo_flags": seo,
                    "auto_review_flags": auto_review_flags(row["locality"], school["city"], seo),
                    "approved": "", "approved_by": "", "approved_date": "", "analyst_notes": "",
                })

    for sid, school in sorted(master.items(), key=lambda kv: kv[0]):
        if sid not in located_in:
            unresolved.append({"school_id": sid, "reason": "NO_LOCATED_IN",
                               "detail": f"{school['city']}, {school['state']} ({school['brand']})"})

    # --- snapshot hash over candidate fields only, sorted ---
    candidate_fields = ["school_id", "locality", "rio_city", "state", "relationship",
                        "source_run_date", "seo_flags"]
    canon = json.dumps(
        [{k: r[k] for k in candidate_fields} for r in sorted(register, key=lambda r: (int(r["school_id"]), r["locality"]))],
        sort_keys=True, ensure_ascii=False,
    )
    snapshot_hash = hashlib.sha256(canon.encode("utf-8")).hexdigest()

    # --- merge preserved approvals; track drift ---
    approval_drift = []
    matched_keys = set()
    for r in register:
        r["snapshot_hash"] = snapshot_hash
        key = (r["school_id"], norm_key(r["locality"]))
        if key in existing:
            prior = existing[key]
            for k in APPROVAL_FIELDS:
                r[k] = prior.get(k, "")
            matched_keys.add(key)
    for key, prior in existing.items():
        if key not in matched_keys and prior.get("approved"):
            approval_drift.append({"school_id": key[0], "locality_key": key[1],
                                   "issue": "APPROVAL_ORPHANED — approved row no longer produced by sources"})

    nearby_out = {
        sid: [{"name": r["locality"], "distance_mi": r["distance_mi"], "relationship": r["relationship"]}
              for r in entry["rows"]]
        for sid, entry in sorted(nearby.items(), key=lambda kv: kv[0])
    }

    return register, city_dups, unresolved, nearby_out, snapshot_hash, approval_drift, files_scanned, multi_located


def write_csv(path: Path, rows: list[dict], columns: list[str]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=columns)
        w.writeheader()
        w.writerows(rows)


def main():
    ap = argparse.ArgumentParser(description="Build the Verified Locality Register from loc-intel workbooks")
    ap.add_argument("--batch-dir", default=str(BATCH_DIR))
    args = ap.parse_args()

    (register, city_dups, unresolved, nearby_out, snapshot_hash,
     approval_drift, files_scanned, multi_located) = build(Path(args.batch_dir))

    write_csv(REGISTER_CSV, sorted(register, key=lambda r: int(r["school_id"])), REGISTER_COLUMNS)
    write_csv(CITY_DUP_CSV, sorted(city_dups, key=lambda r: int(r["school_id"])),
              ["school_id", "locality", "rio_city", "state", "source_run_date", "note"])
    write_csv(UNRESOLVED_CSV, sorted(unresolved, key=lambda r: (r["reason"], r["school_id"])),
              ["school_id", "reason", "detail"])
    NEARBY_JSON.write_text(json.dumps(nearby_out, indent=1, ensure_ascii=False), encoding="utf-8")

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "files_scanned": len(files_scanned),
        "snapshot_hash": snapshot_hash,
        "split": {
            "register_true_localities": len(register),
            "city_duplicates_na_cohort": len(city_dups),
            "unresolved": dict(Counter(u["reason"] for u in unresolved)),
        },
        "schools_with_multiple_located_in": multi_located,
        "approvals": {
            "approved": sum(1 for r in register if r["approved"].strip().lower() == "yes"),
            "pending": sum(1 for r in register if not r["approved"].strip()),
            "rejected": sum(1 for r in register if r["approved"].strip().lower() == "no"),
            "drift": approval_drift,
        },
        "auto_review_flag_counts": dict(Counter(
            fl for r in register for fl in r["auto_review_flags"].split(";") if fl
        )),
        "nearby_diagnostics": {"schools": len(nearby_out),
                               "pairs": sum(len(v) for v in nearby_out.values())},
    }
    REPORT_JSON.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"register (true sub-city localities): {len(register)}")
    print(f"city-duplicates (GEO_04 = N/A):      {len(city_dups)}")
    print(f"unresolved:                          {report['split']['unresolved']}")
    print(f"multiple LOCATED_IN schools:         {len(multi_located)}")
    print(f"snapshot_hash: {snapshot_hash}")
    print(f"auto-review flags: {report['auto_review_flag_counts']}")
    if approval_drift:
        print(f"!! APPROVAL DRIFT: {approval_drift}")


if __name__ == "__main__":
    main()
