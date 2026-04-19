"""Convert OHLCV bars to QuantConnect Lean's equity-daily on-disk format.

Lean expects:
- Path: `<data_dir>/equity/usa/daily/<ticker>.zip` (ticker lowercase)
- Zip contains one file: `<ticker>.csv` (lowercase, no extension duplication)
- CSV columns, no header:
    `YYYYMMDD 00:00,open*10000,high*10000,low*10000,close*10000,volume`
- One row per trading day, sorted ascending by date.

Prices are scaled by 10_000 (Lean's "deci-thousands" integer convention).
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from pathlib import Path

_PRICE_SCALE = 10_000


@dataclass(frozen=True)
class DailyBar:
    date: str            # "YYYYMMDD"
    open: float
    high: float
    low: float
    close: float
    volume: int


def format_bar(bar: DailyBar) -> str:
    return (
        f"{bar.date} 00:00,"
        f"{int(round(bar.open * _PRICE_SCALE))},"
        f"{int(round(bar.high * _PRICE_SCALE))},"
        f"{int(round(bar.low * _PRICE_SCALE))},"
        f"{int(round(bar.close * _PRICE_SCALE))},"
        f"{bar.volume}"
    )


def parse_bar(line: str) -> DailyBar:
    parts = line.strip().split(",")
    if len(parts) != 6:
        raise ValueError(f"Expected 6 fields, got {len(parts)}: {line!r}")
    ts, o, h, l, c, v = parts
    return DailyBar(
        date=ts.split(" ")[0],
        open=int(o) / _PRICE_SCALE,
        high=int(h) / _PRICE_SCALE,
        low=int(l) / _PRICE_SCALE,
        close=int(c) / _PRICE_SCALE,
        volume=int(v),
    )


class LeanCsvWriter:
    """Per-ticker daily-bar ZIPs under `<data_dir>/equity/usa/daily/`."""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)

    def path_for(self, ticker: str) -> Path:
        return (
            self.data_dir / "equity" / "usa" / "daily" / f"{ticker.lower()}.zip"
        )

    def read_bars(self, ticker: str) -> list[DailyBar]:
        target = self.path_for(ticker)
        if not target.exists():
            return []
        with zipfile.ZipFile(target, "r") as zf:
            csv_name = f"{ticker.lower()}.csv"
            with zf.open(csv_name) as fh:
                text = fh.read().decode("utf-8")
        return [parse_bar(line) for line in text.splitlines() if line.strip()]

    def write_bars(self, ticker: str, bars: list[DailyBar]) -> None:
        """Overwrite the zip with `bars` (sorted by date). Atomic via tmp + rename."""
        if not bars:
            raise ValueError("Refusing to write empty bar list")
        target = self.path_for(ticker)
        target.parent.mkdir(parents=True, exist_ok=True)
        sorted_bars = sorted(bars, key=lambda b: b.date)

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            csv_body = "\n".join(format_bar(b) for b in sorted_bars) + "\n"
            zf.writestr(f"{ticker.lower()}.csv", csv_body)

        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_bytes(buf.getvalue())
        tmp.replace(target)

    def upsert_bars(self, ticker: str, new_bars: list[DailyBar]) -> int:
        """Merge `new_bars` with existing, dedup by date (new wins), rewrite. Returns row count."""
        existing = self.read_bars(ticker)
        by_date = {b.date: b for b in existing}
        for b in new_bars:
            by_date[b.date] = b
        merged = list(by_date.values())
        if merged:
            self.write_bars(ticker, merged)
        return len(merged)
