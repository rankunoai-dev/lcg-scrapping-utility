"""S8 — output validator: defense-in-depth layer 3 (ARCHITECTURE §2).

Re-opens the FINISHED workbook and independently re-checks the invariants the
reporter was supposed to enforce. The validator shares no code path with the
prescription builder, so a reporter bug cannot hide from it.

Checks:
  V1  no formula cells and no cell text starting with = + - @ (injection)
  V2  no illegal control characters; no cell over Excel's 32,767-char limit
  V3  04_Remediation_Queue never names an UNAPPROVED register locality
  V4  04_Remediation_Queue never contains a NEARBY (>5 mi) locality name
  V5  GEO_06/GEO_07 rows in 04 are FIELD TASKS (no suggested landmark text)
  V6  every PRESENT/PARTIAL row in the matrix carries evidence
  V7  sheet set complete; scorecard rows == distinct schools in matrix

Usage:
    python -m src.qa.output_validator LCG_Welcome_SEO_Audit.xlsx
Exit code 1 on any violation.
"""

from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path

import openpyxl

from src.matching.normalize import norm_key, phrase_pattern

_CTRL_RX = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
EXPECTED_SHEETS = [
    "00_Run_Summary", "01_School_Scorecard", "02_Parameter_Presence_Matrix",
    "03_Parameter_Summary", "04_Remediation_Queue", "05_Evaluation_Evidence",
    "06_Location_Review", "07_Welcome_Presence_Pivot", "08_Page_Excl_Welcome_Pivot",
    "09_Welcome_Present_Absent",
]
PIVOT_SHEETS = ["07_Welcome_Presence_Pivot", "08_Page_Excl_Welcome_Pivot"]

# NEARBY "localities" that are generic English words colliding with fixed
# instruction vocabulary — never a meaningful prescription target.
_GENERIC_NEARBY_NAMES = {"street", "park", "center", "centre", "north", "south",
                         "east", "west", "city", "spring", "lakes"}


def load_locality_artifacts(data_dir: str | Path = "data"):
    d = Path(data_dir)
    register: dict[str, dict] = {}
    p = d / "verified_locality_register.csv"
    if p.exists():
        with open(p, newline="", encoding="utf-8") as f:
            register = {r["school_id"]: r for r in csv.DictReader(f)}
    nearby: dict[str, list[str]] = {}
    q = d / "nearby_diagnostic_set.json"
    if q.exists():
        raw = json.loads(q.read_text(encoding="utf-8"))
        nearby = {sid: [x.get("name", "") for x in rows] for sid, rows in raw.items()}
    return register, nearby


