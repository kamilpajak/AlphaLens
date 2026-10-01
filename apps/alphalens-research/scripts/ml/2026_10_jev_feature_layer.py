"""Build a System One feature layer over the stored news and brief candidates.

WHAT THIS IS, AND WHAT IT IS NOT
--------------------------------
It computes features and writes them to a store. It fits no model, computes no
CV metric and reads NO outcome column — no `sel_ar_*`, no `car_*`. House rule 11
makes a run a LOOK when it computes a metric or p-value against an EDGE outcome,
so this is not a look and carries no ledger charge. The FIRST run that joins
these columns to `sel_ar_20` is a look and needs its own row; preferably a
registration rather than an exploration, because by then the feature list is
known and a registered test is the only thing that can promote it.

Most of the README house rules are about fitting (episode dedup, GroupKFold,
EPV budget, baselines). They do not apply to a builder and are not ignored by
accident; the script they apply to is whatever later reads this store.

WHY IT EXISTS
-------------
Eight places in the tree turn text into a number, and essentially every number
was chosen by hand: `EVENT_TYPE_TIER` is 33 tier values plus 6 noise types,
none fitted; `catalyst_confidence` is a model grading its own parsing on a
13-value menu; the `soi_count / 5` term rewards a model for writing more
sentences; the `0.45` floor was moved up from `0.25` after the output looked
wrong. A System One model returns a calibrated probability over an answer space
we define, which is the same job done by an instrument that can be checked
against observed frequencies instead of against our intuition.

This builder does NOT change the live score. `selection_score` is frozen (owner
decision 2026-09-17, "Path A"): no new hand-made terms until an ML score passes
its own registered test. Wiring these columns into `catalyst_strength` would
also bump `catalyst_config_version` and partition the EDGE pool, to improve an
ordering measured as carrying no information. So the destination of this layer
is the feature set of that future ML score, not the formula.

THE BODY CAP IS MEASURED, NOT ASSUMED
-------------------------------------
The vendor documents that accuracy falls as the state grows with content
unrelated to the decision ("context rot"), so the cap is an accuracy question
and the near-zero token price does not settle it. Measured 2026-10-01 on 80
real plus 80 scrambled (article, ticker) pairs, every one an `edgar_press_release`
with a body over 2000 chars (p50 12936, p90 65248, max 139959), scoring
discrimination of real pairs against scrambled ones:

    cap (chars)   touches AUC   gain AUC
          2 000         0.848      0.739
          8 000         0.846      0.767
         32 000         0.847      0.798   <- chosen
        100 000         0.846      0.783

The easy question is flat across a fiftyfold range of input: a headline and the
first paragraph already say what a press release is about. The harder question
gains about 6 AUC points up to 32 000 and then gives some back. Zero refusals at
any cap. 75.9 % of all articles are under 2000 chars, so the cap touches ~21 %
of the store and all of them come from one source, `edgar_press_release`.

Cost of the whole history at this cap: 23835 articles, 28.8M input tokens,
about $1.21. At the old 2000-char cap it would be $0.17.

WHY A VERSION TOKEN
-------------------
`JEV_FEATURE_VERSION` fingerprints the question documents, the body cap and the
model id. Rewording a question changes the number it produces, so rows written
before and after must never pool — the same hazard `catalyst_config_version`
exists to prevent on the scoring side. The token drifts automatically; never
edit it by hand. The model id is pinned to a VERSIONED build rather than the
`jev-latest` alias for the same reason, and the build that actually answered is
recorded per row in `jev_model` so an alias move is visible after the fact.

TWO PASSES, TWO UNITS
---------------------
* `article` — one call per news article, three questions. Unit: `news_id`.
  Replaces what `event_type`, `catalyst_confidence` and the `soi_count` term
  were each reaching for.
* `candidate` — one call per (brief row, ticker), two questions. Unit:
  (`brief_date`, `ticker`). Replaces `llm_confidence`, the mapper's own
  "subjective confidence that this company really stands to gain". The two
  questions are the exact forms measured at AUC 0.808 and 0.745; the company
  name, SIC industry and sector are supplied because a bare ticker scored 0.517,
  which is a coin flip. `rationale` is deliberately NOT supplied: the mapper
  wrote it while deciding this company fits this article, so feeding it back
  would hand the model the answer.

The mapper itself is NOT replaced. Its job is generative — it invents ticker
candidates from world knowledge — and a System One model cannot propose, only
judge. Same for `second_order_implications`, the brief prose and the channel
assessment, which is a System Two task.

USAGE
    .venv/bin/python apps/alphalens-research/scripts/ml/2026_10_jev_feature_layer.py \
        --run --pass article [--dates 2026-05-18:2026-09-30] [--workers 8] [--limit N]

Idempotent: a row already present for its unit under the CURRENT feature version
is not recomputed. A version bump recomputes everything, by design.

Last run: not yet run.
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import hashlib
import json
import logging
import pathlib
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from alphalens_pipeline.data.alt_data.openrouter_client import (
    DEFAULT_SYSTEM_ONE_MODEL,
    OpenRouterClient,
    SystemOneResponse,
)
from alphalens_pipeline.thematic.screening.catalyst_signals import (
    EVENT_TYPE_TIER,
    NOISE_EVENT_TYPES,
)

logger = logging.getLogger("jev_feature_layer")

HOME = pathlib.Path.home() / ".alphalens"
NEWS_DIR = HOME / "thematic_news"
BRIEFS_DIR = HOME / "thematic_briefs"
OUT_ROOT = HOME / "jev_features"

# Measured, see the module docstring. Not a cost decision.
BODY_CHAR_CAP = 32_000
MODEL = DEFAULT_SYSTEM_ONE_MODEL
DEFAULT_WORKERS = 8

# Retry policy. The canonical client deliberately has none: it states that retry and
# throttle are caller concerns, and this builder is the caller. A whole-history run is
# about 24000 calls against a vendor that documents rate limits changing without
# notice, so without a retry a 429 burst leaves holes in the store, and without the
# non-zero exit below the run that produced them still reports success.
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
_MAX_ATTEMPTS = 4
_RETRY_SLEEP_SECONDS = 2.0

# One short clause per option. The vocabulary itself comes from the live
# taxonomy below, so a type added to EVENT_TYPE_TIER without a description here
# fails loudly rather than silently narrowing the answer space.
_EVENT_TYPE_DESCRIPTIONS: dict[str, str] = {
    "m_and_a": "A merger, acquisition, takeover bid, or sale of the company or a business unit.",
    "earnings": "Reported results for a period that has already ended.",
    "guidance": "A forecast of future results, raised, lowered or reaffirmed.",
    "regulatory": "A decision, approval, rule or enforcement action by a government body.",
    "bankruptcy": "A bankruptcy, insolvency or creditor-protection filing.",
    "ipo": "A first listing of shares on a public market.",
    "secondary": "A follow-on sale of shares by a company that is already listed.",
    "spinoff": "A business unit being separated into its own listed company.",
    "restructuring": "A reorganisation of operations, segments or the balance sheet.",
    "activist_position": "An activist investor taking or disclosing a stake and demanding change.",
    "product_launch": "A new product, service or facility being introduced.",
    "product_retirement": "A product, service or facility being discontinued.",
    "contract_award": "A customer order, tender win or supply agreement.",
    "partnership": "A collaboration, joint venture or alliance with another company.",
    "financing": "Raising debt or equity, a credit facility, or a refinancing.",
    "dividend": "A dividend being declared, raised, cut or suspended.",
    "buyback": "A share repurchase programme being announced or changed.",
    "exec_change": "A change of chief executive, chief financial officer or another named officer.",
    "board_change": "A change in who sits on the board.",
    "strike": "Industrial action by employees, or a credible threat of it.",
    "layoffs": "Job cuts or a reduction in the workforce.",
    "litigation": "A lawsuit being filed, or a ruling in one.",
    "settlement": "A legal dispute being settled.",
    "investigation": "An investigation or probe being opened by an authority.",
    "recall": "A product being recalled or withdrawn on safety grounds.",
    "breach": "A data breach, cyber attack or security incident.",
    "analyst": "Analyst commentary that is neither a rating change nor a price target.",
    "rating_change": "An analyst upgrade or downgrade.",
    "price_target": "An analyst price target being raised or lowered.",
    "macro": "An economy-wide figure or development, not specific to one company.",
    "geopolitical": "An international political or military development.",
    "central_bank": "A central bank decision or communication.",
    "other": "A real business development that none of the other options describes.",
    "opinion": "An opinion column or editorial.",
    "lifestyle": "A lifestyle or human-interest feature.",
    "listicle": "A ranked or numbered list article.",
    "promo": "Promotional or marketing copy.",
    "evergreen": "A timeless explainer, not tied to a dated development.",
    "sponsored": "Sponsored or paid content.",
}

# Ordered levels. A Score returns a position that may fall between two of them,
# which is the quantity EVENT_TYPE_TIER was a hand-written proxy for.
_MATERIALITY_LEVELS = [
    "Not market-moving: no effect on any company's revenue, earnings or outlook.",
    "Marginally material: a small or uncertain effect on one company.",
    "Clearly material: a measurable effect on one company's revenue, earnings or outlook.",
    "Transformational: it changes what the company is, or how it should be valued.",
]


def _event_type_criteria() -> dict[str, str]:
    """The Choice options, taken from the LIVE taxonomy, not a second copy.

    Raises when the two disagree. A type added to `EVENT_TYPE_TIER` with no
    description here would otherwise quietly drop out of the answer space, and
    the model would be forced to pick a type that cannot be the right one.
    """
    vocabulary = set(EVENT_TYPE_TIER) | set(NOISE_EVENT_TYPES)
    missing = sorted(vocabulary - set(_EVENT_TYPE_DESCRIPTIONS))
    extra = sorted(set(_EVENT_TYPE_DESCRIPTIONS) - vocabulary)
    if missing or extra:
        raise ValueError(
            f"event-type vocabulary drift: missing descriptions {missing}, "
            f"descriptions for unknown types {extra}"
        )
    return {name: _EVENT_TYPE_DESCRIPTIONS[name] for name in sorted(vocabulary)}


ARTICLE_QUESTIONS: dict[str, dict] = {
    "event_type": {
        "type": "choice",
        "instructions": (
            "Which single option best describes the development reported in "
            "`article_title` and `article_body`?"
        ),
        "criteria": _event_type_criteria(),
    },
    "materiality": {
        "type": "score",
        "instructions": (
            "How material is the development in `article_title` and `article_body` for the "
            "company it is mainly about?"
        ),
        "criteria": _MATERIALITY_LEVELS,
    },
    "concrete_fact": {
        "type": "noul",
        "instructions": (
            "Does `article_body` state a specific corporate development with a date, an amount "
            "or a named counterparty attached?"
        ),
        "criteria": {
            "true": "It names a dated action, an amount, a counterparty, or a quantified effect.",
            "false": (
                "It is commentary, speculation, a forecast with no stated basis, or a general "
                "explainer."
            ),
        },
    },
}

# Both forms exactly as measured on 2026-10-01: touches AUC 0.808, gain 0.745,
# against 0.517 for the gain question when the state carried only a ticker.
CANDIDATE_QUESTIONS: dict[str, dict] = {
    "touches_industry": {
        "type": "noul",
        "instructions": (
            "Does the event in `article_title` and `article_body` affect the line of business "
            "named in `company_industry`?"
        ),
        "criteria": {
            "true": "The event concerns that line of business.",
            "false": "The event concerns a different line of business.",
        },
    },
    "company_gain": {
        "type": "noul",
        "instructions": (
            "Does `company_name`, trading as `ticker`, stand to gain from the event in "
            "`article_title` and `article_body`?"
        ),
        "criteria": {
            "true": "The event plainly improves this company's revenue, earnings or outlook.",
            "false": (
                "The event does not improve this company's prospects, or the link is speculative "
                "or second-hand."
            ),
        },
    },
}

_VERSION_SCHEMA = 1


def jev_feature_version() -> str:
    """Poolability key for this layer.

    Fingerprints the question documents, the body cap and the model id, because
    each of them changes the numbers written. Rows carrying different tokens were
    produced by different instruments and must never pool — the rule
    `catalyst_config_version` already enforces on the scoring side. Drifts
    automatically; never edit by hand.
    """
    config = {
        "schema": _VERSION_SCHEMA,
        "model": MODEL,
        "body_char_cap": BODY_CHAR_CAP,
        "article_questions": ARTICLE_QUESTIONS,
        "candidate_questions": CANDIDATE_QUESTIONS,
    }
    canon = json.dumps(config, sort_keys=True, separators=(",", ":"))
    return f"jev-v{_VERSION_SCHEMA}-{hashlib.sha256(canon.encode()).hexdigest()[:12]}"


# Explicit schemas. An all-refused date would otherwise write null-typed columns
# and break a later pyarrow dataset read across the store (the #1489 lesson).
_ARTICLE_SCHEMA = pa.schema(
    [
        ("date", pa.string()),
        ("news_id", pa.string()),
        ("url", pa.string()),
        ("source", pa.string()),
        ("jev_model", pa.string()),
        ("jev_event_type", pa.string()),
        ("jev_event_type_confidence", pa.float64()),
        ("jev_event_type_probs_json", pa.string()),
        ("jev_materiality", pa.float64()),
        ("jev_materiality_confidence", pa.float64()),
        ("jev_materiality_probs_json", pa.string()),
        ("jev_concrete_fact", pa.float64()),
        ("jev_body_chars_sent", pa.int64()),
        ("jev_cost_usd", pa.float64()),
        ("jev_feature_version", pa.string()),
        ("jev_computed_at", pa.string()),
    ]
)

_CANDIDATE_SCHEMA = pa.schema(
    [
        ("brief_date", pa.string()),
        ("ticker", pa.string()),
        ("news_id", pa.string()),
        ("url", pa.string()),
        ("jev_model", pa.string()),
        ("jev_touches_industry", pa.float64()),
        ("jev_company_gain", pa.float64()),
        ("jev_body_chars_sent", pa.int64()),
        ("jev_cost_usd", pa.float64()),
        ("jev_feature_version", pa.string()),
        ("jev_computed_at", pa.string()),
    ]
)

_cost_lock = threading.Lock()


class _Spend:
    """Thread-safe running total, so the report is the real sum and not a race."""

    def __init__(self) -> None:
        self.usd = 0.0
        self.calls = 0
        self.costed = 0
        self.refused = 0

    def add(self, usd: float | None) -> None:
        """Count the call; add the cost only when one was actually reported.

        `costed` is tracked apart from `calls` so a vendor that stopped returning
        `usage` reads as "N calls, 0 of them costed" rather than as a free run.
        `is not None` rather than a truth test, so a genuine 0.0 is a reported cost
        and not a missing one.
        """
        with _cost_lock:
            self.calls += 1
            if usd is not None:
                self.costed += 1
                self.usd += usd

    def refuse(self) -> None:
        with _cost_lock:
            self.refused += 1


def _probs_json(answer) -> str | None:
    return (
        None if answer.probabilities is None else json.dumps(answer.probabilities, sort_keys=True)
    )


def _ask(
    client: OpenRouterClient, state: dict, questions: dict, spend: _Spend
) -> SystemOneResponse | None:
    """One call, retried on a transient status. Returns None once it gives up.

    Retries only `_RETRYABLE_STATUS`: a 400 means the request itself is wrong, so
    re-sending it spends money to get the same answer back. Fail-soft per row and
    loud in the counters: a batch over tens of thousands of articles must not abort
    because of one of them, and a run that gave up on many must not look clean.
    """
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            out = client.system_one(state=state, questions=questions, model=MODEL)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status in _RETRYABLE_STATUS and attempt < _MAX_ATTEMPTS:
                time.sleep(_RETRY_SLEEP_SECONDS * attempt)
                continue
            spend.refuse()
            logger.warning("gave up after %d attempt(s): HTTP %s", attempt, status)
            return None
        except Exception as exc:  # every failure is ONE skipped row, never an aborted batch
            spend.refuse()
            logger.warning("refused: %s: %s", type(exc).__name__, exc)
            return None
        spend.add(out.cost_usd)
        return out
    return None


def _exit_code(spend: _Spend) -> int:
    """Non-zero when any row was given up on.

    A sparse store that reports success is worse than a failed run: the holes are
    invisible to whatever reads the store next.
    """
    return 0 if spend.refused == 0 else 1


def _dates_in(directory: pathlib.Path) -> list[str]:
    return sorted(pathlib.Path(p).stem for p in glob.glob(str(directory / "*.parquet")))


def _select_dates(available: list[str], span: str | None) -> list[str]:
    if not span:
        return available
    first, _, last = span.partition(":")
    lo = first or available[0]
    hi = last or available[-1]
    return [d for d in available if lo <= d <= hi]


def _read_existing(path: pathlib.Path) -> pd.DataFrame:
    """Read a store file, refusing loudly when it cannot be read.

    Treating a corrupt file as absent would re-send every row for that date to the
    vendor and still report success. A file of ours going unreadable is not a normal
    event, so the operator is told what to do instead of being billed for it.
    """
    try:
        return pd.read_parquet(path)
    except Exception as exc:
        raise RuntimeError(
            f"cannot read {path}: {type(exc).__name__}: {exc}. Treating it as absent would "
            "re-send every row for this date to the vendor. Delete or restore the file, then "
            "re-run."
        ) from exc


def _already_done(out_path: pathlib.Path, key_cols: list[str], version: str) -> set[tuple]:
    """Keys already written for this date UNDER THE CURRENT VERSION.

    A row from an older version is not a hit: it was produced by a different
    instrument, so re-asking is the point of the bump rather than waste.
    """
    if not out_path.exists():
        return set()
    done = _read_existing(out_path)
    if "jev_feature_version" not in done.columns:
        return set()
    done = done[done["jev_feature_version"] == version]
    if done.empty:
        return set()
    # Column-wise, not `iterrows`. The review's suggested `itertuples` form was run
    # and raises: a namedtuple cannot be indexed by column name.
    return set(zip(*[done[col].astype(str) for col in key_cols], strict=True))


def _write(out_path: pathlib.Path, rows: list[dict], schema: pa.Schema, key_cols: list[str]) -> int:
    """Append rows to the date's parquet, last write wins per key.

    Written through a temp file and `os.replace` so an interrupted run leaves the
    previous parquet intact rather than a half-written one.

    SINGLE WRITER PER DATE, assumed and not enforced. This is read-modify-write with
    no coordination, so two concurrent invocations covering the same date would each
    read before the other wrote and the loser's rows would vanish. The consequence is
    bounded: lost rows are simply absent from the done-set next time, so the next run
    recomputes them, a respend rather than a permanent loss. The script is invoked by
    hand; if it ever runs from a timer this needs a lock.
    """
    if not rows:
        return 0
    frame = pd.DataFrame(rows)
    if out_path.exists():
        frame = pd.concat([_read_existing(out_path), frame], ignore_index=True)
    frame = frame.drop_duplicates(subset=[*key_cols, "jev_feature_version"], keep="last")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".parquet.tmp")
    pq.write_table(pa.Table.from_pandas(frame, schema=schema, preserve_index=False), tmp)
    tmp.replace(out_path)
    return len(frame)


def _text(value) -> str:
    """Coerce a stored field to text, with a MISSING value becoming empty.

    `str(value or "")` is wrong here: a pandas missing value is NaN, and NaN is
    truthy, so that idiom yields the literal string "nan" — which the model then
    reads as the content of the article. 20 % of stored articles have no body, so
    this is the common path, not an edge case.
    """
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value)


def article_state(row) -> dict[str, str]:
    """The state for one article. Title plus body, body capped."""
    return {
        "article_title": _text(row.get("title")),
        "article_body": _text(row.get("body"))[:BODY_CHAR_CAP],
    }


def candidate_state(row, article) -> dict[str, str]:
    """The state for one (brief row, ticker) pair.

    Carries the company's IDENTITY — name, SIC industry, sector — because a bare
    ticker scored 0.517 against a scrambled control, a coin flip, while the same
    question with the identity supplied scored 0.808. All three are objective.

    `rationale` is deliberately absent. The mapper wrote it while deciding this
    company fits this article, so feeding it back would hand the model the answer
    and inflate the very discrimination this layer measures.
    """
    return {
        "article_title": _text(article.get("title")),
        "article_body": _text(article.get("body"))[:BODY_CHAR_CAP],
        "ticker": _text(row["ticker"]).upper(),
        "company_name": _text(row.get("company_name")),
        "company_industry": _text(row.get("industry_name")),
        "company_sector": _text(row.get("sector_name")),
    }


def article_row(row, out: SystemOneResponse, *, date: str, version: str, body_chars: int) -> dict:
    """One article's store row.

    Its keys must match `_ARTICLE_SCHEMA` EXACTLY. `pa.Table.from_pandas` raises
    on a key the schema declares and the dict omits, but it silently DROPS a key
    the dict has and the schema does not — so a new field added here without a
    schema entry would be discarded with no error at all. A test pins the parity
    in both directions.
    """
    etype = out.answers.get("event_type")
    mat = out.answers.get("materiality")
    fact = out.answers.get("concrete_fact")
    return {
        "date": date,
        "news_id": _text(row["id"]),
        "url": _text(row.get("url")),
        "source": _text(row.get("source")),
        "jev_model": out.model,
        "jev_event_type": None if etype is None else etype.choice,
        "jev_event_type_confidence": None if etype is None else etype.confidence,
        "jev_event_type_probs_json": None if etype is None else _probs_json(etype),
        "jev_materiality": None if mat is None else mat.score,
        "jev_materiality_confidence": None if mat is None else mat.confidence,
        "jev_materiality_probs_json": None if mat is None else _probs_json(mat),
        "jev_concrete_fact": None if fact is None else fact.noul,
        "jev_body_chars_sent": body_chars,
        "jev_cost_usd": out.cost_usd,
        "jev_feature_version": version,
        "jev_computed_at": _now_iso(),
    }


def candidate_row(
    row, article, out: SystemOneResponse, *, date: str, version: str, body_chars: int
) -> dict:
    """One candidate's store row. Keys must match `_CANDIDATE_SCHEMA` exactly."""
    touches = out.answers.get("touches_industry")
    gain = out.answers.get("company_gain")
    return {
        "brief_date": date,
        "ticker": _text(row["ticker"]).upper(),
        "news_id": _text(article.get("id")),
        "url": _text(article.get("url")),
        "jev_model": out.model,
        "jev_touches_industry": None if touches is None else touches.noul,
        "jev_company_gain": None if gain is None else gain.noul,
        "jev_body_chars_sent": body_chars,
        "jev_cost_usd": out.cost_usd,
        "jev_feature_version": version,
        "jev_computed_at": _now_iso(),
    }


