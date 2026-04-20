from pathlib import Path

THEMED_DEFAULTS = {
    # Metric weights (7 metrics, equal weight on start; sum to 1.0)
    "weight_near_high": 1 / 7,
    "weight_pct_20d": 1 / 7,
    "weight_volume_surge": 1 / 7,
    "weight_rel_strength": 1 / 7,
    "weight_rsi": 1 / 7,
    "weight_adx": 1 / 7,
    "weight_macd": 1 / 7,
    # Metric thresholds
    "rsi_low": 50,
    "rsi_high": 75,
    "adx_min": 25,
    "volume_surge_min": 2.0,  # today vs 50d avg
    "pct_20d_min": 0.0,
    "near_high_pct": 0.15,  # within 15% of 52w high -> full score
    # Guardrails (anti-pump)
    "min_market_cap": 300_000_000,
    "min_avg_volume": 1_000_000,
    "min_price": 2.0,
    "reverse_split_lookback_days": 365,
    # Benchmark for relative strength
    "benchmark": "SPY",
    # Output
    "top_n": 5,
    # Data
    "price_lookback_days": 260,
    "batch_size": 50,
}

UNIVERSE_PATH = Path(__file__).parent / "universe.yaml"
