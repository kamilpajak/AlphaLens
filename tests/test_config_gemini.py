import unittest


class TestBuildGeminiConfig(unittest.TestCase):
    """Shared Gemini config used by `alphalens analyze` and the watchdog worker."""

    def test_llm_provider_is_google(self):
        from alphalens.config_gemini import build_gemini_config

        cfg = build_gemini_config()
        self.assertEqual(cfg["llm_provider"], "google")

    def test_deep_and_quick_models(self):
        from alphalens.config_gemini import build_gemini_config

        cfg = build_gemini_config()
        self.assertEqual(cfg["deep_think_llm"], "gemini-3.1-pro-preview")
        self.assertEqual(cfg["quick_think_llm"], "gemini-3-flash-preview")

    def test_backend_url_none_for_google(self):
        from alphalens.config_gemini import build_gemini_config

        cfg = build_gemini_config()
        self.assertIsNone(cfg["backend_url"])

    def test_google_thinking_level_high(self):
        from alphalens.config_gemini import build_gemini_config

        cfg = build_gemini_config()
        self.assertEqual(cfg["google_thinking_level"], "high")

    def test_debate_rounds_are_one(self):
        from alphalens.config_gemini import build_gemini_config

        cfg = build_gemini_config()
        self.assertEqual(cfg["max_debate_rounds"], 1)
        self.assertEqual(cfg["max_risk_discuss_rounds"], 1)

    def test_data_vendors_route_fundamentals_and_news_to_alpha_vantage(self):
        from alphalens.config_gemini import build_gemini_config

        cfg = build_gemini_config()
        self.assertEqual(cfg["data_vendors"]["core_stock_apis"], "yfinance")
        self.assertEqual(cfg["data_vendors"]["technical_indicators"], "yfinance")
        self.assertEqual(cfg["data_vendors"]["fundamental_data"], "alpha_vantage")
        self.assertEqual(cfg["data_vendors"]["news_data"], "alpha_vantage")

    def test_inherits_default_config_keys(self):
        """Should start from DEFAULT_CONFIG, not construct from scratch — so keys
        like results_dir, online_tools, etc. stay populated without us naming them."""
        from tradingagents.default_config import DEFAULT_CONFIG

        from alphalens.config_gemini import build_gemini_config

        cfg = build_gemini_config()
        for key in DEFAULT_CONFIG:
            self.assertIn(key, cfg, f"missing inherited key: {key}")

    def test_returns_fresh_dict_each_call(self):
        """Mutating the returned dict must not leak across calls."""
        from alphalens.config_gemini import build_gemini_config

        a = build_gemini_config()
        a["llm_provider"] = "mutated"
        b = build_gemini_config()
        self.assertEqual(b["llm_provider"], "google")


if __name__ == "__main__":
    unittest.main()
