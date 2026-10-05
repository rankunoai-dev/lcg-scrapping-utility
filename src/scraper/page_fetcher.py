"""S3 — Playwright page fetcher v2.

Design (ARCHITECTURE.md §4.1, fixes defects #4/#5 from README Part 5):
  * ONE shared Chromium instance for the whole run (not one per page).
  * Per-domain politeness on top of the global worker semaphore: at most one
    in-flight request per domain, with a minimum start-gap + jitter
    (brand sites concentrate hundreds of schools on a single domain).
  * NO plain-HTTP fallback — verified 2026-10-05: these sites tarpit
    non-browser clients (curl hung past its own timeout and died).
  * Validation gates BEFORE anything is cached; failures land in
    data/quarantine/ with a reason, never in the clean cache. This is the
    cache-poisoning defense: a cache hit requires a PASSED sidecar meta file.
  * Every outcome is recorded in the run manifest.

Cache layout:
  data/raw_html/{id}.html + {id}.meta.json   validated snapshots only
  data/quarantine/{id}.html + {id}.meta.json rejected fetches, kept for triage

CLI (S3 gate):
  python -m src.scraper.page_fetcher --sample-per-domain   # 1 school per brand domain
  python -m src.scraper.page_fetcher --schools 7440,3034 [--force]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
import logging

from src.matching.normalize import norm_key, norm_url
from src.scraper.run_manifest import RunManifest

logger = logging.getLogger(__name__)

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")
MIN_HTML_CHARS = 15_000
_TITLE_RX = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_H1_RX = re.compile(r"<h1[^>]*>(.*?)</h1>", re.I | re.S)
_TAG_RX = re.compile(r"<[^>]+>")


class DomainThrottle:
    """At most one in-flight request per domain, min start-gap + jitter."""

    def __init__(self, min_delay: float = 1.0, jitter: float = 0.5):
        self.min_delay = min_delay
        self.jitter = jitter
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_start: dict[str, float] = {}

    def _lock(self, domain: str) -> asyncio.Lock:
        return self._locks.setdefault(domain, asyncio.Lock())

    def slot(self, domain: str):
        throttle = self

        class _Slot:
            async def __aenter__(self):
                await throttle._lock(domain).acquire()
                gap = throttle.min_delay + random.uniform(0, throttle.jitter)
                wait = throttle._last_start.get(domain, 0.0) + gap - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                throttle._last_start[domain] = time.monotonic()

            async def __aexit__(self, *exc):
                throttle._lock(domain).release()

        return _Slot()


class PageFetcher:
    def __init__(self, brand_cfg: dict,
                 raw_html_dir: str = "data/raw_html",
                 quarantine_dir: str = "data/quarantine",
                 manifest: RunManifest | None = None,
                 block_resources: bool = True):
        self.brand_cfg = brand_cfg
        self.raw_dir = Path(raw_html_dir)
        self.quarantine_dir = Path(quarantine_dir)
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.quarantine_dir.mkdir(parents=True, exist_ok=True)
        self.manifest = manifest or RunManifest()
        self.block_resources = block_resources
        self.timeout_sec = brand_cfg.get("request_timeout_seconds", 45)
        self.max_retries = brand_cfg.get("max_retries", 3)
        self.throttle = DomainThrottle(
            min_delay=brand_cfg.get("per_domain_min_delay_seconds", 1.0))
        self._pw = None
        self._browser = None
        self._context = None

    # ------------------------------------------------------------- lifecycle
    async def __aenter__(self):
        from playwright.async_api import async_playwright
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=True)
        self._context = await self._browser.new_context(
            user_agent=USER_AGENT, viewport={"width": 1366, "height": 900})
        if self.block_resources:
            await self._context.route(
                "**/*",
                lambda route: route.abort()
                if route.request.resource_type in ("image", "media", "font")
                else route.continue_(),
            )
        return self

    async def __aexit__(self, *exc):
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()

    # ----------------------------------------------------------------- cache
    def _paths(self, school_id: str, quarantine: bool = False):
        base = self.quarantine_dir if quarantine else self.raw_dir
        return base / f"{school_id}.html", base / f"{school_id}.meta.json"

    def is_cached(self, school_id: str) -> bool:
        html_p, meta_p = self._paths(school_id)
        if not (html_p.exists() and meta_p.exists()):
            return False
        try:
            meta = json.loads(meta_p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return False
        return meta.get("validation") == "PASSED"

    def load_cached(self, school_id: str) -> str:
        return self._paths(school_id)[0].read_text(encoding="utf-8")

    def _store(self, school_id: str, html: str, meta: dict, quarantine: bool):
        html_p, meta_p = self._paths(school_id, quarantine)
        html_p.write_text(html, encoding="utf-8")
        meta_p.write_text(json.dumps(meta, indent=1), encoding="utf-8")

    # ------------------------------------------------------------ validation
    def validate(self, school: dict, requested: str, final_url: str,
                 status: int, html: str) -> tuple[bool, str]:
        if status != 200:
            return False, f"HTTP_{status}"
        if len(html) < MIN_HTML_CHARS:
            return False, "CONTENT_TOO_SHORT"

        if norm_url(final_url) != norm_url(requested):
            # Redirected somewhere: still this school's page only if the school
            # number survives in the final path (zero-padding tolerated).
            sid = int(school["school_id"])
            nums = [int(n) for n in re.findall(r"\d{2,6}", urlparse(final_url).path)]
            if sid not in nums:
                return False, "REDIRECTED_OFF_PAGE"

        # Identity: title or first H1 must mention the brand or the city.
        tm = _TITLE_RX.search(html)
        hm = _H1_RX.search(html)
        identity_text = norm_key(_TAG_RX.sub(" ", (tm.group(1) if tm else "") + " " +
                                             (hm.group(1) if hm else "")))
        brand_entry = self.brand_cfg.get("brands", {}).get(school.get("brand", ""), {})
        needles = [school.get("city", ""), brand_entry.get("display_name", ""),
                   school.get("brand", "")] + list(brand_entry.get("aliases", []))
        if not any(norm_key(n) and norm_key(n) in identity_text for n in needles):
            return False, "IDENTITY_MISMATCH"
        # Diagnostic (not a gate): brand alone also matches on a *different*
        # school of the same brand — e.g. school 646's known-bad URL serves
        # school 9108's page. Rows flagged DATA_DUP_URL/ID_URL_MISMATCH at
        # ingest plus city_in_identity=False in the meta make that visible.
        self._last_city_in_identity = bool(
            norm_key(school.get("city", "")) and
            norm_key(school.get("city", "")) in identity_text)
        return True, "PASSED"

    # ----------------------------------------------------------------- fetch
    async def fetch(self, school: dict, force: bool = False) -> dict:
        """Fetch one school. Returns {'ok', 'school_id', 'reason', 'html'?}."""
        school_id = str(school["school_id"])
        url = school["url"]
        if not force and self.is_cached(school_id):
            await self.manifest.update(school_id, "FETCHED", cache_hit=True)
            return {"ok": True, "school_id": school_id, "reason": "CACHE_HIT",
                    "html": self.load_cached(school_id)}

        domain = urlparse(url).netloc.lower()
        last_reason = "UNKNOWN"
        for attempt in range(1, self.max_retries + 1):
            async with self.throttle.slot(domain):
                try:
                    page = await self._context.new_page()
                    try:
                        resp = await page.goto(url, timeout=self.timeout_sec * 1000,
                                               wait_until="domcontentloaded")
                        try:
                            await page.wait_for_load_state("networkidle", timeout=8000)
                        except Exception:
                            pass  # settle is best-effort; dynamic pages may never idle
                        await asyncio.sleep(0.5)
                        html = await page.content()
                        status = resp.status if resp else 0
                        final_url = page.url
                    finally:
                        await page.close()
                except Exception as e:
                    last_reason = f"FETCH_ERROR:{type(e).__name__}"
                    logger.warning("[%s] attempt %d/%d failed: %s",
                                   school_id, attempt, self.max_retries, e)
                    await asyncio.sleep(min(2 ** attempt, 15))
                    continue

            meta = {
                "school_id": school_id, "requested_url": url, "final_url": final_url,
                "http_status": status, "content_length": len(html),
                "fetch_ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "attempt": attempt, "user_agent": USER_AGENT,
            }
            self._last_city_in_identity = None
            ok, reason = self.validate(school, url, final_url, status, html)
            meta["validation"] = reason
            meta["city_in_identity"] = self._last_city_in_identity
            if ok:
                self._store(school_id, html, meta, quarantine=False)
                await self.manifest.update(school_id, "FETCHED", cache_hit=False,
                                           final_url=final_url, http_status=status,
                                           content_length=len(html))
                return {"ok": True, "school_id": school_id, "reason": reason, "html": html}

            last_reason = reason
            self._store(school_id, html, meta, quarantine=True)
            if reason.startswith(("HTTP_403", "HTTP_429")):
                await asyncio.sleep(min(5 * 2 ** attempt, 60))  # likely rate-limited
                continue
            if reason in ("REDIRECTED_OFF_PAGE", "IDENTITY_MISMATCH"):
                break  # deterministic outcome; retrying won't change it

        state = "QUARANTINED" if last_reason not in ("UNKNOWN",) and \
            not last_reason.startswith("FETCH_ERROR") else "FETCH_FAILED"
        await self.manifest.update(school_id, state, error_class=last_reason, final_url=url)
        return {"ok": False, "school_id": school_id, "reason": last_reason}


# --------------------------------------------------------------------- CLI
def _load_schools() -> list[dict]:
    import csv
    with open("data/master_schools.csv", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _sample_per_domain(schools: list[dict]) -> list[dict]:
    """One school per brand domain; prefer clean rows; keep 7440 for lapetite."""
    by_domain: dict[str, dict] = {}
    for s in schools:
        d = urlparse(s["url"]).netloc.lower().removeprefix("www.")
        clean = s["flags"] in ("", "URL_PATTERN_OTHER")
        cur = by_domain.get(d)
        if d == "lapetite.com":
            if s["school_id"] == "7440":
                by_domain[d] = s
            elif cur is None:
                by_domain[d] = s
        elif cur is None or (clean and cur["flags"] not in ("", "URL_PATTERN_OTHER")):
            if cur is None or clean:
                by_domain[d] = s
    return list(by_domain.values())


async def _main_async(args):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    brand_cfg = json.loads(Path("config/brand_defaults.json").read_text(encoding="utf-8"))
    schools = _load_schools()
    if args.schools:
        wanted = {x.strip() for x in args.schools.split(",")}
        targets = [s for s in schools if s["school_id"] in wanted]
    elif args.sample_per_domain:
        targets = _sample_per_domain(schools)
    else:
        targets = schools

    manifest = RunManifest()
    sem = asyncio.Semaphore(brand_cfg.get("default_concurrency", 5))
    results = []
    async with PageFetcher(brand_cfg, manifest=manifest) as fetcher:
        async def worker(s):
            async with sem:
                r = await fetcher.fetch(s, force=args.force)
                tag = "OK " if r["ok"] else "FAIL"
                print(f"  [{tag}] {s['school_id']:>6} {urlparse(s['url']).netloc:<36} {r['reason']}")
                return r
        results = await asyncio.gather(*(worker(s) for s in targets), return_exceptions=False)

    ok = sum(1 for r in results if r["ok"])
    print(f"\nFetched OK: {ok}/{len(results)}   manifest: {manifest.summary()}")


def main():
    ap = argparse.ArgumentParser(description="lcg-utility page fetcher v2")
    ap.add_argument("--schools", help="comma-separated school ids")
    ap.add_argument("--sample-per-domain", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    asyncio.run(_main_async(args))


if __name__ == "__main__":
    main()
