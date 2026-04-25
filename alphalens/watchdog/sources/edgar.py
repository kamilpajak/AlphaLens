from __future__ import annotations

import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime

import requests

from ..config import WATCHDOG_DEFAULTS
from ..storage import SeenEventStore
from ..types import Event, FormType
from .base import EventSource
from .cik_loader import CIKLoader
from .eightk import extract_5_02_section, extract_8k_items, infer_5_02_subsection
from .form4 import parse_form4_xml

logger = logging.getLogger(__name__)

ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}
ACCESSION_URN_PREFIX = "urn:tag:sec.gov,2008:accession-number="
ITEM_PATTERN = re.compile(r"Item\s+(\d+\.\d+)", re.IGNORECASE)


class SECEdgarSource(EventSource):
    def __init__(
        self,
        tickers: list[str],
        config: dict | None = None,
        store: SeenEventStore | None = None,
        ticker_to_cik: dict[str, str] | None = None,
        cik_loader: CIKLoader | None = None,
    ):
        self.config = dict(WATCHDOG_DEFAULTS)
        if config:
            self.config.update(config)

        if not self.config.get("user_agent"):
            raise ValueError(
                "SEC mandates a real contact email in User-Agent. "
                "Set config['user_agent'] to 'YourName contact@example.com'."
            )

        self.tickers = tickers
        self.store = store if store is not None else SeenEventStore()
        self.ticker_to_cik = ticker_to_cik or {}
        self.cik_loader = cik_loader
        self.form_filter: set[FormType] = set(self.config["form_filter"])
        self.rate_limit_seconds = float(self.config["rate_limit_seconds"])
        self.fetch_form4_details = bool(self.config.get("fetch_form4_details", False))
        self.fetch_8k_details = bool(self.config.get("fetch_8k_details", False))

    def _collect_events_for_ticker(self, idx: int, ticker: str) -> list[Event]:
        """Fetch + parse one ticker's Atom feed; logs and skips on missing CIK or fetch error."""
        cik = self._resolve_cik(ticker)
        if not cik:
            logger.warning("No CIK mapping for %s, skipping", ticker)
            return []
        if idx > 0:
            time.sleep(self.rate_limit_seconds)
        xml_text = self._fetch_feed(cik)
        if xml_text is None:
            return []
        return self._parse_atom(xml_text, ticker)

    def _apply_enrichments(self, unseen: list[Event]) -> None:
        if self.fetch_form4_details:
            for event in unseen:
                if event.form_type == FormType.FORM_4:
                    self._enrich_form4(event)
        if self.fetch_8k_details:
            for event in unseen:
                if event.form_type == FormType.FORM_8K:
                    self._enrich_8k(event)

    def detect(self) -> list[Event]:
        all_events: list[Event] = []
        for idx, ticker in enumerate(self.tickers):
            all_events.extend(self._collect_events_for_ticker(idx, ticker))

        filtered = [e for e in all_events if e.form_type in self.form_filter]
        unseen = self.store.filter_unseen(filtered)
        self._apply_enrichments(unseen)
        for event in unseen:
            self.store.mark_seen(event.accession_number)
        return unseen

    def _resolve_cik(self, ticker: str) -> str | None:
        if self.cik_loader is not None:
            cik = self.cik_loader.get_cik(ticker)
            if cik:
                return cik
        return self.ticker_to_cik.get(ticker)

    def _fetch_feed(self, cik: str) -> str | None:
        params = {
            "action": "getcompany",
            "CIK": cik,
            "type": "",
            "dateb": "",
            "owner": "include",
            "count": str(self.config["edgar_recent_count"]),
            "output": "atom",
        }
        return self._get(self.config["edgar_base_url"], params=params, context=f"feed CIK={cik}")

    def _enrich_form4(self, event: Event) -> None:
        """Find and parse the Form 4 XBRL via SEC's index.json (canonical file listing)."""
        base_dir = event.url.rsplit("/", 1)[0] if "/" in event.url else ""
        if not base_dir:
            return

        index_text = self._get(
            f"{base_dir}/index.json", context=f"form4 index {event.accession_number}"
        )
        if index_text is None:
            return

        xml_name = _pick_form4_xml_name(index_text)
        if not xml_name:
            return

        xml_text = self._get(
            f"{base_dir}/{xml_name}", context=f"form4 xml {event.accession_number}"
        )
        if xml_text is None:
            return

        parsed = parse_form4_xml(xml_text)
        if parsed:
            event.raw_data.update(parsed)

    def _enrich_8k(self, event: Event) -> None:
        """Fetch primary 8-K HTML via FilingSummary.xml doctype metadata and extract items."""
        base_dir = event.url.rsplit("/", 1)[0] if "/" in event.url else ""
        if not base_dir:
            return

        summary_text = self._get(
            f"{base_dir}/FilingSummary.xml",
            context=f"8-k summary {event.accession_number}",
        )
        if summary_text is None:
            return

        primary = _pick_8k_primary_name(summary_text)
        if not primary:
            return

        html_text = self._get(f"{base_dir}/{primary}", context=f"8-k html {event.accession_number}")
        if html_text is None:
            return

        items = extract_8k_items(html_text)
        if items:
            event.raw_data["items"] = _resolve_5_02_subsection(items, html_text)

    def _get(self, url: str, params: dict | None = None, context: str = "") -> str | None:
        headers = {"User-Agent": self.config["user_agent"]}
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=10)
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as exc:
            logger.error("EDGAR fetch failed (%s): %s", context, exc)
            return None

    def _parse_atom(self, xml_text: str, ticker: str) -> list[Event]:
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            logger.error("Malformed Atom feed for %s: %s", ticker, exc)
            return []

        events: list[Event] = []
        for entry in root.findall("atom:entry", ATOM_NS):
            event = self._parse_entry(entry, ticker)
            if event is not None:
                events.append(event)
        return events

    def _parse_entry(self, entry: ET.Element, ticker: str) -> Event | None:
        category = entry.find("atom:category", ATOM_NS)
        if category is None:
            return None
        form_str = category.get("term", "")
        form_type = FormType.from_sec_string(form_str)
        if form_type is None:
            return None

        accession = self._extract_accession(entry)
        if not accession:
            return None

        link = entry.find("atom:link", ATOM_NS)
        url = link.get("href", "") if link is not None else ""

        updated = entry.find("atom:updated", ATOM_NS)
        filed_at = (
            _parse_iso_datetime(updated.text) if updated is not None and updated.text else None
        )
        if filed_at is None:
            return None

        title_el = entry.find("atom:title", ATOM_NS)
        title_text = title_el.text if title_el is not None and title_el.text else ""

        raw = {"title": title_text, "form_str": form_str}
        if form_type == FormType.FORM_8K:
            items = ITEM_PATTERN.findall(title_text)
            if items:
                raw["items"] = items

        return Event(
            ticker=ticker,
            form_type=form_type,
            accession_number=accession,
            filed_at=filed_at,
            url=url,
            raw_data=raw,
        )

    @staticmethod
    def _extract_accession(entry: ET.Element) -> str | None:
        id_el = entry.find("atom:id", ATOM_NS)
        if id_el is not None and id_el.text and ACCESSION_URN_PREFIX in id_el.text:
            return id_el.text.split(ACCESSION_URN_PREFIX, 1)[1].strip()
        return None


