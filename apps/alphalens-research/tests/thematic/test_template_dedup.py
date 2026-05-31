"""PR-4 of epic #321 — multi-source dedup via template tuples.

When the same M&A is reported by 10 outlets within a 24h window AND each
extraction landed on the same template (`m_and_a_press_release`) with the
same resolved entity set (`{NVDA, XYZ}`), the catalyst resolver and brief
generator should treat that as ONE event, not 10.

Today (post PR-2 / PR-3) the only collapse-at-injection guard is the
sort-tiebreaker in ``orchestrator._sort_and_dedup_for_brief`` — that one
fires at the brief layer per ``(ticker, …)`` and is downstream of
``catalyst_resolver``. Multi-source echo dedup belongs upstream so the
resolver sees the collapsed view too (echo_count, supersession-window
arithmetic, theme-arc traversal all benefit).

Contract:
  - Rows with ``template_id`` null (Flash path) PASS THROUGH unchanged
    with ``dedup_count=1`` and the new aggregate columns null. PR-4
    scope is strictly template-extracted echoes; Flash dedup is the
    existing PR #141/#142 Jaccard pre-stage's responsibility.
  - Rows with the same ``(template_id, frozenset(primary_entities))``
    whose timestamps fall within ``window`` of each other (sliding
    anchor — see ``dedup_template_events`` docstring) collapse to ONE
    representative row.
  - The representative is the row with the RICHEST ``template_fields_json``
    (most non-null keys). Tie-break by earliest timestamp (the first
    outlet to publish wins — matches the catalyst-resolver's "earliest in
    story arc" convention).
  - The representative carries:
      * ``dedup_count: int`` — group size (1 for singletons)
      * ``dedup_source_urls_json: str | None`` — JSON list of source URLs,
        ordered by timestamp ascending; ``None`` when count == 1
      * ``dedup_news_ids_json: str | None`` — JSON list of news_id values
        for the audit trail; ``None`` when count == 1
  - Idempotent: re-applying to an already-deduplicated frame is a no-op.
  - Order-insensitive on ``primary_entities`` — ``["NVDA", "XYZ"]`` and
    ``["XYZ", "NVDA"]`` cluster together.

Wired in catalyst_resolver between the news-join and template-precedence
stages so ``_apply_template_precedence`` sees the collapsed view (one
template row per cluster, not ten).
"""

from __future__ import annotations

import json
import unittest

import pandas as pd
from alphalens_pipeline.thematic import dedup


def _row(
    news_id: str,
    *,
    timestamp: str,
    template_id: str | None,
    primary_entities: list[str],
    fields: dict | None,
    url: str,
    event_type: str = "m_and_a",
) -> dict:
    """Build a post-join row matching catalyst_resolver's frame shape."""
    return {
        "news_id": news_id,
        "id": news_id,  # news.id == events.news_id post-merge
        "event_type": event_type,
        "primary_entities": primary_entities,
        "template_id": template_id,
        "template_fields_json": (
            json.dumps(fields, sort_keys=True) if fields is not None else None
        ),
        "extraction_method": "template" if template_id is not None else "flash",
        "url": url,
        "source": "businesswire" if "businesswire" in url else "reuters",
        "published_at": pd.Timestamp(timestamp),
    }


