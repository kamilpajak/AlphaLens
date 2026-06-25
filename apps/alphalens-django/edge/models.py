"""ORM models for the market-behavior edge dashboard.

``LadderOutcome`` is one row per ``(brief_date, ticker)`` — the broker-free
population-ladder monitor's outcome for one surfaced candidate over its real
~42-session hold (TELEMETRY ONLY, per the design memo). It mirrors the
``population_ladders/{date}.parquet`` schema produced by
``alphalens_pipeline.feedback.population_ladder_monitor`` plus the two
benchmark-excess columns written by
``alphalens_pipeline.feedback.benchmark_excess``.

``DayMetaLadderOutcome`` is the per-day rebuild bookkeeping row (counts +
parquet mtime), the same incremental-rebuild gate the briefs cache uses.

Unit conventions (see memo §3 + the discovery's "CRITICAL UNIT RESOLUTION"):

* ``realized_r`` / ``open_r`` / ``mfe`` / ``mae`` / ``ratchet_realized_r`` are
  RISK-NORMALISED (multiples of R = the per-share risk ``blended_entry −
  disaster_stop``). They are the protocol "gross / pre-cost" metric, NEVER a
  raw return.
* ``forward_return`` and ``benchmark_window_return`` are RAW close-to-close
  returns (decimal) over the SAME arrival→exit window. ``market_excess_return``
  = ``forward_return − benchmark_window_return`` is the dashboard headline, also
  a raw return (NOT R-normalised).
* ``*_pct_of_book`` are portfolio-contribution percentages (size layer); kept
  strictly separate from the size-free edge metric.

All numeric fields are nullable: NO_FILL terminal rows have no position so
``realized_r`` is NULL while ``forward_return`` (fill-independent) is set;
non-plannable rows carry NULLs for the whole replay block; older parquets that
predate the size / benchmark columns ingest those as NULL by design.
"""

from __future__ import annotations

from django.db import models


