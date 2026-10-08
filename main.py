"""lcg-utility CLI orchestrator v2 — wires the S1–S8 pipeline end to end.

Per-school lifecycle (each stage persisted, resumable):
    FETCH -> EXTRACT -> DETERMINISTIC -> SEMANTIC -> SCORE -> observation JSON
then one aggregation pass: 7-sheet workbook + output validator.

Stage flags:
    --fetch-only      fetch+cache HTML, skip evaluation
    --report-only     build workbook from existing observations (no fetching)
    --skip-semantic   deterministic only; semantic params scored N_A
    --force           ignore caches (refetch + re-evaluate)
    --schools 1,2,3   restrict to specific school ids
Caching: a school is skipped when its observation exists AND its config_hash
matches the current config (change any config/SKILL file -> auto re-eval).
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

from src.evaluator.deterministic_checker import DeterministicChecker
from src.evaluator.llm_client import LLMClient
from src.evaluator.semantic_evaluator import SEMANTIC_PARAM_IDS, SemanticEvaluator
from src.ingest.welcome_source import ClientWelcomeSource, apply_client_welcome
from src.matching.geo_matcher import LocalityResolver
from src.qa.output_validator import OutputValidator, load_locality_artifacts
from src.reporter.report_generator import ReportGenerator
from src.scraper.page_fetcher import PageFetcher
from src.scraper.run_manifest import RunManifest
from src.scraper.section_extractor import SectionExtractor
from src.scoring.scoring_engine import ScoringEngine, compute_config_hash

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("lcg-utility")


def load_configs():
    params = json.loads(Path("config/parameters_database.json")
                        .read_text(encoding="utf-8"))["parameters"]
    brand_cfg = json.loads(Path("config/brand_defaults.json")
                           .read_text(encoding="utf-8"))
    return params, brand_cfg


def load_schools(csv_path: str, only_ids: set[str] | None) -> list[dict]:
    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if only_ids:
        rows = [r for r in rows if r["school_id"] in only_ids]
    return rows


def skipped_semantic_results(reason: str) -> dict:
    return {
        pid: {"welcome_status": "N_A", "rest_status": "N_A", "page_status": "N_A",
              "welcome_evidence": None, "page_evidence": None,
              "source_tier": "LLM_SEMANTIC", "provenance": reason,
              "flags": ["SEMANTIC_SKIPPED"], "counts": {}, "method": "SEMANTIC",
              "confidence": None}
        for pid in SEMANTIC_PARAM_IDS
    }


def _cached_needs_semantic_rerun(obs: dict) -> bool:
    """A budget-starved/failed semantic pass must not freeze into the cache."""
    return any("SEMANTIC_FAILED" in f or "SEMANTIC_SKIPPED" in f
               for f in obs.get("flags", []))


async def process_school(school, fetcher, extractor, checker, sem_eval,
                         engine, args, llm_rollup, client_welcome=None) -> dict | None:
    sid = str(school["school_id"])
    if not args.force and engine.is_observed(sid):
        obs = engine.load_observation(sid)
        if args.skip_semantic or not _cached_needs_semantic_rerun(obs):
            logger.info("[%s] observation cached (config hash match)", sid)
            return obs
        logger.info("[%s] cached observation lacks semantic results — re-evaluating "
                    "from cached HTML", sid)

    fetch = await fetcher.fetch(school, force=args.force)
    if not fetch["ok"]:
        logger.warning("[%s] fetch failed: %s", sid, fetch["reason"])
        return None
    if args.fetch_only:
        return None

    meta_path = Path("data/raw_html") / f"{sid}.meta.json"
    fetch_meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    extracted = extractor.extract(sid, fetch["html"], brand=school.get("brand", ""),
                                  school_name=school.get("school_name", ""),
                                  fetch_meta=fetch_meta)
    # Welcome scope source of truth: the client CMS export (Tier 0);
    # scraped welcome remains the fallback and the cross-check.
    if client_welcome is not None:
        extracted = apply_client_welcome(extracted, client_welcome.get(sid))
        Path(f"data/extracted_sections/{sid}.json").write_text(
            json.dumps(extracted, indent=1, ensure_ascii=False), encoding="utf-8")

    det = checker.evaluate(extracted, school)

    if args.skip_semantic:
        sem_results, llm_meta = skipped_semantic_results(
            "Semantic evaluation skipped (--skip-semantic)"), None
    else:
        sem_results, llm_meta = await asyncio.to_thread(
            sem_eval.evaluate, extracted, school, det)
        if llm_meta:
            llm_rollup["model"] = llm_meta.model
            llm_rollup["input_tokens"] = llm_rollup.get("input_tokens", 0) + llm_meta.input_tokens
            llm_rollup["output_tokens"] = llm_rollup.get("output_tokens", 0) + llm_meta.output_tokens

    merged = {**det, **sem_results}
    scorecard = engine.compute_school_scorecard(
        sid, extracted, merged, school_meta=school,
        llm_meta={
            "model": llm_meta.model,
            "request_id": llm_meta.request_id,
            "input_tokens": llm_meta.input_tokens,
            "output_tokens": llm_meta.output_tokens,
        } if llm_meta else {})
    logger.info("[%s] scored: welcome=%s page=%s gap=%s band=%s", sid,
                scorecard["s_welcome"], scorecard["s_page"], scorecard["s_gap"],
                scorecard["priority_band"])
    return scorecard


def collect_observations(engine: ScoringEngine, ids: list[str] | None) -> list[dict]:
    cards = []
    for p in sorted(engine.observations_dir.glob("*_eval.json")):
        sid = p.stem.replace("_eval", "")
        if ids and sid not in ids:
            continue
        try:
            cards.append(json.loads(p.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            logger.warning("Corrupt observation skipped: %s", p)
    return cards


async def main_async(args):
    params, brand_cfg = load_configs()
    config_hash = compute_config_hash()
    only = {x.strip() for x in args.schools.split(",")} if args.schools else None
    schools = load_schools(args.csv, only)
    logger.info("Schools in scope: %d | config hash: %s", len(schools), config_hash)

    # ---- credit guardrail 1: pre-spend school cap -----------------------
    # An LLM-evaluating run larger than DEFAULT_MAX_SCHOOLS refuses to start
    # unless --all-schools is explicit. Test runs stay small by default.
    llm_active = not (args.skip_semantic or args.fetch_only or args.report_only)
    school_cap = int(os.environ.get("DEFAULT_MAX_SCHOOLS", "10"))
    if llm_active and len(schools) > school_cap and not args.all_schools:
        logger.error(
            "GUARDRAIL: %d schools requested with LLM evaluation active, but the "
            "safety cap is %d (env DEFAULT_MAX_SCHOOLS). Pass --all-schools to "
            "run the full batch deliberately, or --skip-semantic for a free "
            "deterministic-only run.", len(schools), school_cap)
        raise SystemExit(2)

    llm_client = LLMClient(max_cost_usd=args.max_cost)
    if llm_active:
        logger.info("Credit guardrails: model=%s | session cost cap=$%.2f | "
                    "school cap=%s", llm_client.model, llm_client.max_cost_usd,
                    "OFF (--all-schools)" if args.all_schools else school_cap)

    resolver = LocalityResolver("data")
    checker = DeterministicChecker(params, brand_cfg, resolver)
    sem_eval = SemanticEvaluator(params, brand_cfg, llm_client=llm_client)
    engine = ScoringEngine(params, config_hash=config_hash)
    extractor = SectionExtractor()
    manifest = RunManifest()
    llm_rollup: dict = {}
    client_welcome = ClientWelcomeSource()
    if client_welcome.data:
        logger.info("Client CMS welcome text loaded for %d schools "
                    "(authoritative welcome scope)", len(client_welcome.data))

    scorecards: list[dict] = []
    if not args.report_only:
        sem = asyncio.Semaphore(args.concurrency)
        async with PageFetcher(brand_cfg, manifest=manifest) as fetcher:
            async def worker(s):
                async with sem:
                    try:
                        return await process_school(s, fetcher, extractor, checker,
                                                    sem_eval, engine, args, llm_rollup,
                                                    client_welcome=client_welcome)
                    except Exception:
                        logger.exception("[%s] pipeline error", s.get("school_id"))
                        await manifest.update(str(s.get("school_id")), "FAILED",
                                              error_class="PIPELINE_EXCEPTION")
                        return None
            results = await asyncio.gather(*(worker(s) for s in schools),
                                           return_exceptions=False)
        scorecards = [r for r in results if r]
        if args.fetch_only:
            logger.info("Fetch-only run complete. Manifest: %s", manifest.summary())
            return

        # ---- credit guardrail 2: spend ledger + session summary ---------
        if llm_active:
            spent = round(llm_client.total_cost_usd, 4)
            llm_rollup["cost_usd"] = spent
            ledger_path = Path("data/llm_spend_ledger.json")
            ledger = json.loads(ledger_path.read_text(encoding="utf-8")) \
                if ledger_path.exists() else []
            ledger.append({
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "model": llm_client.model,
                "schools_in_scope": len(schools),
                "input_tokens": llm_rollup.get("input_tokens", 0),
                "output_tokens": llm_rollup.get("output_tokens", 0),
                "session_cost_usd": spent,
                "session_cap_usd": llm_client.max_cost_usd,
            })
            ledger_path.write_text(json.dumps(ledger, indent=1), encoding="utf-8")
            lifetime = round(sum(e.get("session_cost_usd", 0) for e in ledger), 4)
            logger.info("LLM spend this run: $%.4f (cap $%.2f) | lifetime ledger "
                        "total: $%.4f", spent, llm_client.max_cost_usd, lifetime)

    # The workbook always reflects the WHOLE network state on disk, not just
    # this run's slice — chunked runs accumulate into one deliverable.
    scorecards = collect_observations(engine, None)
    if not scorecards:
        logger.error("No observations available to report on.")
        return

    register, nearby = load_locality_artifacts("data")
    ingest_rows = []
    ingest_report = Path("data/ingest_report.json")
    if ingest_report.exists():
        rep = json.loads(ingest_report.read_text(encoding="utf-8"))
        ingest_rows = [
            {"school_id": fr["school_id"], "issue": "INGEST_FLAG",
             "detail": f"{fr['flags']} | {fr['url']}"}
            for fr in rep.get("flagged_rows", [])
            if fr.get("flags") not in ("", "URL_PATTERN_OTHER")
        ]

    all_schools_map = {r["school_id"]: r["url"] for r in load_schools(args.csv, None)}

    # Client sequencing: rows follow the dropped-pages batch order when the
    # plan exists (Batch 1 -> 47, then the rest of the network).
    school_order, batch_map = [], {}
    plan_path = Path("data/batch_plan.json")
    if plan_path.exists():
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        for bname, ids in plan.get("batches", {}).items():
            for sid in ids:
                if sid in batch_map:
                    continue  # reference lists some schools twice; FIRST position wins
                school_order.append(sid)
                batch_map[sid] = bname
        school_order += [sid for sid in plan.get("rest_of_network", [])
                         if sid not in batch_map]

    reporter = ReportGenerator(output_path=args.output,
                               school_urls=all_schools_map,
                               school_order=school_order, batch_map=batch_map,
                               locality_register=register, nearby_names={
                                   sid: [x["name"] for x in rows]
                                   for sid, rows in json.loads(
                                       Path("data/nearby_diagnostic_set.json")
                                       .read_text(encoding="utf-8")).items()
                               } if Path("data/nearby_diagnostic_set.json").exists() else {})
    out = reporter.generate_report(scorecards, params, run_context={
        "config_hash": config_hash,
        "manifest_summary": manifest.summary(),
        "llm_rollup": llm_rollup,
        "ingest_review_rows": ingest_rows,
    })
    logger.info("Workbook written: %s (%d schools)", out, len(scorecards))

    violations = OutputValidator(register, nearby).validate(out)
    if violations:
        logger.error("OUTPUT VALIDATOR FAILED — %d violation(s):", len(violations))
        for v in violations[:30]:
            logger.error("  %s", v)
        raise SystemExit(1)
    logger.info("Output validator: PASS (V1–V8)")


def main():
    ap = argparse.ArgumentParser(description="LCG Local SEO Audit Utility v2")
    ap.add_argument("--csv", default="data/master_schools.csv")
    ap.add_argument("--output", default="LCG_Welcome_SEO_Audit.xlsx")
    ap.add_argument("--schools", help="comma-separated school ids")
    ap.add_argument("--concurrency", type=int, default=5)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--fetch-only", action="store_true")
    ap.add_argument("--report-only", action="store_true")
    ap.add_argument("--skip-semantic", action="store_true",
                    help="deterministic engine only; semantic params scored N_A")
    ap.add_argument("--max-cost", type=float,
                    default=float(os.environ.get("DEFAULT_MAX_COST_USD", "1.0")),
                    help="hard USD cap on this run's LLM spend (default from "
                         ".env DEFAULT_MAX_COST_USD, else $1.00)")
    ap.add_argument("--all-schools", action="store_true",
                    help="bypass the DEFAULT_MAX_SCHOOLS guardrail for a "
                         "deliberate full-network LLM run")
    args = ap.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
