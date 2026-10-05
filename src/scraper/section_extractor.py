"""S4 — Section extractor v2: three channels + welcome_found, no fake fallbacks.

Fixes defects #6/#7 from README Part 5 and implements the ARCHITECTURE §4.1
contract:

  welcome_text      the Welcome section only (brand selector, else heading walk)
  body_text         main content: body minus boilerplate (header/nav/footer/
                    cookie/modal/nearby-school widgets), welcome INCLUDED
  body_rest_text    body_text minus the Welcome container — the "elsewhere on
                    page" dimension that makes WELCOME_ONLY reachable in the
                    four-state placement model
  boilerplate_text  everything stripped out of body_text (diagnostics only;
                    geo/keyword checks must never receive it)

Hard rules:
  * If no Welcome section is found, welcome_found=False and welcome_text=""
    — NEVER a "first N words" substitute (that fabricated Welcome sections
    out of nav text and poisoned STR_01/STR_02).
  * Candidate containers are rejected when smaller than `welcome_min_words`
    or larger than `welcome_max_body_fraction` of the body text (a wrapper
    div grabbing the whole page must not silently zero the placement gap).
  * Exact-duplicate blocks are removed (verified: the staff carousel renders
    its content 3x in the DOM).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from bs4 import BeautifulSoup

from src.matching.normalize import fold_text

EXTRACTOR_VERSION = "2.0.0"
_BLOCK_TAGS = ["p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "figcaption"]
_STRIP_TAGS = ["script", "style", "noscript", "svg", "iframe", "template"]


def _blocks_of(root) -> list[str]:
    """Ordered, de-duplicated block texts under `root`.

    An <li> that contains block children is skipped (its children are
    collected on their own), which avoids the nested double-count defect.
    """
    out, seen = [], set()
    for el in root.find_all(_BLOCK_TAGS):
        if el.name == "li" and el.find(_BLOCK_TAGS):
            continue
        text = fold_text(el.get_text(" ", strip=True))
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue  # carousel / repeated template block
        seen.add(key)
        out.append(text)
    if not out:
        text = fold_text(root.get_text(" ", strip=True))
        if text:
            out.append(text)
    return out


def _wc(s: str) -> int:
    return len(s.split())


class SectionExtractor:
    def __init__(self,
                 selectors_path: str | Path = "config/brand_selectors.json",
                 extracted_dir: str | Path = "data/extracted_sections"):
        self.cfg = json.loads(Path(selectors_path).read_text(encoding="utf-8"))
        self.extracted_dir = Path(extracted_dir)
        self.extracted_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ profiles
    def _profile(self, brand: str) -> dict:
        prof = dict(self.cfg.get("default", {}))
        prof.update(self.cfg.get("brands", {}).get(brand, {}))
        return prof

    # ------------------------------------------------------------- welcome
    def _acceptable(self, container, body_words: int, enforce_fraction: bool = True) -> bool:
        words = _wc(container.get_text(" ", strip=True))
        if words < self.cfg.get("welcome_min_words", 30):
            return False
        # The fraction cap guards the low-trust heading walk against grabbing
        # a page-wrapper div. It must NOT apply to the brand selector: that
        # selector IS the welcome container by definition, and sparse real
        # pages can legitimately be welcome-dominated.
        if enforce_fraction and body_words and \
                words > self.cfg.get("welcome_max_body_fraction", 0.6) * body_words:
            return False
        return True

    def _find_welcome(self, body_el, brand: str, body_words: int):
        """Returns (container, method, dom_path) or (None, 'NOT_FOUND', '')."""
        prof = self._profile(brand)

        for sel in prof.get("welcome_selectors", []):
            el = body_el.select_one(sel)
            if el is not None and self._acceptable(el, body_words, enforce_fraction=False):
                return el, "BRAND_SELECTOR", sel

        for pat in prof.get("welcome_heading_patterns", []):
            rx = re.compile(pat, re.I)
            for h in body_el.find_all(["h1", "h2", "h3", "h4"]):
                if not rx.search(fold_text(h.get_text(" ", strip=True)).lower()):
                    continue
                # climb: nearest ancestor that is big enough but not the page
                node = h.parent
                for _ in range(5):
                    if node is None or node.name in ("body", "html"):
                        break
                    if self._acceptable(node, body_words):
                        return node, "HEADING_WALK", f"{pat} -> ancestor <{node.name} class='{' '.join(node.get('class', []))}'>"
                    node = node.parent
                # sibling fallback: following blocks until the next heading.
                # Check acceptability BEFORE touching the tree — a rejected
                # candidate must leave the body intact for the other channels.
                sibs, sib = [], h
                while True:
                    sib = sib.find_next_sibling()
                    if sib is None or sib.name in ("h1", "h2"):
                        break
                    sibs.append(sib)
                if sibs:
                    combined = " ".join(fold_text(s.get_text(" ", strip=True)) for s in sibs)
                    words = _wc(combined)
                    min_w = self.cfg.get("welcome_min_words", 30)
                    frac = self.cfg.get("welcome_max_body_fraction", 0.6)
                    if words >= min_w and (not body_words or words <= frac * body_words):
                        wrapper = BeautifulSoup("<div></div>", "lxml").div
                        for s in sibs:
                            wrapper.append(s.extract())
                        return wrapper, "HEADING_WALK", f"{pat} -> following-siblings"
        return None, "NOT_FOUND", ""

    # -------------------------------------------------------------- extract
    def extract(self, school_id: str, html: str, brand: str = "",
                school_name: str = "", fetch_meta: dict | None = None) -> dict:
        soup = BeautifulSoup(html, "lxml")
        for tag in soup(_STRIP_TAGS):
            tag.decompose()

        # boilerplate channel: collected, then removed from the body
        boiler_blocks: list[str] = []
        for sel in self.cfg.get("boilerplate_selectors", []):
            try:
                found = soup.select(sel)
            except Exception:
                continue
            for el in found:
                boiler_blocks.extend(_blocks_of(el))
                el.decompose()
        boilerplate_text = " ".join(dict.fromkeys(boiler_blocks))

        body_el = soup.body or soup
        body_blocks = _blocks_of(body_el)
        body_text = " ".join(body_blocks)
        body_words = _wc(body_text)

        container, method, dom_path = self._find_welcome(body_el, brand, body_words)
        if container is not None:
            welcome_text = " ".join(_blocks_of(container))
            welcome_found = True
            # body_rest: remove the welcome container, re-collect
            if container.parent is not None:
                container.extract()
            body_rest_text = " ".join(_blocks_of(body_el))
        else:
            welcome_text = ""
            welcome_found = False
            body_rest_text = body_text

        out = {
            "school_id": str(school_id),
            "school_name": school_name,
            "brand": brand,
            "welcome_found": welcome_found,
            "extraction_method": method,
            "welcome_dom_path": dom_path,
            "welcome_text": welcome_text,
            "welcome_word_count": _wc(welcome_text),
            "body_text": body_text,
            "body_word_count": body_words,
            "body_rest_text": body_rest_text,
            "boilerplate_text": boilerplate_text,
            "extractor_version": EXTRACTOR_VERSION,
            "fetch_meta": {k: fetch_meta[k] for k in ("fetch_ts", "final_url", "http_status")
                           if fetch_meta and k in fetch_meta} if fetch_meta else {},
        }
        (self.extracted_dir / f"{school_id}.json").write_text(
            json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
        return out
