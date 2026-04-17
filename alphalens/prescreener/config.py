PRESCREENER_DEFAULTS = {
    # Scoring weights (must sum to 1.0)
    "weight_fundamental": 0.45,
    "weight_technical": 0.35,
    "weight_volume": 0.20,
    # Technical thresholds
    "rsi_low": 30,
    "rsi_high": 70,
    "adx_min": 25,
    # Fundamental thresholds
    "pe_max": 25.0,
    "peg_max": 1.5,
    "roe_min": 0.12,
    "debt_ebitda_max": 3.0,
    "eps_growth_min": 0.10,
    # Volume thresholds
    "min_avg_volume": 500_000,
    "min_market_cap": 2_000_000_000,
    # Output
    "top_n": 15,
    # Data
    "price_lookback_days": 250,
    "batch_size": 50,
}
