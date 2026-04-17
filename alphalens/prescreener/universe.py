"""S&P 500 universe: fetch from Wikipedia with hardcoded fallback."""

from __future__ import annotations

import io
import logging

import pandas as pd

logger = logging.getLogger(__name__)

# fmt: off
SP500_FALLBACK: list[str] = [
    "A", "AAPL", "ABBV", "ABNB", "ABT", "ACGL", "ACN", "ADBE", "ADI", "ADM",
    "ADP", "ADSK", "AEE", "AEP", "AES", "AFL", "AIG", "AIZ", "AJG", "AKAM",
    "ALB", "ALGN", "ALL", "ALLE", "AMAT", "AMCR", "AMD", "AME", "AMGN", "AMP",
    "AMT", "AMZN", "ANET", "AON", "AOS", "APA", "APD", "APH", "APO", "APP",
    "APTV", "ARE", "ARES", "ATO", "AVB", "AVGO", "AVY", "AWK", "AXON", "AXP",
    "AZO", "BA", "BAC", "BALL", "BAX", "BBY", "BDX", "BEN", "BF-B", "BG",
    "BIIB", "BK", "BKNG", "BKR", "BLDR", "BLK", "BMY", "BR", "BRK-B", "BRO",
    "BSX", "BX", "BXP", "C", "CAG", "CAH", "CARR", "CASY", "CAT", "CB",
    "CBOE", "CBRE", "CCI", "CCL", "CDNS", "CDW", "CEG", "CF", "CFG", "CHD",
    "CHRW", "CHTR", "CI", "CIEN", "CINF", "CL", "CLX", "CMCSA", "CME", "CMG",
    "CMI", "CMS", "CNC", "CNP", "COF", "COHR", "COIN", "COO", "COP", "COR",
    "COST", "CPAY", "CPB", "CPRT", "CPT", "CRH", "CRL", "CRM", "CRWD", "CSCO",
    "CSGP", "CSX", "CTAS", "CTRA", "CTSH", "CTVA", "CVNA", "CVS", "CVX", "D",
    "DAL", "DASH", "DD", "DDOG", "DE", "DECK", "DELL", "DG", "DGX", "DHI",
    "DHR", "DIS", "DLR", "DLTR", "DOC", "DOV", "DOW", "DPZ", "DRI", "DTE",
    "DUK", "DVA", "DVN", "DXCM", "EA", "EBAY", "ECL", "ED", "EFX", "EG",
    "EIX", "EL", "ELV", "EME", "EMR", "EOG", "EPAM", "EQIX", "EQR", "EQT",
    "ERIE", "ES", "ESS", "ETN", "ETR", "EVRG", "EW", "EXC", "EXE", "EXPD",
    "EXPE", "EXR", "F", "FANG", "FAST", "FCX", "FDS", "FDX", "FE", "FFIV",
    "FICO", "FIS", "FISV", "FITB", "FIX", "FOX", "FOXA", "FRT", "FSLR", "FTNT",
    "FTV", "GD", "GDDY", "GE", "GEHC", "GEN", "GEV", "GILD", "GIS", "GL",
    "GLW", "GM", "GNRC", "GOOG", "GOOGL", "GPC", "GPN", "GRMN", "GS", "GWW",
    "HAL", "HAS", "HBAN", "HCA", "HD", "HIG", "HII", "HLT", "HON", "HOOD",
    "HPE", "HPQ", "HRL", "HSIC", "HST", "HSY", "HUBB", "HUM", "HWM", "IBKR",
    "IBM", "ICE", "IDXX", "IEX", "IFF", "INCY", "INTC", "INTU", "INVH", "IP",
    "IQV", "IR", "IRM", "ISRG", "IT", "ITW", "IVZ", "J", "JBHT", "JBL",
    "JCI", "JKHY", "JNJ", "JPM", "KDP", "KEY", "KEYS", "KHC", "KIM", "KKR",
    "KLAC", "KMB", "KMI", "KO", "KR", "KVUE", "L", "LDOS", "LEN", "LH",
    "LHX", "LII", "LIN", "LITE", "LLY", "LMT", "LNT", "LOW", "LRCX", "LULU",
    "LUV", "LVS", "LYB", "LYV", "MA", "MAA", "MAR", "MAS", "MCD", "MCHP",
    "MCK", "MCO", "MDLZ", "MDT", "MET", "META", "MGM", "MKC", "MLM", "MMM",
    "MNST", "MO", "MOS", "MPC", "MPWR", "MRK", "MRNA", "MRSH", "MS", "MSCI",
    "MSFT", "MSI", "MTB", "MTD", "MU", "NCLH", "NDAQ", "NDSN", "NEE", "NEM",
    "NFLX", "NI", "NKE", "NOC", "NOW", "NRG", "NSC", "NTAP", "NTRS", "NUE",
    "NVDA", "NVR", "NWS", "NWSA", "NXPI", "O", "ODFL", "OKE", "OMC", "ON",
    "ORCL", "ORLY", "OTIS", "OXY", "PANW", "PAYX", "PCAR", "PCG", "PEG", "PEP",
    "PFE", "PFG", "PG", "PGR", "PH", "PHM", "PKG", "PLD", "PLTR", "PM",
    "PNC", "PNR", "PNW", "PODD", "POOL", "PPG", "PPL", "PRU", "PSA", "PSKY",
    "PSX", "PTC", "PWR", "PYPL", "Q", "QCOM", "RCL", "REG", "REGN", "RF",
    "RJF", "RL", "RMD", "ROK", "ROL", "ROP", "ROST", "RSG", "RTX", "RVTY",
    "SATS", "SBAC", "SBUX", "SCHW", "SHW", "SJM", "SLB", "SMCI", "SNA", "SNDK",
    "SNPS", "SO", "SOLV", "SPG", "SPGI", "SRE", "STE", "STLD", "STT", "STX",
    "STZ", "SW", "SWK", "SWKS", "SYF", "SYK", "SYY", "T", "TAP", "TDG",
    "TDY", "TECH", "TEL", "TER", "TFC", "TGT", "TJX", "TKO", "TMO", "TMUS",
    "TPL", "TPR", "TRGP", "TRMB", "TROW", "TRV", "TSCO", "TSLA", "TSN", "TT",
    "TTD", "TTWO", "TXN", "TXT", "TYL", "UAL", "UBER", "UDR", "UHS", "ULTA",
    "UNH", "UNP", "UPS", "URI", "USB", "V", "VICI", "VLO", "VLTO", "VMC",
    "VRSK", "VRSN", "VRT", "VRTX", "VST", "VTR", "VTRS", "VZ", "WAB", "WAT",
    "WBD", "WDAY", "WDC", "WEC", "WELL", "WFC", "WM", "WMB", "WMT", "WRB",
    "WSM", "WST", "WTW", "WY", "WYNN", "XEL", "XOM", "XYL", "XYZ", "YUM",
    "ZBH", "ZBRA", "ZTS",
]
# fmt: on


def get_sp500_tickers() -> list[str]:
    """Return S&P 500 constituents. Fetches from Wikipedia, falls back to snapshot."""
    try:
        return _fetch_from_wikipedia()
    except Exception:
        logger.warning("Wikipedia fetch failed, using fallback S&P 500 list")
        return list(SP500_FALLBACK)


def _fetch_from_wikipedia() -> list[str]:
    import requests

    resp = requests.get(
        "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
        headers={"User-Agent": "AlphaLens/1.0"},
        timeout=10,
    )
    resp.raise_for_status()
    tables = pd.read_html(io.StringIO(resp.text))
    symbols = tables[0]["Symbol"].str.replace(".", "-", regex=False).tolist()
    return sorted(symbols)
