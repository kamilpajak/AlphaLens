"""Quick test: run TradingAgents with Gemini models."""

from dotenv import load_dotenv

load_dotenv()

from alphalens.config_gemini import build_gemini_config
from tradingagents.graph.trading_graph import TradingAgentsGraph

config = build_gemini_config()

ta = TradingAgentsGraph(debug=True, config=config)

# Analyze as of a recent trading day
_, decision = ta.propagate("MU", "2026-04-15")
print("\n" + "=" * 60)
print(decision)
