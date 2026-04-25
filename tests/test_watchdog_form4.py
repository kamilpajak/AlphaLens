import unittest

SAMPLE_FORM4_BUY = """<?xml version="1.0"?>
<ownershipDocument>
  <schemaVersion>X0508</schemaVersion>
  <issuer>
    <issuerCik>0000320193</issuerCik>
    <issuerTradingSymbol>AAPL</issuerTradingSymbol>
  </issuer>
  <reportingOwner>
    <reportingOwnerId><rptOwnerName>John Doe</rptOwnerName></reportingOwnerId>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <transactionCoding>
        <transactionFormType>4</transactionFormType>
        <transactionCode>P</transactionCode>
      </transactionCoding>
      <transactionAmounts>
        <transactionShares><value>1000</value></transactionShares>
        <transactionPricePerShare><value>175.50</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>
"""

SAMPLE_FORM4_SALE = """<?xml version="1.0"?>
<ownershipDocument>
  <issuer><issuerTradingSymbol>AAPL</issuerTradingSymbol></issuer>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <transactionCoding>
        <transactionCode>S</transactionCode>
      </transactionCoding>
      <transactionAmounts>
        <transactionShares><value>500</value></transactionShares>
        <transactionPricePerShare><value>180.00</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>
"""

SAMPLE_FORM4_MULTIPLE = """<?xml version="1.0"?>
<ownershipDocument>
  <issuer><issuerTradingSymbol>AAPL</issuerTradingSymbol></issuer>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>1000</value></transactionShares>
        <transactionPricePerShare><value>175</value></transactionPricePerShare>
      </transactionAmounts>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>500</value></transactionShares>
        <transactionPricePerShare><value>176</value></transactionPricePerShare>
      </transactionAmounts>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <transactionCoding><transactionCode>S</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>100</value></transactionShares>
        <transactionPricePerShare><value>180</value></transactionPricePerShare>
      </transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>
"""


SAMPLE_FORM4_EXERCISE_ONLY = """<?xml version="1.0"?>
<ownershipDocument>
  <issuer><issuerTradingSymbol>AAPL</issuerTradingSymbol></issuer>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <transactionCoding><transactionCode>M</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>2000</value></transactionShares>
        <transactionPricePerShare><value>50</value></transactionPricePerShare>
      </transactionAmounts>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <transactionCoding><transactionCode>F</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>500</value></transactionShares>
        <transactionPricePerShare><value>180</value></transactionPricePerShare>
      </transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>
"""

SAMPLE_FORM4_SALE_WITH_EXERCISE = """<?xml version="1.0"?>
<ownershipDocument>
  <issuer><issuerTradingSymbol>AAPL</issuerTradingSymbol></issuer>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <transactionCoding><transactionCode>S</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>300</value></transactionShares>
        <transactionPricePerShare><value>180</value></transactionPricePerShare>
      </transactionAmounts>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <transactionCoding><transactionCode>M</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>1000</value></transactionShares>
        <transactionPricePerShare><value>50</value></transactionPricePerShare>
      </transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>
"""


class TestForm4Parser(unittest.TestCase):
    def test_parse_transaction_code_P_is_buy(self):
        from alphalens.watchdog.sources.form4 import parse_form4_xml

        result = parse_form4_xml(SAMPLE_FORM4_BUY)
        self.assertEqual(result["insider_action"], "BUY")

    def test_parse_transaction_code_S_is_sale(self):
        from alphalens.watchdog.sources.form4 import parse_form4_xml

        result = parse_form4_xml(SAMPLE_FORM4_SALE)
        self.assertEqual(result["insider_action"], "SELL")

    def test_parse_extracts_shares_and_price_and_value(self):
        from alphalens.watchdog.sources.form4 import parse_form4_xml

        result = parse_form4_xml(SAMPLE_FORM4_BUY)
        self.assertEqual(result["total_shares"], 1000.0)
        self.assertAlmostEqual(result["transaction_value_usd"], 1000 * 175.50)

    def test_parse_handles_multiple_transactions(self):
        """Net buy: 1000+500 buys vs 100 sell → BUY dominates."""
        from alphalens.watchdog.sources.form4 import parse_form4_xml

        result = parse_form4_xml(SAMPLE_FORM4_MULTIPLE)
        self.assertEqual(result["insider_action"], "BUY")
        buy_value = 1000 * 175 + 500 * 176
        self.assertAlmostEqual(result["transaction_value_usd"], buy_value)

    def test_parse_handles_missing_fields_returns_empty_dict(self):
        from alphalens.watchdog.sources.form4 import parse_form4_xml

        result = parse_form4_xml("<?xml version='1.0'?><ownershipDocument/>")
        self.assertEqual(result, {})

    def test_parse_returns_empty_on_malformed_xml(self):
        from alphalens.watchdog.sources.form4 import parse_form4_xml

        result = parse_form4_xml("<bad<<xml")
        self.assertEqual(result, {})

    def test_parse_exercise_only_returns_exercise_action(self):
        """Form 4 with only M (option exercise) + F (tax withholding) — no open-market
        buy/sell. Parser should return EXERCISE so the digest entry shows what happened
        instead of being mute."""
        from alphalens.watchdog.sources.form4 import parse_form4_xml

        result = parse_form4_xml(SAMPLE_FORM4_EXERCISE_ONLY)
        self.assertEqual(result["insider_action"], "EXERCISE")
        self.assertEqual(result["total_shares"], 2500.0)  # 2000 (M) + 500 (F)
        self.assertAlmostEqual(result["transaction_value_usd"], 2000 * 50 + 500 * 180)

    def test_parse_real_sell_overrides_exercise(self):
        """Mixed filing: real S sale + M exercise. Real market action is the signal —
        EXERCISE must not shadow it."""
        from alphalens.watchdog.sources.form4 import parse_form4_xml

        result = parse_form4_xml(SAMPLE_FORM4_SALE_WITH_EXERCISE)
        self.assertEqual(result["insider_action"], "SELL")
        self.assertEqual(result["total_shares"], 300.0)
        self.assertAlmostEqual(result["transaction_value_usd"], 300 * 180)


if __name__ == "__main__":
    unittest.main()
