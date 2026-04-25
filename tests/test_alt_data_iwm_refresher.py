import tempfile
import unittest
from pathlib import Path

import yaml

# Representative iShares IWM CSV shape (junk preamble + real header + rows).
_SAMPLE_CSV = """\
"iShares Russell 2000 ETF"
"Fund Holdings as of Apr 18 2026"
""
""
"Ticker","Name","Sector","Asset Class","Weight (%)","Price","Exchange"
"UPST","Upstart Holdings","Financial","Equity","1.17","82.05","NASDAQ"
"SMCI","Super Micro Computer","Technology","Equity","0.85","44.12","NASDAQ"
"GPC","Genuine Parts Co","Consumer","Equity","0.70","131.44","NYSE"
"-","USD Cash","--","Cash","0.42","1.00","-"
"MYMKT","Money Market","--","Cash Collateral","0.15","1.00","-"
"SWK","Stanley Black & Decker","Industrials","Equity","0.58","69.12","NYSE"
"""


class TestParseIsharesCsv(unittest.TestCase):
    def test_extracts_equity_tickers_only(self):
        from alphalens.alt_data.iwm_refresher import parse_ishares_csv

        tickers = parse_ishares_csv(_SAMPLE_CSV)

        self.assertEqual(tickers, ["UPST", "SMCI", "GPC", "SWK"])

    def test_drops_cash_ticker_dash(self):
        from alphalens.alt_data.iwm_refresher import parse_ishares_csv

        tickers = parse_ishares_csv(_SAMPLE_CSV)

        self.assertNotIn("-", tickers)

    def test_drops_cash_collateral_rows(self):
        from alphalens.alt_data.iwm_refresher import parse_ishares_csv

        tickers = parse_ishares_csv(_SAMPLE_CSV)

        self.assertNotIn("MYMKT", tickers)

    def test_uppercases_tickers(self):
        from alphalens.alt_data.iwm_refresher import parse_ishares_csv

        csv = '\n\n\n"Ticker","Name","Asset Class"\n"aapl","Apple","Equity"\n'

        tickers = parse_ishares_csv(csv)

        self.assertEqual(tickers, ["AAPL"])

    def test_missing_header_raises(self):
        from alphalens.alt_data.iwm_refresher import (
            IsharesCsvFormatError,
            parse_ishares_csv,
        )

        csv = "no header here\njust junk\n"

        with self.assertRaises(IsharesCsvFormatError):
            parse_ishares_csv(csv)

    def test_empty_data_after_header_returns_empty(self):
        from alphalens.alt_data.iwm_refresher import parse_ishares_csv

        csv = '"Ticker","Name","Asset Class"\n'

        self.assertEqual(parse_ishares_csv(csv), [])

    def test_footer_disclaimer_row_dropped(self):
        """iShares CSV has a multi-line legal disclaimer that smashes into
        the first column. Reject anything that doesn't match a ticker pattern."""
        from alphalens.alt_data.iwm_refresher import parse_ishares_csv

        csv_text = (
            '"Ticker","Name","Asset Class"\n'
            '"AAPL","Apple","Equity"\n'
            '"THE CONTENT CONTAINED HEREIN IS OWNED BY BLACKROCK","copyright","Equity"\n'
            '"MSFT","Microsoft","Equity"\n'
        )

        tickers = parse_ishares_csv(csv_text)

        self.assertEqual(tickers, ["AAPL", "MSFT"])

    def test_numeric_pseudo_ticker_dropped(self):
        """iShares includes rows like 'P5N994' (internal codes) that aren't real tickers."""
        from alphalens.alt_data.iwm_refresher import parse_ishares_csv

        csv_text = (
            '"Ticker","Name","Asset Class"\n'
            '"AAPL","Apple","Equity"\n'
            '"P5N994","","Equity"\n'
            '"MPTI RT","Rights","Equity"\n'
        )

        tickers = parse_ishares_csv(csv_text)

        self.assertEqual(tickers, ["AAPL"])

    def test_class_share_suffix_allowed(self):
        """BRK.B / GOOG-L style class shares must be kept."""
        from alphalens.alt_data.iwm_refresher import parse_ishares_csv

        csv_text = (
            '"Ticker","Name","Asset Class"\n'
            '"BRK.B","Berkshire B","Equity"\n'
            '"GOOG-L","Alphabet L","Equity"\n'
        )

        tickers = parse_ishares_csv(csv_text)

        self.assertEqual(set(tickers), {"BRK.B", "GOOG-L"})

    def test_dedups_preserving_first_occurrence(self):
        from alphalens.alt_data.iwm_refresher import parse_ishares_csv

        csv = (
            '"Ticker","Name","Asset Class"\n'
            '"AAPL","Apple","Equity"\n'
            '"MSFT","Microsoft","Equity"\n'
            '"AAPL","Apple dupe","Equity"\n'
        )

        tickers = parse_ishares_csv(csv)

        self.assertEqual(tickers, ["AAPL", "MSFT"])


