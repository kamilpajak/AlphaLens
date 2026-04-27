"""Tests for `alphalens.literature_review` module.

Covers: prompt loading, Perplexity HTTP client request shape, runner
orchestration (file write + Telegram dispatch + git branch commit),
and TRIGGER_REACTIVATION detection.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SAMPLE_MONTHLY_RESPONSE = """\
# Literature Review — 2026-05

## Scanned
12 papers across 4 baskets (retail order flow, LLM 10-K intangibles,
cross-asset overlays, factor decay 2025+).

## Survivors after 5-filter triage

| Title | Sample | Net? | Public? | n | Multiple-test? | Verdict |
|---|---|---|---|---|---|---|
| Patient Limit Orders for Retail | 2003-2024 | yes | yes | 5M | yes | WORTH_DEEPER_READ |
| LLM Intangible Risk Disclosures | 2010-2025 | yes | yes | 200K | partial | SKIP |

## TRIGGER_REACTIVATION candidates
None this month.

## Raw notes
...
"""

SAMPLE_TRIGGER_RESPONSE = """\
# Literature Review — 2026-09

## TRIGGER_REACTIVATION candidates

- Hou, Xue, Zhang 2026 — replication of factor X passes all 5 filters
  with OOS post-2020 t-stat 3.4 net of costs.
