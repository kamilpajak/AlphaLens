"""Live SEC EDGAR probe — opt-in via SEC_LIVE_TEST=1. Pins incident #2
(#332->#338): EX-99.1 press releases live in the ``{accession}-index.htm``
Document Format Files "Type" column, NOT in ``FilingSummary.xml`` (which SEC
never lists EX-99.1 in). The hermetic regression fixture was hand-fabricated
with a fake FilingSummary EX-99.1 entry SEC never emits — so ONLY a real filing
fetch proves ``pick_ex_991_name`` resolves the exhibit the way prod needs.

DISCOVERY, NOT A PINNED ACCESSION (open question 6): the probe walks a
large-cap's REAL recent 8-K feed (Apple files earnings press-release 8-Ks every
quarter, each carrying an EX-99.1) and asserts at least one resolves. A pinned
constant would either age out or — worse — be fabricated, recreating the exact
#338 trap. Discovery is self-maintaining: nothing to refresh, and a true SEC
index.htm format change FAILs loudly (which is the point), while a transient
network error is tolerated.

    SEC_LIVE_TEST=1 .venv/bin/python -m unittest tests.live.test_sec_live -v
"""

from __future__ import annotations

import os
import unittest

from tests.live import PermanentProbeError, TransientProbeError, run_probes

_LIVE = os.environ.get("SEC_LIVE_TEST") == "1"

# Apple Inc — a mega-cap that will not delist and files quarterly earnings 8-Ks
# (each with an EX-99.1). CIK is permanent; no accession is pinned.
_CIK = "0000320193"
# How many recent 8-Ks to walk looking for an EX-99.1. Apple files several 8-Ks
# a year and ~4 carry EX-99.1, so within ~15 there is essentially always one;
# the bound caps fetches (SEC 10 req/s) without risking a between-earnings miss.
_MAX_8K_SCAN = 15
_EXHIBIT_SUFFIXES = (".htm", ".html", ".txt")


def _index_url(cik: str, accession: str) -> str:
    """Mirror edgar_press_release._base_dir_from_index_filename URL layout."""
    cik_no_zeros = str(int(cik))
    acc_no_dashes = accession.replace("-", "")
    return (
        f"https://www.sec.gov/Archives/edgar/data/{cik_no_zeros}/"
        f"{acc_no_dashes}/{accession}-index.htm"
    )


@unittest.skipUnless(_LIVE, "set SEC_LIVE_TEST=1 to run the live SEC probe")
class TestSecEx991Live(unittest.TestCase):
    def test_ex991_resolves_from_a_real_recent_8k_index(self):
        from alphalens_pipeline.data.alt_data.sec_edgar_client import get_default_sec_client
        from alphalens_pipeline.thematic.sources.edgar_press_release import pick_ex_991_name

        def _probe() -> None:
            client = get_default_sec_client()
            try:
                submissions = client.fetch_submissions(_CIK)
            except Exception as exc:  # network error reaching submissions -> transient
                raise TransientProbeError(f"submissions fetch failed: {exc}") from exc

            recent = submissions.get("filings", {}).get("recent", {})
            forms = recent.get("form", [])
            accessions = recent.get("accessionNumber", [])
            if not forms or not accessions:
                raise PermanentProbeError(
                    "Apple submissions feed had no recent filings — SEC submissions "
                    "JSON shape changed (expected filings.recent.form/accessionNumber)."
                )

            scanned = 0
            transient_hits = 0
            for form, accession in zip(forms, accessions, strict=False):
                if form != "8-K":
                    continue
                if scanned >= _MAX_8K_SCAN:
                    break
                scanned += 1
                try:
                    index_html = client.get_text(_index_url(_CIK, accession))
                except Exception:  # one bad index.htm fetch != a shape break
                    transient_hits += 1
                    continue
                if not index_html:
                    transient_hits += 1
                    continue
                name = pick_ex_991_name(index_html)
                if name:
                    # SHAPE-ONLY: the exhibit resolved to a real document
                    # basename from the index.htm Type column — never assert
                    # what the press release SAYS.
                    if not name.lower().endswith(_EXHIBIT_SUFFIXES):
                        raise PermanentProbeError(
                            f"EX-99.1 resolved to an unexpected basename: {name!r}"
                        )
                    return  # success: a real EX-99.1 resolved from real bytes

            if scanned == 0:
                raise PermanentProbeError(
                    "no 8-K in Apple's recent filings feed — submissions shape changed."
                )
            if transient_hits >= scanned:
                raise TransientProbeError(
                    f"all {scanned} index.htm fetches failed (network/rate-limit)."
                )
            raise PermanentProbeError(
                f"scanned {scanned} recent Apple 8-Ks and NONE resolved an EX-99.1 — "
                "either the resolver regressed to FilingSummary.xml (#332->#338) or "
                "SEC changed the index.htm Document Format Files table format."
            )

        run_probes(self, {"AAPL/EX-99.1-discovery": _probe}, label="sec")


if __name__ == "__main__":
    unittest.main()