def _now_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def run_article_pass(
    client: OpenRouterClient,
    dates: list[str],
    *,
    workers: int,
    limit: int | None,
    spend: _Spend,
) -> None:
    """One call per article: event type, materiality, and whether a fact is stated."""
    version = jev_feature_version()
    out_dir = OUT_ROOT / "article"
    for date in dates:
        news = pd.read_parquet(NEWS_DIR / f"{date}.parquet")
        out_path = out_dir / f"{date}.parquet"
        done = _already_done(out_path, ["news_id"], version)
        todo = [r for _, r in news.iterrows() if str(r["id"]) not in {k[0] for k in done}]
        if limit is not None:
            todo = todo[:limit]
        if not todo:
            logger.info("%s article: nothing to do (%d already at %s)", date, len(done), version)
            continue

        def one(row, *, date: str = date, version: str = version) -> dict | None:
            state = article_state(row)
            out = _ask(client, state, ARTICLE_QUESTIONS, spend)
            if out is None:
                return None
            return article_row(
                row, out, date=date, version=version, body_chars=len(state["article_body"])
            )

        with ThreadPoolExecutor(max_workers=workers) as pool:
            rows = [r for r in pool.map(one, todo) if r is not None]
        total = _write(out_path, rows, _ARTICLE_SCHEMA, ["news_id"])
        logger.info(
            "%s article: +%d of %d attempted, %d rows in file, $%.4f so far",
            date,
            len(rows),
            len(todo),
            total,
            spend.usd,
        )