"""


class TestTriggerDetection(unittest.TestCase):
    def test_no_trigger_when_response_says_none(self):
        from alphalens.literature_review.runner import has_reactivation_trigger

        self.assertFalse(has_reactivation_trigger(SAMPLE_MONTHLY_RESPONSE))

    def test_trigger_detected_when_candidate_listed(self):
        from alphalens.literature_review.runner import has_reactivation_trigger

        self.assertTrue(has_reactivation_trigger(SAMPLE_TRIGGER_RESPONSE))

    def test_trigger_section_must_have_content_below(self):
        from alphalens.literature_review.runner import has_reactivation_trigger

        # Heading present but only "None" content -> no trigger
        empty = "## TRIGGER_REACTIVATION candidates\n\nNone this month.\n"
        self.assertFalse(has_reactivation_trigger(empty))

    def test_no_trigger_on_alternative_dismissals(self):
        """Phrases like 'None of the above' or 'No papers this period' must
        also be treated as no-trigger. The previous regex required the exact
        prefix 'none this' which silently flipped these to trigger=True.
        """
        from alphalens.literature_review.runner import has_reactivation_trigger

        for body in (
            "## TRIGGER_REACTIVATION candidates\n\nNone of the above.\n",
            "## TRIGGER_REACTIVATION candidates\n\nNone at this time.\n",
            "## TRIGGER_REACTIVATION candidates\n\nNo papers this period.\n",
            "## TRIGGER_REACTIVATION candidates\n\nNo relevant work surfaced.\n",
            "## TRIGGER_REACTIVATION candidates\n\n   Nothing meaningful.\n",
        ):
            with self.subTest(body=body):
                self.assertFalse(has_reactivation_trigger(body))


class TestPerplexityClient(unittest.TestCase):
    @patch("alphalens.literature_review.perplexity_client.requests.post")
    def test_ask_posts_to_chat_completions(self, mock_post):
        from alphalens.literature_review.perplexity_client import PerplexityClient

        mock_post.return_value = MagicMock(
            status_code=200,
            raise_for_status=MagicMock(),
            json=MagicMock(return_value={"choices": [{"message": {"content": "research result"}}]}),
        )
        client = PerplexityClient(api_key="pplx-test")
        result = client.ask("query", search_context_size="high")

        self.assertEqual(result, "research result")
        url = mock_post.call_args.args[0]
        self.assertIn("api.perplexity.ai", url)
        self.assertIn("chat/completions", url)

    @patch("alphalens.literature_review.perplexity_client.requests.post")
    def test_ask_includes_search_context_size(self, mock_post):
        from alphalens.literature_review.perplexity_client import PerplexityClient

        mock_post.return_value = MagicMock(
            status_code=200,
            raise_for_status=MagicMock(),
            json=MagicMock(return_value={"choices": [{"message": {"content": "x"}}]}),
        )
        client = PerplexityClient(api_key="pplx-test")
        client.ask("q", search_context_size="high")

        body = mock_post.call_args.kwargs["json"]
        self.assertEqual(body["web_search_options"]["search_context_size"], "high")

    @patch("alphalens.literature_review.perplexity_client.requests.post")
    def test_ask_sends_bearer_auth(self, mock_post):
        from alphalens.literature_review.perplexity_client import PerplexityClient

        mock_post.return_value = MagicMock(
            status_code=200,
            raise_for_status=MagicMock(),
            json=MagicMock(return_value={"choices": [{"message": {"content": "x"}}]}),
        )
        client = PerplexityClient(api_key="pplx-test")
        client.ask("q")

        headers = mock_post.call_args.kwargs["headers"]
        self.assertEqual(headers["Authorization"], "Bearer pplx-test")

    def test_constructor_rejects_empty_key(self):
        from alphalens.literature_review.perplexity_client import PerplexityClient

        with self.assertRaises(ValueError):
            PerplexityClient(api_key="")


class TestPrompts(unittest.TestCase):
    def test_monthly_prompt_mentions_4_topic_baskets(self):
        from alphalens.literature_review.prompts import build_monthly_prompt

        prompt = build_monthly_prompt(period="2026-05")
        # 4 baskets per project pivot postmortem
        self.assertIn("retail order flow", prompt.lower())
        self.assertIn("intangible", prompt.lower())
        self.assertIn("cross-asset", prompt.lower())
        self.assertIn("factor decay", prompt.lower())

    def test_monthly_prompt_includes_triage_filters(self):
        from alphalens.literature_review.prompts import build_monthly_prompt

        prompt = build_monthly_prompt(period="2026-05")
        self.assertIn("15", prompt)  # sample period >= 15y
        self.assertIn("OOS", prompt)
        self.assertIn("CRSP", prompt)
        self.assertIn("multiple", prompt.lower())  # multiple-testing

    def test_monthly_prompt_demands_trigger_section(self):
        from alphalens.literature_review.prompts import build_monthly_prompt

        prompt = build_monthly_prompt(period="2026-05")
        self.assertIn("TRIGGER_REACTIVATION", prompt)

    def test_monthly_prompt_makes_disconfirming_section_conditional(self):
        """First smoke test exposed Perplexity literally echoing 'Skip section
        if no triggers' as content. The instruction must be unambiguous about
        omitting the heading entirely when there are no triggers.
        """
        from alphalens.literature_review.prompts import build_monthly_prompt

        prompt = build_monthly_prompt(period="2026-05")
        lower = prompt.lower()
        self.assertIn("only when", lower)
        self.assertIn("omit this entire", lower)

    def test_monthly_prompt_widens_window_to_2024_plus(self):
        """First smoke test had Perplexity report '0 papers' because the
        prompt narrowed to 2025-2026. Widen to 2024+ with 2025+ priority.
        """
        from alphalens.literature_review.prompts import build_monthly_prompt

        prompt = build_monthly_prompt(period="2026-05")
        self.assertIn("2024", prompt)

    def test_monthly_prompt_provides_empty_basket_fallback(self):
        from alphalens.literature_review.prompts import build_monthly_prompt

        prompt = build_monthly_prompt(period="2026-05")
        self.assertIn("background reading", prompt)

    def test_weekly_prompt_is_shorter_and_mentions_top_3(self):
        from alphalens.literature_review.prompts import build_weekly_prompt

        monthly = __import__(
            "alphalens.literature_review.prompts", fromlist=["build_monthly_prompt"]
        ).build_monthly_prompt(period="2026-W17")
        weekly = build_weekly_prompt(period="2026-W17")
        self.assertLess(len(weekly), len(monthly))
        self.assertIn("top", weekly.lower())


class TestRunnerOrchestration(unittest.TestCase):
    def setUp(self):
        # Patch all I/O at module-level for orchestration tests
        self.client_patcher = patch("alphalens.literature_review.runner.PerplexityClient")
        self.telegram_patcher = patch("alphalens.literature_review.runner.TelegramHandler")
        self.client_cls = self.client_patcher.start()
        self.telegram_cls = self.telegram_patcher.start()
        self.client_instance = self.client_cls.return_value
        self.telegram_instance = self.telegram_cls.return_value
        self.client_instance.ask.return_value = SAMPLE_MONTHLY_RESPONSE

    def tearDown(self):
        self.client_patcher.stop()
        self.telegram_patcher.stop()

    def test_run_monthly_writes_markdown_to_correct_path(self):
        import tempfile

        from alphalens.literature_review.runner import run_monthly

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            result = run_monthly(
                output_dir=out_dir,
                perplexity_api_key="pplx-x",
                telegram_bot_token="bot",
                telegram_chat_id="chat",
                period="2026-05",
            )
            expected = out_dir / "2026-05.md"
            self.assertTrue(expected.exists())
            self.assertEqual(result.path, expected)
            content = expected.read_text()
            self.assertIn("Literature Review", content)

    def test_run_monthly_dispatches_telegram_digest(self):
        import tempfile

        from alphalens.literature_review.runner import run_monthly

        with tempfile.TemporaryDirectory() as tmp:
            run_monthly(
                output_dir=Path(tmp),
                perplexity_api_key="pplx-x",
                telegram_bot_token="bot",
                telegram_chat_id="chat",
                period="2026-05",
            )
        self.telegram_instance.send_message.assert_called_once()
        digest = self.telegram_instance.send_message.call_args.args[0]
        self.assertIn("2026-05", digest)
        # Telegram digest should be terse — under 600 chars per spec
        self.assertLess(len(digest), 600)

    def test_run_monthly_skips_telegram_when_chat_id_missing(self):
        import tempfile

        from alphalens.literature_review.runner import run_monthly

        with tempfile.TemporaryDirectory() as tmp:
            run_monthly(
                output_dir=Path(tmp),
                perplexity_api_key="pplx-x",
                telegram_bot_token="",
                telegram_chat_id="",
                period="2026-05",
            )
        self.telegram_cls.assert_not_called()

    def test_run_weekly_uses_weekly_subdir(self):
        import tempfile

        from alphalens.literature_review.runner import run_weekly

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            result = run_weekly(
                output_dir=out_dir,
                perplexity_api_key="pplx-x",
                telegram_bot_token="bot",
                telegram_chat_id="chat",
                period="2026-W18",
            )
            expected = out_dir / "weekly" / "2026-W18.md"
            self.assertTrue(expected.exists())
            self.assertEqual(result.path, expected)

    def test_run_monthly_returns_trigger_flag(self):
        import tempfile

        from alphalens.literature_review.runner import run_monthly

        self.client_instance.ask.return_value = SAMPLE_TRIGGER_RESPONSE
        with tempfile.TemporaryDirectory() as tmp:
            result = run_monthly(
                output_dir=Path(tmp),
                perplexity_api_key="pplx-x",
                telegram_bot_token="bot",
                telegram_chat_id="chat",
                period="2026-09",
            )
        self.assertTrue(result.has_trigger)
        digest = self.telegram_instance.send_message.call_args.args[0]
        self.assertIn("TRIGGER", digest)

    def test_run_monthly_passes_year_recency_filter(self):
        """Live smoke test had Perplexity return 0 papers without recency
        biasing. We anchor the search window via the API's recency filter.
        """
        import tempfile

        from alphalens.literature_review.runner import run_monthly

        with tempfile.TemporaryDirectory() as tmp:
            run_monthly(
                output_dir=Path(tmp),
                perplexity_api_key="pplx-x",
                telegram_bot_token="",
                telegram_chat_id="",
                period="2026-05",
            )
        kwargs = self.client_instance.ask.call_args.kwargs
        self.assertEqual(kwargs.get("search_recency_filter"), "year")


class TestPeriodFormat(unittest.TestCase):
    def test_default_monthly_period_is_yyyy_mm(self):
        from datetime import date

        from alphalens.literature_review.runner import default_period

        result = default_period(date(2026, 5, 1), cadence="monthly")
        self.assertEqual(result, "2026-05")

    def test_default_weekly_period_uses_iso_week(self):
        from datetime import date

        from alphalens.literature_review.runner import default_period

        # 2026-05-03 is Sunday of ISO week 18
        result = default_period(date(2026, 5, 3), cadence="weekly")
        self.assertEqual(result, "2026-W18")


class TestLayerStatus(unittest.TestCase):
    def test_literature_review_module_declares_active_status(self):
        import alphalens.literature_review as mod

        self.assertEqual(mod.__status__, "ACTIVE")


if __name__ == "__main__":
    unittest.main()
