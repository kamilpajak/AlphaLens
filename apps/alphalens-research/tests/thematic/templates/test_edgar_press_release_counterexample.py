"""Realistic edgar_press_release counterexample against the SHIPPED templates.

The hermetic engine tests feed hand-authored two-ticker businesswire
prose — a shape the production edgar_press_release adapter never
produces. Real rows carry the title ``EX-99.1``, exactly ONE feed ticker
(from the filer CIK), and a raw-HTML EX-99.1 exhibit body. Fed through
the shipped YAML library + the production ``EntityResolver``, such a row
passes every m_and_a_press_release text predicate and then fails ONLY on
the second required entity role — the exact silence #321 diagnosed and
issue #1108 instruments. This module pins that whole path end to end:
no match, the distinct ``required_role_unfilled`` holdout reason, and an
explicit zero match series in the flushed metrics render.

Fixture provenance (capture): real SEC 8-K EX-99.1 exhibit, accession
0001709442-26-000033, filer FirstSun Capital Bancorp (FSUN), filed
2026-06-05 ("Sunflower Bank Closes Sale of ~$890 Million ... to
Brookfield"). Body trimmed from 11KB to the document header, headline
div, and the first two paragraphs — preserving the tokens the shipped
m_and_a_press_release predicates key on ("$890 million" for
amount_mentioned, "acquisition" for any_sentence_contains) and the
raw-Workiva-HTML shape. Do not "clean up" the HTML: the unrealism of the
older hand-authored fixtures is what let the shortfall go unmeasured.
"""

from __future__ import annotations

import datetime as dt
import os
import tempfile
import unittest
from pathlib import Path

from alphalens_pipeline.thematic.extraction.event_extractor import DEFAULT_TEMPLATES_DIR
from alphalens_pipeline.thematic.extraction.templates.engine import TemplateEngine
from alphalens_pipeline.thematic.extraction.templates.entity_resolver import EntityResolver
from alphalens_pipeline.thematic.extraction.templates.holdout import (
    HOLDOUT_ALL_PREDICATES_FAILED,
    HOLDOUT_REQUIRED_ROLE_UNFILLED,
)
from alphalens_pipeline.thematic.extraction.templates.spec import Article, ResolvedEntity

# Trimmed real EX-99.1 exhibit body (see module docstring for provenance).
_FSUN_EX99_BODY = """<DOCUMENT>
<TYPE>EX-99.1
<SEQUENCE>2
<FILENAME>exhibit991-sunflowerbankcl.htm
<DESCRIPTION>EX-99.1
<TEXT>
<html><head>
<!-- Document created using Wdesk -->
<!-- Copyright 2026 Workiva -->
<title>Document</title></head><body><div id="i96fa6f14f0a6424e989943d4522c1c5b_1"></div><div style="text-align:center"><font style="color:#000000;font-family:'Arial',sans-serif;font-size:18pt;font-weight:700;line-height:112%">Sunflower Bank Closes Sale of Approximately $890 Million of Multifamily Commercial Real Estate Loans to Brookfield  </font></div><div><font><br></font></div><div><font style="color:#000000;font-family:'Arial',sans-serif;font-size:10pt;font-weight:400;line-height:120%">DENVER &#38; NEW YORK, June 5, 2026--(BUSINESS WIRE) FirstSun Capital Bancorp (&#34;FirstSun&#34;) (NASDAQ&#58; FSUN), the holding company for Sunflower Bank, National Association (the &#8220;Bank&#8221;) announced today that the Bank has closed on the sale of  performing multifamily commercial real estate mortgage loans acquired from First Foundation Bank to entities affiliated with Brookfield Asset Management (&#8220;Brookfield&#8221;) (NYSE&#58; BAM, TSX&#58; BAM), a global alternative asset manager. The loans sold had contractual balances totaling approximately $890 million.  </font></div><div><font style="color:#000000;font-family:'Arial',sans-serif;font-size:10pt;font-weight:400;line-height:120%">The multifamily loan sale was contemplated and announced as part of FirstSun&#8217;s acquisition of First Foundation, Inc., which closed on April 1, 2026.</font></div></body></html>
</TEXT>
</DOCUMENT>"""