def build_url_index(news: pd.DataFrame) -> dict[str, dict]:
    """url -> one article record, as a PLAIN DICT.

    Worker threads read this concurrently. A pandas index builds its hash engine
    lazily on the first lookup, which mutates index internals, so a shared DataFrame
    index raises a question a dict does not. Copying the returned Series, as the
    review suggested, would not have addressed that.

    A duplicated url keeps the LAST record, matching the convention elsewhere in
    these stores that a later write supersedes an earlier one.
    """
    index: dict[str, dict] = {}
    for record in news.to_dict("records"):
        url = record.get("url")
        if isinstance(url, str) and url:
            index[url] = record
    return index


def run_candidate_pass(
    client: OpenRouterClient,
    dates: list[str],
    *,
    workers: int,
    limit: int | None,
    spend: _Spend,
) -> None:
    """One call per (brief row, ticker): does the event touch it, does it gain.

    Needs the article text, so it joins the brief row to `thematic_news` through
    `source_event_url`. A row whose article cannot be recovered is SKIPPED and
    counted, never imputed.
    """
    version = jev_feature_version()
    out_dir = OUT_ROOT / "candidate"
    by_url = build_url_index(
        pd.concat(
            [
                pd.read_parquet(p, columns=["id", "url", "title", "body"])
                for p in sorted(glob.glob(str(NEWS_DIR / "*.parquet")))
            ],
            ignore_index=True,
        )
    )

    for date in dates:
        brief_path = BRIEFS_DIR / f"{date}.parquet"
        if not brief_path.exists():
            continue
        briefs = pd.read_parquet(brief_path)
        needed = {"ticker", "source_event_url", "company_name"}
        if not needed.issubset(briefs.columns):
            logger.info(
                "%s candidate: brief lacks %s, skipped", date, sorted(needed - set(briefs.columns))
            )
            continue
        out_path = out_dir / f"{date}.parquet"
        done = _already_done(out_path, ["brief_date", "ticker"], version)
        todo, unresolved = [], 0
        for _, r in briefs.iterrows():
            if (date, str(r["ticker"]).upper()) in done:
                continue
            url = r.get("source_event_url")
            if not isinstance(url, str) or url not in by_url:
                unresolved += 1
                continue
            todo.append((r, by_url[url]))
        if limit is not None:
            todo = todo[:limit]
        if not todo:
            logger.info(
                "%s candidate: nothing to do (%d at %s, %d unresolved)",
                date,
                len(done),
                version,
                unresolved,
            )
            continue

        def one(pair, *, date: str = date, version: str = version) -> dict | None:
            row, article = pair
            state = candidate_state(row, article)
            out = _ask(client, state, CANDIDATE_QUESTIONS, spend)
            if out is None:
                return None
            return candidate_row(
                row,
                article,
                out,
                date=date,
                version=version,
                body_chars=len(state["article_body"]),
            )

        with ThreadPoolExecutor(max_workers=workers) as pool:
            rows = [r for r in pool.map(one, todo) if r is not None]
        total = _write(out_path, rows, _CANDIDATE_SCHEMA, ["brief_date", "ticker"])
        logger.info(
            "%s candidate: +%d of %d attempted, %d rows in file, %d unresolved, $%.4f so far",
            date,
            len(rows),
            len(todo),
            total,
            unresolved,
            spend.usd,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--run", action="store_true", required=True, help="required; the script spends money"
    )
    parser.add_argument(
        "--pass", dest="which", choices=("article", "candidate", "both"), default="both"
    )
    parser.add_argument("--dates", default=None, help="FROM:TO inclusive, either side may be empty")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument(
        "--limit", type=int, default=None, help="cap rows per date, for a smoke run"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    version = jev_feature_version()
    logger.info("feature version %s | model %s | body cap %d chars", version, MODEL, BODY_CHAR_CAP)

    client = OpenRouterClient.from_env()
    spend = _Spend()
    if args.which in ("article", "both"):
        dates = _select_dates(_dates_in(NEWS_DIR), args.dates)
        logger.info("article pass over %d dates", len(dates))
        run_article_pass(client, dates, workers=args.workers, limit=args.limit, spend=spend)
    if args.which in ("candidate", "both"):
        dates = _select_dates(_dates_in(BRIEFS_DIR), args.dates)
        logger.info("candidate pass over %d dates", len(dates))
        run_candidate_pass(client, dates, workers=args.workers, limit=args.limit, spend=spend)

    logger.info(
        "done: %d calls (%d reported a cost), %d given up on, $%.4f spent, version %s",
        spend.calls,
        spend.costed,
        spend.refused,
        spend.usd,
        version,
    )
    return _exit_code(spend)


if __name__ == "__main__":
    sys.exit(main())
