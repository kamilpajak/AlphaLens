"""Quick test: run TradingAgents with Gemini models."""

from dotenv import load_dotenv

load_dotenv()

from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG

config = DEFAULT_CONFIG.copy()

# --- Gemini configuration ---
config["llm_provider"] = "google"
config["deep_think_llm"] = "gemini-3.1-pro-preview"
config["quick_think_llm"] = "gemini-2.5-flash"
config["google_thinking_level"] = "high"
config["backend_url"] = None

# --- Keep it cheap for the first test ---
config["max_debate_rounds"] = 1
config["max_risk_discuss_rounds"] = 1

# --- Data: yfinance for prices, Alpha Vantage for fundamentals & news ---
config["data_vendors"] = {
    "core_stock_apis": "yfinance",
    "technical_indicators": "yfinance",
    "fundamental_data": "alpha_vantage",
    "news_data": "alpha_vantage",
}

ta = TradingAgentsGraph(debug=True, config=config)

# Analyze as of a recent trading day
_, decision = ta.propagate("MU", "2026-04-15")
print("\n" + "=" * 60)
print(decision)