class TestRefresh(unittest.TestCase):
    def test_writes_yaml_compatible_with_load_iwm_current(self):
        from alphalens.alt_data.iwm_refresher import refresh_iwm_current
        from alphalens.alt_data.russell_universe import load_iwm_current

        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "iwm.yaml"

            count = refresh_iwm_current(
                out,
                csv_text_fetcher=lambda: _SAMPLE_CSV,
            )

            self.assertEqual(count, 4)
            tickers = load_iwm_current(out)

        self.assertEqual(tickers, ["UPST", "SMCI", "GPC", "SWK"])

    def test_fallback_on_fetch_error_copies_fallback(self):
        from alphalens.alt_data.iwm_refresher import refresh_iwm_current
        from alphalens.alt_data.russell_universe import load_iwm_current

        def broken_fetcher():
            raise RuntimeError("network down")

        fallback_payload = {"tickers": ["FALLBACK1", "FALLBACK2"]}

        with tempfile.TemporaryDirectory() as td:
            fallback = Path(td) / "fallback.yaml"
            fallback.write_text(yaml.safe_dump(fallback_payload))
            out = Path(td) / "iwm.yaml"

            count = refresh_iwm_current(
                out,
                csv_text_fetcher=broken_fetcher,
                fallback_path=fallback,
            )

            self.assertEqual(count, 2)
            self.assertEqual(load_iwm_current(out), ["FALLBACK1", "FALLBACK2"])

    def test_fallback_missing_reraises(self):
        from alphalens.alt_data.iwm_refresher import refresh_iwm_current

        def broken_fetcher():
            raise RuntimeError("network down")

        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "iwm.yaml"

            with self.assertRaises(RuntimeError):
                refresh_iwm_current(out, csv_text_fetcher=broken_fetcher)

    def test_format_error_also_triggers_fallback(self):
        """Parse failure should fallback-or-raise, same as fetch failure."""
        from alphalens.alt_data.iwm_refresher import refresh_iwm_current

        def garbage_fetcher():
            return "nothing resembling a CSV"

        fallback_payload = {"tickers": ["X"]}

        with tempfile.TemporaryDirectory() as td:
            fallback = Path(td) / "fb.yaml"
            fallback.write_text(yaml.safe_dump(fallback_payload))
            out = Path(td) / "iwm.yaml"

            count = refresh_iwm_current(
                out, csv_text_fetcher=garbage_fetcher, fallback_path=fallback
            )

            self.assertEqual(count, 1)

    def test_creates_parent_dirs(self):
        from alphalens.alt_data.iwm_refresher import refresh_iwm_current

        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "nested" / "deeper" / "iwm.yaml"

            refresh_iwm_current(out, csv_text_fetcher=lambda: _SAMPLE_CSV)

            self.assertTrue(out.exists())


if __name__ == "__main__":
    unittest.main()