def _parse_iso_datetime(text: str) -> datetime | None:
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _pick_8k_primary_name(filing_summary_xml: str) -> str | None:
    """From SEC FilingSummary.xml, pick the primary 8-K document filename.

    FilingSummary.xml is authoritative — its <File doctype="..."> attribute
    declares the form type explicitly, whereas index.json's `type` field is
    only a MIME icon hint ("text.gif") that can't distinguish primary 8-K
    from EX-99 exhibits or XBRL wrappers.
    """
    try:
        root = ET.fromstring(filing_summary_xml)
    except ET.ParseError:
        return None

    for file_el in root.iter("File"):
        doctype = (file_el.get("doctype") or "").upper()
        if doctype in {"8-K", "8-K/A"}:
            original = file_el.get("original")
            if original:
                return original
            text = (file_el.text or "").strip()
            return text or None
    return None


def _resolve_5_02_subsection(items: list[str], html_text: str) -> list[str]:
    """Upgrade bare ``5.02`` to its subsection (5.02(a)/(b)/...) when the section
    narrative gives a clear signal. Per Perplexity 2026-04-18 ~60-70% of real
    principal-officer events use bare ``Item 5.02`` headings.
    """
    if "5.02" not in items or any(i.startswith("5.02(") for i in items):
        return items
    inferred = infer_5_02_subsection(extract_5_02_section(html_text))
    if not inferred:
        return items
    return [inferred if i == "5.02" else i for i in items]


def _pick_form4_xml_name(index_json_text: str) -> str | None:
    """From SEC filing index.json, pick the Form 4 ownership XML file.

    Real Form 4 filings store XBRL under varied names (primary_doc.xml,
    wk-form4_*.xml, etc.). We pick the first .xml that isn't a meta file.
    """
    try:
        data = json.loads(index_json_text)
    except ValueError:
        return None

    items = data.get("directory", {}).get("item", [])
    ignore = {"FilingSummary.xml", "MetaLinks.json"}
    for item in items:
        name = item.get("name", "")
        if name.endswith(".xml") and name not in ignore and "index" not in name.lower():
            return name
    return None