class LadderOutcome(models.Model):
    """One population-ladder outcome — keyed by (brief_date, ticker)."""

    pk = models.CompositePrimaryKey("brief_date", "ticker")

    brief_date = models.DateField(db_index=True)
    ticker = models.CharField(max_length=12)

    # Theme captured AT the brief (provenance), carried in the population-ladder
    # parquet. Empty for older rows that predate the column or when the brief
    # carried no theme (the ingest coerces a NULL cell to ""); the read view maps
    # "" → null. Replaces the fragile downstream join on the (mutable,
    # 6x/day-rebuilt) briefs cache that returned NULL for churned candidates.
    theme = models.CharField(max_length=64, blank=True, default="")

    # Plannability + terminal state.
    plannable = models.BooleanField(default=False)
    nonplannable_reason = models.CharField(max_length=256, blank=True, default="")
    terminal = models.BooleanField(default=False)
    matured_at = models.DateField(null=True, blank=True)
    ladder_classification = models.CharField(max_length=32, blank=True, default="")

    # Ladder output (risk-normalised R-space, size-free).
    blended_entry = models.FloatField(null=True, blank=True)
    realized_r = models.FloatField(null=True, blank=True)
    open_r = models.FloatField(null=True, blank=True)
    mfe = models.FloatField(null=True, blank=True)
    mae = models.FloatField(null=True, blank=True)
    mfe_pct = models.FloatField(null=True, blank=True)
    mae_pct = models.FloatField(null=True, blank=True)

    # Raw close-to-close return (fill-independent) + benchmark-relative excess.
    forward_return = models.FloatField(null=True, blank=True)
    benchmark_window_return = models.FloatField(null=True, blank=True)
    market_excess_return = models.FloatField(null=True, blank=True)

    # Sequence + ratchet what-if.
    sequence_str = models.CharField(max_length=256, blank=True, default="")

    # Pre-computed ladder-chart payload (PR-1): the JSON projection the pipeline
    # enrich step writes onto the parquet (daily OHLC candles + entry/TP/stop
    # price lines + modeled fill/exit markers). The slim Django image cannot
    # import alphalens_pipeline, so the heavy compute (bars + markers) lives
    # pipeline-side; the /v1/edge/chart endpoint only READS this string. "" =
    # older row that predates the column (the chart endpoint treats it as a
    # NO_DATA payload). See alphalens_pipeline.feedback.ladder_chart.
    chart_payload_json = models.TextField(blank=True, default="")
    ambiguous_bars = models.IntegerField(null=True, blank=True)
    ratchet_realized_r = models.FloatField(null=True, blank=True)

    # Holding period (first-fill → exit; NULL for NO_FILL).
    holding_days_elapsed = models.IntegerField(null=True, blank=True)

    # TTL geometry.
    entry_ttl_days = models.IntegerField(null=True, blank=True)
    position_ttl_days = models.IntegerField(null=True, blank=True)

    # Canonical token of the load-bearing replay config (time-stop horizon,
    # order-TTL actually used, arrival-VWAP window, ratchet + same-bar tiebreak
    # rule) that produced this row. A future constant change yields a different
    # token, so a tuning analyst can GROUP BY it to avoid blending two replay
    # geometries into one mean. Empty for non-plannable rows (never replayed).
    ladder_config_version = models.CharField(max_length=256, blank=True, default="")

    # Canonical token identifying which scorer produced the population-monitor
    # rows in this parquet (e.g. "scorer-v1-absdiff-2x"). Allows a tuning
    # analyst to GROUP BY scorer_config_version to avoid blending runs produced
    # by different scoring configs. Empty for rows predating the stamp.
    scorer_config_version = models.CharField(max_length=128, blank=True, default="")

    # Alternate-exit-ladder grid (PR-2): JSON map {config -> realized_r} from
    # re-replaying the SAME bars under each exit policy (single_tp_first /
    # single_tp_last / no_tp_ride). Holds the candidate + entry + stop fixed, so
    # the spread vs realized_r is the trade-management effect, not the pick.
    # Empty until a minute resolve computes it (non-plannable / placeholder rows).
    grid_realized_r_json = models.TextField(blank=True, default="")

    # Entry-side counterfactual (PR-3): realized R if all tiers had filled at the
    # full-ladder blended entry, same exit ladder + bars. Paired with
    # full_ladder_blended_entry; the gap realized_r - realized_r_full_fill is the
    # entry-tier-spacing drag. NULL until a minute resolve computes it.
    realized_r_full_fill = models.FloatField(null=True, blank=True)

    # Portfolio / size layer (additive, NOT the edge). Signal-time (intended).
    suggested_gross_weight_pct = models.FloatField(null=True, blank=True)
    full_ladder_blended_entry = models.FloatField(null=True, blank=True)
    stop_distance_pct_full = models.FloatField(null=True, blank=True)
    implied_risk_pct_full = models.FloatField(null=True, blank=True)
    # Outcome-time (what actually deployed).
    tiers_filled_count = models.FloatField(null=True, blank=True)
    realized_gross_weight_pct = models.FloatField(null=True, blank=True)
    stop_distance_pct = models.FloatField(null=True, blank=True)
    realized_risk_pct = models.FloatField(null=True, blank=True)
    realized_return_pct_of_book = models.FloatField(null=True, blank=True)
    open_return_pct_of_book = models.FloatField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["-brief_date", "ticker"], name="ladder_date_ticker_idx"),
            models.Index(fields=["terminal"], name="ladder_terminal_idx"),
        ]
        ordering = ["-brief_date", "ticker"]

    def __str__(self) -> str:
        state = self.ladder_classification or ("terminal" if self.terminal else "ongoing")
        return f"{self.brief_date} {self.ticker} ({state})"


class DayMetaLadderOutcome(models.Model):
    """Per-day rebuild bookkeeping for the ladder-outcome cache."""

    brief_date = models.DateField(primary_key=True)
    n_rows = models.IntegerField()
    n_plannable = models.IntegerField()
    n_terminal = models.IntegerField()
    parquet_mtime = models.FloatField()
    rebuilt_at = models.DateTimeField()

    class Meta:
        ordering = ["-brief_date"]

    def __str__(self) -> str:
        return f"{self.brief_date} ({self.n_rows} rows, {self.n_terminal} terminal)"