def _fsun_article() -> Article:
    """Exactly the shape edgar_press_release.py:535-546 produces."""
    return Article(
        id="0001709442-26-000033",
        source="edgar_press_release",
        title="EX-99.1",
        body=_FSUN_EX99_BODY,
        url=(
            "https://www.sec.gov/Archives/edgar/data/1709442/"
            "000170944226000033/0001709442-26-000033-index.htm"
        ),
        published_at=dt.datetime(2026, 6, 5, tzinfo=dt.UTC),
        tickers_raw=["FSUN"],
    )


class TestSingleTickerEdgarPressRelease(unittest.TestCase):
    """The counterexample that would have caught the 12-week silence."""

    def setUp(self):
        # Shipped YAML library (incl. the any_sentence_contains predicate
        # the in-code test specs omit) + the production resolver wiring.
        self.engine = TemplateEngine.from_dir(DEFAULT_TEMPLATES_DIR)
        self.resolver = EntityResolver()

    def test_required_role_unfilled_not_predicate_drift(self):
        article = _fsun_article()
        entities = self.resolver.resolve(article)
        # The production resolver maps feed tickers positionally — one
        # filer-CIK ticker in, one entity out. A second entity (the deal
        # counterparty) can never appear on this source.
        self.assertEqual([e.ticker for e in entities], ["FSUN"])

        event = self.engine.match(article, entities)

        self.assertIsNone(event)
        snap = self.engine.metrics.snapshot()
        # Every m_and_a text predicate passed; the drop is a resolver
        # shortfall, not pattern drift — it must surface under the
        # distinct reason, not collapse into all_predicates_failed.
        self.assertEqual(snap["holdout"][HOLDOUT_REQUIRED_ROLE_UNFILLED], 1)
        self.assertEqual(snap["holdout"][HOLDOUT_ALL_PREDICATES_FAILED], 0)

    def test_flush_renders_explicit_zero_match_series(self):
        tmpdir = Path(tempfile.mkdtemp())
        orig_env = os.environ.get("ALPHALENS_TEXTFILE_DIR")
        os.environ["ALPHALENS_TEXTFILE_DIR"] = str(tmpdir)
        try:
            article = _fsun_article()
            self.engine.match(article, self.resolver.resolve(article))
            self.engine.metrics.flush(job="template-engine-counterexample-test")
            out = tmpdir / "alphalens_domain_template-engine-counterexample-test.prom"
            text = out.read_text()
        finally:
            if orig_env is None:
                os.environ.pop("ALPHALENS_TEXTFILE_DIR", None)
            else:
                os.environ["ALPHALENS_TEXTFILE_DIR"] = orig_env
        # The attempted-but-never-matched template renders an explicit 0
        # match sample — the series the match-rate alert divides on.
        self.assertIn(
            'alphalens_template_attempt_total{template_id="m_and_a_press_release"} 1',
            text,
        )
        self.assertIn(
            'alphalens_template_match_total{template_id="m_and_a_press_release"} 0',
            text,
        )

    def test_positive_control_second_entity_flips_to_match(self):
        # Proves the counterexample discriminates the ROLE shortfall, not
        # some other predicate: append the deal counterparty the resolver
        # cannot see (BAM, named in the exhibit) and the shipped m_and_a
        # template matches. Mirrors the #321 replay proof.
        article = _fsun_article()
        entities = [
            *self.resolver.resolve(article),
            ResolvedEntity(ticker="BAM", name="Brookfield Asset Management", role="company"),
        ]
        event = self.engine.match(article, entities)
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.template_id, "m_and_a_press_release")


if __name__ == "__main__":
    unittest.main()
