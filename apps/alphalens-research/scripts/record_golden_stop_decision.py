"""Record the golden corpus of the daemon's two post-fill stop arms (#1581).

ONE-TIME capture, and the timing is the whole point: this must run while
``position_manager._maybe_trail`` and ``_maybe_reanchor`` still hold their OWN
copy of the stop decision. PR B2 of #1581 makes them delegate that decision to
``broker_contract.stop_decision``; the corpus recorded here is the evidence that
the delegation did not move a live answer. A corpus recorded AFTER the
delegation proves nothing, because it would be the new code agreeing with
itself.

    cd apps/alphalens-research
    uv run python -m scripts.record_golden_stop_decision

Each case records every ``AmendStop`` field, the composed ratchet floor, and
every log record with its LEVEL -- the level because this daemon is quiet on a
happy tick, so a refusal demoted to ``debug`` vanishes from the operator's
journal while every answer stays identical.

No network, no credentials, no warm cache, nothing under ``~/.alphalens``:
neither arm writes a file, opens a socket, starts a subprocess or reads an
environment variable, so the capture is a pure in-process drive of the real
functions over the frozen table in ``tests/golden/stop_decision_cases.py``. That
table and the record builder are shared with the replay test on purpose, so the
recording and the assertion cannot describe different cases.

## The overwrite guard

The script REFUSES to write over an existing corpus, with no flag to override,
following ``scripts/record_golden_map.py`` and for its reason: overwriting
destroys the historical comparison a characterization golden exists for. Here
the risk is sharper than usual -- a re-run after PR B2 lands would quietly
rewrite the corpus to match the delegated code, which is the
rewrite-the-test-to-match-the-code failure this corpus exists to prevent. To
re-record deliberately, delete the file in its own commit and say why there.

## Floats

``json.dumps`` is called with ``allow_nan`` left on, as
``scripts/record_golden_score.py`` does and for the reason its comment gives:
NaN and the infinities round-trip natively instead of being coerced to null. The
table carries all three on purpose, so coercion would lose the cases.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from tests.golden.stop_decision_cases import BUCKETS, CASES, run_case, shape_violations

_GOLDEN = (
    Path(__file__).resolve().parents[1]
    / "tests"
    / "golden"
    / "fixtures"
    / "stop_decision"
    / "golden"
    / "corpus.json"
)


def main() -> None:
    if _GOLDEN.exists():
        raise SystemExit(
            f"{_GOLDEN} already holds a recording. Overwriting destroys the "
            "historical comparison this characterization golden exists for, and "
            "a re-run after the #1581 delegation lands would rewrite the corpus "
            "to match the new code. To re-record deliberately, delete the file "
            "in its own commit and say why in the message."
        )
    records = [run_case(case) for case in CASES]

    problems = shape_violations(records)
    if problems:
        raise SystemExit(
            "the recording disagrees with the shapes the buckets declare, so it "
            "is refused rather than committed. A corpus whose case names do not "
            "describe what the case does is worse than no corpus:\n  - " + "\n  - ".join(problems)
        )

    _GOLDEN.parent.mkdir(parents=True, exist_ok=True)
    # Write beside the target and rename, so a crash mid-write cannot leave a
    # partial corpus that the existence guard above then refuses to replace.
    # Same directory as the target so os.replace is an atomic rename on one
    # filesystem (the convention scripts/sync_prometheus_rules.py follows).
    staged = _GOLDEN.with_suffix(".json.partial")
    staged.write_text(json.dumps(records, indent=2, sort_keys=True) + "\n")
    os.replace(staged, _GOLDEN)
    placed = sum(1 for r in records if r["answer"] is not None)
    logged = sum(1 for r in records if r["logs"])
    print(
        f"wrote {len(records)} cases to {_GOLDEN} "
        f"({placed} placed a level, {len(records) - placed} answered None, "
        f"{logged} emitted a log line, {len(BUCKETS)} buckets)"
    )


if __name__ == "__main__":
    main()
