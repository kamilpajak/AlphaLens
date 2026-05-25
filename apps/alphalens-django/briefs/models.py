"""ORM models for thematic briefs.

`Brief` is the unit of work — one ticker, one date, one theme, one full payload.
`DayMeta` is the per-day aggregate (counts + top theme + theme counts) populated
during cache rebuild.

Greenfield deviations from legacy `alphalens/api/schema.py`:

* `gates_passed`, `gates_failed`, `gates_unknown`, `theme_search_keywords`,
  `also_in_themes` are stored as `JSONField` (canonical), not as a parallel
  `*_str` denormalised TEXT column. The legacy `*_str` columns existed because
  SQLite JSON path queries were awkward; Postgres `JSONB` makes them
  redundant. DRF serializers expose the joined string via a method field if
  the frontend still needs it.
* `(date, ticker)` is a real `CompositePrimaryKey`, not a separate surrogate
  id + unique constraint. Django 5.2+ supports it natively.
* `next_earnings_date` is a real `DateField`, not free-form text.
* `valuation_financials_publish_date` keeps `CharField` for now — upstream
  parquet writes ISO date strings AND occasional partial dates (`2024-Q3`).
  Tighten to `DateField` once the writer guarantees ISO 8601.
"""

from __future__ import annotations

from django.db import models


class Brief(models.Model):
    """One thematic brief row — keyed by (date, ticker)."""

    pk = models.CompositePrimaryKey("date", "ticker")

    date = models.DateField(db_index=True)
    ticker = models.CharField(max_length=12)

    theme = models.CharField(max_length=128, db_index=True)
    company_name = models.CharField(max_length=256)
    rationale = models.TextField(blank=True)
    gemini_confidence = models.FloatField(null=True, blank=True)
    market_cap = models.FloatField(null=True, blank=True)

    gates_passed = models.JSONField(default=list, blank=True)
    n_gates_passed = models.IntegerField(default=0)
    gates_failed = models.JSONField(default=list, blank=True)
    n_gates_failed = models.IntegerField(default=0)
    gates_unknown = models.JSONField(default=list, blank=True)
    n_gates_unknown = models.IntegerField(default=0)
    verified = models.BooleanField(default=False)

    source_event_url = models.URLField(max_length=2048, blank=True)
    source_event_title = models.CharField(max_length=512, blank=True)
    source_event_published_at = models.CharField(max_length=64, blank=True)
    theme_search_keywords = models.JSONField(default=list, blank=True)

    industry_id = models.FloatField(null=True, blank=True)
    industry_name = models.CharField(max_length=256, blank=True)
    sector_name = models.CharField(max_length=128, blank=True)
    # Issue #197: peer-cohort resolution level — "sic4" / "sic3" / "thin".
    # "thin" means the percentile fields above were suppressed (no
    # reliable cohort), so the UI should swap the colored bar for a
    # thin-cohort badge. ``default=""`` makes the AddField migration safe
    # over a populated table (CharField defaults to NOT NULL; without an
    # explicit default the DDL would fail on existing Postgres rows).
    peer_cohort_level = models.CharField(max_length=8, blank=True, default="")

    insider_score_usd = models.FloatField(null=True, blank=True)
    insider_score_sector_percentile = models.FloatField(null=True, blank=True)

    fcff_yield_pct = models.FloatField(null=True, blank=True)
    fcff_yield_sector_percentile = models.FloatField(null=True, blank=True)

    valuation_pe = models.FloatField(null=True, blank=True)
    valuation_ps = models.FloatField(null=True, blank=True)
    valuation_ev_rev = models.FloatField(null=True, blank=True)
    valuation_ev_ebitda = models.FloatField(null=True, blank=True)
    valuation_fcf_margin = models.FloatField(null=True, blank=True)
    valuation_composite_sector_percentile = models.FloatField(null=True, blank=True)
    valuation_financials_publish_date = models.CharField(max_length=32, blank=True)
    valuation_financials_age_days = models.IntegerField(null=True, blank=True)

    roic_pct = models.FloatField(null=True, blank=True)
    roe_pct = models.FloatField(null=True, blank=True)
    magic_formula_health_pass = models.BooleanField(default=False)

    technical_rsi = models.FloatField(null=True, blank=True)
    technical_ma50_distance_pct = models.FloatField(null=True, blank=True)
    technical_atr_pct = models.FloatField(null=True, blank=True)
    technical_volume_zscore = models.FloatField(null=True, blank=True)
    technical_pct_off_52w_high = models.FloatField(null=True, blank=True)
    technical_pct_off_52w_low = models.FloatField(null=True, blank=True)
    technical_ma200_distance_pct = models.FloatField(null=True, blank=True)
    technical_ma200_slope_pct_per_day = models.FloatField(null=True, blank=True)

    catalyst_strength = models.FloatField(null=True, blank=True)
    catalyst_event_type = models.CharField(max_length=64, blank=True)
    catalyst_confidence = models.FloatField(null=True, blank=True)

    magic_formula_rank = models.FloatField(null=True, blank=True)
    magic_formula_cohort_n = models.IntegerField(null=True, blank=True)
    deep_drawdown_reversal = models.BooleanField(default=False)

    layer4_weighted_score = models.IntegerField(default=0)

    also_in_themes = models.JSONField(default=list, blank=True)
    rank_in_day = models.IntegerField(null=True, blank=True)
    cohort_size_in_day = models.IntegerField(null=True, blank=True)
    next_earnings_date = models.DateField(null=True, blank=True)

    brief_model_used = models.CharField(max_length=64, blank=True)
    brief_tldr = models.TextField(blank=True)
    brief_supply_chain_md = models.TextField(blank=True)
    brief_bear_summary_md = models.TextField(blank=True)
    brief_catalyst_failure_exit = models.TextField(blank=True)
    brief_entry_price_note = models.TextField(blank=True)
    brief_position_pct = models.FloatField(null=True, blank=True)
    brief_time_exit_weeks = models.IntegerField(null=True, blank=True)
    brief_time_exit_on_catalyst_failure_weeks = models.IntegerField(null=True, blank=True)
    brief_disaster_stop_pct = models.FloatField(null=True, blank=True)
    brief_full_md = models.TextField(blank=True)
    brief_generated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["-date", "-layer4_weighted_score"], name="brief_date_score_idx"),
            models.Index(fields=["ticker"], name="brief_ticker_idx"),
        ]
        ordering = ["-date", "-layer4_weighted_score", "ticker"]

    def __str__(self) -> str:
        return f"{self.date} {self.ticker} ({self.theme})"


class DayMeta(models.Model):
    """Per-day aggregate, populated by the parquet→DB rebuild command."""

    date = models.DateField(primary_key=True)
    n_candidates = models.IntegerField()
    n_themes = models.IntegerField()
    top_theme = models.CharField(max_length=128, blank=True)
    theme_counts = models.JSONField(default=dict)
    parquet_mtime = models.FloatField()
    rebuilt_at = models.DateTimeField()

    class Meta:
        ordering = ["-date"]

    def __str__(self) -> str:
        return f"{self.date} ({self.n_candidates} candidates, {self.n_themes} themes)"