def _frame(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


class TestDedupCollapsesEchoes(unittest.TestCase):
    """Same (template_id, entity_set, 24h window) → one row, count=N."""

    def test_ten_outlets_same_m_and_a_collapses_to_one(self):
        # Ten outlets, same template, same entities, 1-hour spread.
        fields = {
            "acquirer_ticker": "NVDA",
            "target_ticker": "XYZ",
            "consideration_usd": 5_000_000_000,
            "announcement_date": "2026-05-31",
        }
        rows = [
            _row(
                f"src{i}",
                timestamp=f"2026-05-31T08:0{i}:00Z",
                template_id="m_and_a_press_release",
                primary_entities=["NVDA", "XYZ"],
                fields=fields,
                url=f"https://outlet{i}.example.com/m-and-a",
            )
            for i in range(10)
        ]
        out = dedup.dedup_template_events(_frame(rows), time_col="published_at")

        self.assertEqual(len(out), 1)
        survivor = out.iloc[0]
        self.assertEqual(survivor["dedup_count"], 10)
        urls = json.loads(survivor["dedup_source_urls_json"])
        self.assertEqual(len(urls), 10)
        # Stable order: ascending timestamp (src0 → src9).
        self.assertEqual(urls[0], "https://outlet0.example.com/m-and-a")
        self.assertEqual(urls[-1], "https://outlet9.example.com/m-and-a")
        ids = json.loads(survivor["dedup_news_ids_json"])
        self.assertEqual(ids[0], "src0")
        self.assertEqual(ids[-1], "src9")

    def test_entity_set_order_does_not_matter(self):
        # ["NVDA", "XYZ"] vs ["XYZ", "NVDA"] must cluster — the dedup key
        # is a set, not a list.
        fields = {"acquirer_ticker": "NVDA", "target_ticker": "XYZ"}
        rows = [
            _row(
                "a",
                timestamp="2026-05-31T08:00:00Z",
                template_id="m_and_a_press_release",
                primary_entities=["NVDA", "XYZ"],
                fields=fields,
                url="https://bw.example.com/a",
            ),
            _row(
                "b",
                timestamp="2026-05-31T08:30:00Z",
                template_id="m_and_a_press_release",
                primary_entities=["XYZ", "NVDA"],
                fields=fields,
                url="https://reuters.example.com/b",
            ),
        ]
        out = dedup.dedup_template_events(_frame(rows), time_col="published_at")
        self.assertEqual(len(out), 1)
        self.assertEqual(out.iloc[0]["dedup_count"], 2)


class TestDedupRespectsBoundaries(unittest.TestCase):
    """Different template_id / entity set / window → no collapse."""

    def test_different_template_id_does_not_collapse(self):
        rows = [
            _row(
                "a",
                timestamp="2026-05-31T08:00:00Z",
                template_id="m_and_a_press_release",
                primary_entities=["NVDA"],
                fields={"x": 1},
                url="https://bw.example.com/a",
            ),
            _row(
                "b",
                timestamp="2026-05-31T08:05:00Z",
                template_id="earnings_surprise",
                primary_entities=["NVDA"],
                fields={"x": 2},
                url="https://reuters.example.com/b",
                event_type="earnings",
            ),
        ]
        out = dedup.dedup_template_events(_frame(rows), time_col="published_at")
        self.assertEqual(len(out), 2)
        # Both pass through as singletons.
        for _, row in out.iterrows():
            self.assertEqual(row["dedup_count"], 1)
            self.assertIsNone(row["dedup_source_urls_json"])

    def test_different_entity_set_does_not_collapse(self):
        rows = [
            _row(
                "a",
                timestamp="2026-05-31T08:00:00Z",
                template_id="m_and_a_press_release",
                primary_entities=["NVDA", "XYZ"],
                fields={"x": 1},
                url="https://bw.example.com/a",
            ),
            # Same template, same acquirer, different target.
            _row(
                "b",
                timestamp="2026-05-31T08:05:00Z",
                template_id="m_and_a_press_release",
                primary_entities=["NVDA", "ABC"],
                fields={"x": 2},
                url="https://reuters.example.com/b",
            ),
        ]
        out = dedup.dedup_template_events(_frame(rows), time_col="published_at")
        self.assertEqual(len(out), 2)

    def test_outside_24h_window_does_not_collapse(self):
        rows = [
            _row(
                "a",
                timestamp="2026-05-30T08:00:00Z",
                template_id="m_and_a_press_release",
                primary_entities=["NVDA", "XYZ"],
                fields={"x": 1},
                url="https://bw.example.com/a",
            ),
            # 30 hours later — outside the default 24h sliding window.
            _row(
                "b",
                timestamp="2026-05-31T14:00:00Z",
                template_id="m_and_a_press_release",
                primary_entities=["NVDA", "XYZ"],
                fields={"x": 1},
                url="https://reuters.example.com/b",
            ),
        ]
        out = dedup.dedup_template_events(_frame(rows), time_col="published_at")
        self.assertEqual(len(out), 2)


class TestSurvivorSelection(unittest.TestCase):
    """The richest-fields row wins; ties break on earliest timestamp."""

    def test_survivor_has_most_non_null_fields(self):
        # Two outlets report same M&A; one parses out all 4 fields, the
        # other only extracts 2 (e.g. amount missing in headline). The
        # richer extraction must survive.
        sparse_fields = {"acquirer_ticker": "NVDA", "target_ticker": "XYZ"}
        rich_fields = {
            "acquirer_ticker": "NVDA",
            "target_ticker": "XYZ",
            "consideration_usd": 5_000_000_000,
            "announcement_date": "2026-05-31",
        }
        rows = [
            _row(
                "sparse",
                timestamp="2026-05-31T08:00:00Z",
                template_id="m_and_a_press_release",
                primary_entities=["NVDA", "XYZ"],
                fields=sparse_fields,
                url="https://bw.example.com/sparse",
            ),
            _row(
                "rich",
                timestamp="2026-05-31T08:30:00Z",
                template_id="m_and_a_press_release",
                primary_entities=["NVDA", "XYZ"],
                fields=rich_fields,
                url="https://reuters.example.com/rich",
            ),
        ]
        out = dedup.dedup_template_events(_frame(rows), time_col="published_at")
        self.assertEqual(len(out), 1)
        survivor = out.iloc[0]
        self.assertEqual(survivor["news_id"], "rich")
        # Survivor's template_fields_json carries all 4 keys.
        self.assertEqual(json.loads(survivor["template_fields_json"]), rich_fields)

    def test_tie_break_on_earliest_timestamp(self):
        # Same field count → earliest outlet wins. Matches the catalyst-
        # resolver's "earliest event in story arc = catalyst" convention.
        fields = {"acquirer_ticker": "NVDA", "target_ticker": "XYZ"}
        rows = [
            _row(
                "second",
                timestamp="2026-05-31T08:30:00Z",
                template_id="m_and_a_press_release",
                primary_entities=["NVDA", "XYZ"],
                fields=fields,
                url="https://reuters.example.com/second",
            ),
            _row(
                "first",
                timestamp="2026-05-31T08:00:00Z",
                template_id="m_and_a_press_release",
                primary_entities=["NVDA", "XYZ"],
                fields=fields,
                url="https://bw.example.com/first",
            ),
        ]
        out = dedup.dedup_template_events(_frame(rows), time_col="published_at")
        self.assertEqual(len(out), 1)
        self.assertEqual(out.iloc[0]["news_id"], "first")


class TestPassThroughCases(unittest.TestCase):
    """Flash rows + singletons + edge inputs pass through cleanly."""

    def test_flash_rows_pass_through_with_dedup_count_one(self):
        # Two Flash extractions of unrelated news. Must not collapse, and
        # must carry dedup_count=1 + null aggregate columns.
        rows = [
            _row(
                "a",
                timestamp="2026-05-31T08:00:00Z",
                template_id=None,
                primary_entities=["NVDA"],
                fields=None,
                url="https://polygon.io/a",
            ),
            _row(
                "b",
                timestamp="2026-05-31T08:05:00Z",
                template_id=None,
                primary_entities=["NVDA"],
                fields=None,
                url="https://polygon.io/b",
            ),
        ]
        out = dedup.dedup_template_events(_frame(rows), time_col="published_at")
        self.assertEqual(len(out), 2)
        for _, row in out.iterrows():
            self.assertEqual(row["dedup_count"], 1)
            self.assertIsNone(row["dedup_source_urls_json"])
            self.assertIsNone(row["dedup_news_ids_json"])

    def test_singleton_template_row_emits_count_one(self):
        rows = [
            _row(
                "solo",
                timestamp="2026-05-31T08:00:00Z",
                template_id="guidance_update",
                primary_entities=["AAPL"],
                fields={"direction": "raise"},
                url="https://bw.example.com/solo",
                event_type="guidance",
            ),
        ]
        out = dedup.dedup_template_events(_frame(rows), time_col="published_at")
        self.assertEqual(len(out), 1)
        survivor = out.iloc[0]
        self.assertEqual(survivor["dedup_count"], 1)
        self.assertIsNone(survivor["dedup_source_urls_json"])
        self.assertIsNone(survivor["dedup_news_ids_json"])

    def test_empty_frame_returns_empty_frame_with_columns(self):
        # Empty input must still return a frame carrying the three new
        # audit columns so downstream column-access is safe.
        empty = pd.DataFrame(
            columns=[
                "news_id",
                "id",
                "event_type",
                "primary_entities",
                "template_id",
                "template_fields_json",
                "extraction_method",
                "url",
                "source",
                "published_at",
            ]
        )
        out = dedup.dedup_template_events(empty, time_col="published_at")
        self.assertEqual(len(out), 0)
        for col in ("dedup_count", "dedup_source_urls_json", "dedup_news_ids_json"):
            self.assertIn(col, out.columns)


class TestIdempotenceAndShape(unittest.TestCase):
    """Re-applying dedup is a no-op; schema is preserved."""

    def test_dedup_is_idempotent(self):
        fields = {"acquirer_ticker": "NVDA", "target_ticker": "XYZ"}
        rows = [
            _row(
                "a",
                timestamp="2026-05-31T08:00:00Z",
                template_id="m_and_a_press_release",
                primary_entities=["NVDA", "XYZ"],
                fields=fields,
                url="https://bw.example.com/a",
            ),
            _row(
                "b",
                timestamp="2026-05-31T08:05:00Z",
                template_id="m_and_a_press_release",
                primary_entities=["NVDA", "XYZ"],
                fields=fields,
                url="https://reuters.example.com/b",
            ),
        ]
        once = dedup.dedup_template_events(_frame(rows), time_col="published_at")
        twice = dedup.dedup_template_events(once, time_col="published_at")
        self.assertEqual(len(once), 1)
        self.assertEqual(len(twice), 1)
        self.assertEqual(once.iloc[0]["dedup_count"], 2)
        # Idempotent: a second pass MUST NOT re-multiply the count or
        # collapse the already-singleton survivor with itself.
        self.assertEqual(twice.iloc[0]["dedup_count"], 2)
        # URLs survive verbatim through the second pass.
        self.assertEqual(
            once.iloc[0]["dedup_source_urls_json"],
            twice.iloc[0]["dedup_source_urls_json"],
        )

    def test_legacy_frame_without_template_id_column_is_safe(self):
        # Defensive: catalyst_resolver may pass through a pre-PR-2 frame
        # (no template_id column at all). Dedup must treat every row as
        # flash and pass through with dedup_count=1.
        legacy_rows = [
            {
                "news_id": "a",
                "id": "a",
                "event_type": "m_and_a",
                "primary_entities": ["NVDA"],
                "url": "https://polygon.io/a",
                "source": "polygon",
                "published_at": pd.Timestamp("2026-05-31T08:00:00Z"),
            },
            {
                "news_id": "b",
                "id": "b",
                "event_type": "m_and_a",
                "primary_entities": ["NVDA"],
                "url": "https://polygon.io/b",
                "source": "polygon",
                "published_at": pd.Timestamp("2026-05-31T08:05:00Z"),
            },
        ]
        out = dedup.dedup_template_events(_frame(legacy_rows), time_col="published_at")
        self.assertEqual(len(out), 2)
        for _, row in out.iterrows():
            self.assertEqual(row["dedup_count"], 1)


class TestMixedBatch(unittest.TestCase):
    """Realistic mixed frame: templates AND flash, collapses + singletons."""

    def test_mixed_template_and_flash_batch(self):
        fields = {"acquirer_ticker": "NVDA", "target_ticker": "XYZ"}
        rows = [
            # Cluster of 3 echoes for the same M&A.
            _row(
                "echo1",
                timestamp="2026-05-31T08:00:00Z",
                template_id="m_and_a_press_release",
                primary_entities=["NVDA", "XYZ"],
                fields=fields,
                url="https://bw.example.com/1",
            ),
            _row(
                "echo2",
                timestamp="2026-05-31T08:10:00Z",
                template_id="m_and_a_press_release",
                primary_entities=["NVDA", "XYZ"],
                fields=fields,
                url="https://reuters.example.com/2",
            ),
            _row(
                "echo3",
                timestamp="2026-05-31T08:20:00Z",
                template_id="m_and_a_press_release",
                primary_entities=["NVDA", "XYZ"],
                fields=fields,
                url="https://bloomberg.example.com/3",
            ),
            # Unrelated template singleton.
            _row(
                "solo_t",
                timestamp="2026-05-31T09:00:00Z",
                template_id="earnings_surprise",
                primary_entities=["AAPL"],
                fields={"direction": "beat"},
                url="https://bw.example.com/solo_t",
                event_type="earnings",
            ),
            # Two unrelated Flash rows — must NOT collapse with each other
            # or with the template rows.
            _row(
                "flash_a",
                timestamp="2026-05-31T10:00:00Z",
                template_id=None,
                primary_entities=["TSLA"],
                fields=None,
                url="https://polygon.io/flash_a",
            ),
            _row(
                "flash_b",
                timestamp="2026-05-31T10:05:00Z",
                template_id=None,
                primary_entities=["MSFT"],
                fields=None,
                url="https://polygon.io/flash_b",
            ),
        ]
        out = dedup.dedup_template_events(_frame(rows), time_col="published_at")
        # 1 collapsed M&A + 1 earnings singleton + 2 flash = 4 rows.
        self.assertEqual(len(out), 4)
        # The 3-echo cluster collapsed correctly.
        m_and_a_rows = out[out["template_id"] == "m_and_a_press_release"]
        self.assertEqual(len(m_and_a_rows), 1)
        self.assertEqual(m_and_a_rows.iloc[0]["dedup_count"], 3)


class TestZenReviewHardening(unittest.TestCase):
    """Defensive cases surfaced by the PR #325 zen pre-merge review.

    Track these explicitly so a future schema change or upstream-shape
    drift produces a sharp test failure rather than a silent miscluster.
    """

    def test_nan_in_primary_entities_does_not_stringify_to_nan_token(self):
        # A stray ``float('nan')`` inside primary_entities must NOT
        # become the cluster-key string "NAN" — that would either
        # collide with a legitimate ticker NAN or build a misleading
        # composite key. Both rows below share entity_set={NVDA, XYZ}
        # post-NaN-guard, so they collapse cleanly.
        import math

        fields = {"acquirer_ticker": "NVDA", "target_ticker": "XYZ"}
        rows = [
            _row(
                "a",
                timestamp="2026-05-31T08:00:00Z",
                template_id="m_and_a_press_release",
                primary_entities=["NVDA", "XYZ", math.nan],
                fields=fields,
                url="https://bw.example.com/a",
            ),
            _row(
                "b",
                timestamp="2026-05-31T08:05:00Z",
                template_id="m_and_a_press_release",
                primary_entities=["NVDA", "XYZ"],
                fields=fields,
                url="https://reuters.example.com/b",
            ),
        ]
        out = dedup.dedup_template_events(_frame(rows), time_col="published_at")
        self.assertEqual(len(out), 1)
        self.assertEqual(out.iloc[0]["dedup_count"], 2)

    def test_exact_24h_boundary_clusters_together(self):
        # The sliding-anchor rule uses strict ``>`` against the window —
        # rows landing EXACTLY 24h after the anchor are inside the
        # cluster, not outside. Pins the inclusive boundary so a future
        # refactor to ``>=`` breaks loudly.
        fields = {"acquirer_ticker": "NVDA", "target_ticker": "XYZ"}
        rows = [
            _row(
                "anchor",
                timestamp="2026-05-30T08:00:00Z",
                template_id="m_and_a_press_release",
                primary_entities=["NVDA", "XYZ"],
                fields=fields,
                url="https://bw.example.com/anchor",
            ),
            _row(
                "exactly_24h",
                timestamp="2026-05-31T08:00:00Z",
                template_id="m_and_a_press_release",
                primary_entities=["NVDA", "XYZ"],
                fields=fields,
                url="https://reuters.example.com/exact",
            ),
        ]
        out = dedup.dedup_template_events(_frame(rows), time_col="published_at")
        self.assertEqual(len(out), 1)
        self.assertEqual(out.iloc[0]["dedup_count"], 2)

    def test_just_past_24h_boundary_splits(self):
        # One second past the anchor + 24h must split into two clusters.
        # Complement to the exact-boundary test above.
        fields = {"acquirer_ticker": "NVDA", "target_ticker": "XYZ"}
        rows = [
            _row(
                "anchor",
                timestamp="2026-05-30T08:00:00Z",
                template_id="m_and_a_press_release",
                primary_entities=["NVDA", "XYZ"],
                fields=fields,
                url="https://bw.example.com/anchor",
            ),
            _row(
                "past",
                timestamp="2026-05-31T08:00:01Z",
                template_id="m_and_a_press_release",
                primary_entities=["NVDA", "XYZ"],
                fields=fields,
                url="https://reuters.example.com/past",
            ),
        ]
        out = dedup.dedup_template_events(_frame(rows), time_col="published_at")
        self.assertEqual(len(out), 2)

    def test_missing_news_id_column_does_not_raise(self):
        # An upstream schema regression dropping ``news_id`` from the
        # post-join frame must not crash dedup — the sort falls through
        # to (richness, timestamp) and the news_ids audit column lands
        # null. Pre-PR-2 frames + speculative future direct callers
        # both benefit.
        rows = [
            {
                "event_type": "m_and_a",
                "primary_entities": ["NVDA", "XYZ"],
                "template_id": "m_and_a_press_release",
                "template_fields_json": json.dumps(
                    {"acquirer_ticker": "NVDA", "target_ticker": "XYZ"}, sort_keys=True
                ),
                "extraction_method": "template",
                "url": "https://bw.example.com/a",
                "source": "businesswire",
                "published_at": pd.Timestamp("2026-05-31T08:00:00Z"),
            },
            {
                "event_type": "m_and_a",
                "primary_entities": ["NVDA", "XYZ"],
                "template_id": "m_and_a_press_release",
                "template_fields_json": json.dumps(
                    {"acquirer_ticker": "NVDA", "target_ticker": "XYZ"}, sort_keys=True
                ),
                "extraction_method": "template",
                "url": "https://reuters.example.com/b",
                "source": "reuters",
                "published_at": pd.Timestamp("2026-05-31T08:05:00Z"),
            },
        ]
        out = dedup.dedup_template_events(_frame(rows), time_col="published_at")
        self.assertEqual(len(out), 1)
        self.assertEqual(out.iloc[0]["dedup_count"], 2)
        # No news_id column → no news_id audit list, but the URL audit
        # list still surfaces (chronological order).
        self.assertIsNone(out.iloc[0]["dedup_news_ids_json"])
        urls = json.loads(out.iloc[0]["dedup_source_urls_json"])
        self.assertEqual(urls, ["https://bw.example.com/a", "https://reuters.example.com/b"])


if __name__ == "__main__":
    unittest.main()