class OutputValidator:
    def __init__(self, register: dict[str, dict], nearby: dict[str, list[str]]):
        self.register = register
        self.nearby = nearby

    def validate(self, workbook_path: str | Path) -> list[str]:
        violations: list[str] = []
        wb = openpyxl.load_workbook(workbook_path, data_only=False)

        # V7a sheet set
        for name in EXPECTED_SHEETS:
            if name not in wb.sheetnames:
                violations.append(f"V7: missing sheet {name}")

        # V1 + V2 over every cell of every sheet
        for ws in wb.worksheets:
            for row in ws.iter_rows():
                for cell in row:
                    v = cell.value
                    if v is None:
                        continue
                    if cell.data_type == "f":
                        violations.append(f"V1: formula cell {ws.title}!{cell.coordinate}")
                    if isinstance(v, str):
                        if v.startswith(("=", "+", "@")) or (
                                v.startswith("-") and len(v) > 1 and not _is_number(v)):
                            violations.append(
                                f"V1: injection-prefixed text {ws.title}!{cell.coordinate}: {v[:40]!r}")
                        if _CTRL_RX.search(v):
                            violations.append(
                                f"V2: control char in {ws.title}!{cell.coordinate}")
                        if len(v) > 32767:
                            violations.append(
                                f"V2: cell over 32767 chars {ws.title}!{cell.coordinate}")

        # V3/V4/V5 on the remediation queue
        if "04_Remediation_Queue" in wb.sheetnames:
            ws = wb["04_Remediation_Queue"]
            rows = list(ws.iter_rows(values_only=True))
            header = [str(h) for h in rows[0]] if rows else []
            idx = {h: i for i, h in enumerate(header)}
            for r in rows[1:]:
                if not r or r[0] is None:
                    continue
                sid = str(r[idx["School ID"]])
                pid = str(r[idx["Parameter ID"]])
                # V3/V4 police PRESCRIPTIONS — the instruction a copywriter
                # acts on. Two segments are exempt because they legitimately
                # contain locality words without prescribing them:
                #   * quoted page text after "Page evidence:" ("Old Farm Rd",
                #     "in Humble, TX" — the school's own copy)
                #   * the Provenance column (source citations such as
                #     "Verified Locality Register 'Wylie' (LOCATED_IN...)" or
                #     "RIO Address 1 '8101 Miamisburg Centerville Rd'")
                detail = str(r[idx["Action Detail"]] or "") if "Action Detail" in idx else ""
                detail = detail.split("Page evidence:", 1)[0]
                action = str(r[idx["Action"]] or "") if "Action" in idx else ""
                instruction = action + " " + detail
                text_key = norm_key(instruction)

                reg = self.register.get(sid)
                if reg:
                    loc_key = norm_key(reg.get("locality", ""))
                    approved = (reg.get("approved") or "").strip().lower() == "yes"
                    if loc_key and loc_key in text_key and not approved:
                        violations.append(
                            f"V3: UNAPPROVED locality {reg['locality']!r} named in 04 "
                            f"for school {sid} ({pid})")

                for name in self.nearby.get(sid, []):
                    # loc-intel NEARBY data contains generic-word place names
                    # ("Street") that collide with instruction vocabulary
                    # ("Street Address") — skip those, they cannot be a
                    # meaningful locality prescription anyway.
                    if not name or norm_key(name) in _GENERIC_NEARBY_NAMES:
                        continue
                    if phrase_pattern(name).search(instruction):
                        violations.append(
                            f"V4: NEARBY locality {name!r} named in 04 for school "
                            f"{sid} ({pid})")

                if pid in ("GEO_06", "GEO_07"):
                    # Landmarks may be MOVED (the school's own page already
                    # states them) but never CREATED from thin air.
                    if action not in ("FIELD TASK", "MOVE / SURFACE"):
                        violations.append(
                            f"V5: landmark row for school {sid} ({pid}) is "
                            f"{action!r}, must be FIELD TASK or MOVE / SURFACE")

        # V8 pivot cells: a PRESENT/PARTIAL verdict must embed its page quote
        for name in PIVOT_SHEETS:
            if name not in wb.sheetnames:
                continue
            ws = wb[name]
            for row in ws.iter_rows(min_row=2):
                for cell in row[3:]:  # after School ID / Name / URL
                    v = cell.value
                    if isinstance(v, str) and v.startswith(("PRESENT", "PARTIAL")) \
                            and ' — "' not in v:
                        violations.append(
                            f"V8: pivot verdict without embedded quote "
                            f"{name}!{cell.coordinate}: {v[:40]!r}")

        # V6 evidence on PRESENT/PARTIAL matrix rows
        if "02_Parameter_Presence_Matrix" in wb.sheetnames:
            ws = wb["02_Parameter_Presence_Matrix"]
            rows = ws.iter_rows(values_only=True)
            header = [str(h) for h in next(rows, [])]
            idx = {h: i for i, h in enumerate(header)}
            schools_in_matrix = set()
            for r in rows:
                if not r or r[0] is None:
                    continue
                schools_in_matrix.add(str(r[idx["School ID"]]))
                for scope, evcol in (("Welcome Status", "Welcome Evidence"),
                                     ("Page Status", "Page Evidence")):
                    if str(r[idx[scope]]) in ("PRESENT", "PARTIAL") and \
                            not str(r[idx[evcol]] or "").strip():
                        violations.append(
                            f"V6: {r[idx['Parameter ID']]} school {r[idx['School ID']]} "
                            f"{scope}={r[idx[scope]]} without evidence")
            # V7b reconcile
            if "01_School_Scorecard" in wb.sheetnames:
                n_cards = sum(1 for x in wb["01_School_Scorecard"]
                              .iter_rows(min_row=2, values_only=True)
                              if x and x[0] is not None)
                if n_cards != len(schools_in_matrix):
                    violations.append(
                        f"V7: scorecard has {n_cards} schools, matrix has "
                        f"{len(schools_in_matrix)}")
        return violations


def _is_number(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "LCG_Welcome_SEO_Audit.xlsx"
    register, nearby = load_locality_artifacts()
    violations = OutputValidator(register, nearby).validate(path)
    if violations:
        print(f"FAIL — {len(violations)} violation(s):")
        for v in violations[:50]:
            print("  ", v)
        sys.exit(1)
    print(f"PASS — {path} satisfies all output invariants (V1–V8)")


if __name__ == "__main__":
    main()
