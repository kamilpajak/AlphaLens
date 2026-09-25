# intent-replay

A research backtester that takes a `TradeIntent` document (the same JSON that
`alphalens broker arm` accepts) plus price bars, and reports what the document
would have done.

It is a research tool. It stamps nothing, charges no multiplicity budget, and
carries no accrued history. Every value it needs is read from the document or
stated in the run configuration; nothing is inherited from a deployment, an
environment variable or a production constant. A missing value is a refusal,
never a default.

## Run configuration

Everything the replay needs beyond the document is stated in one JSON block
(spec section 5.2), parsed by `intent_replay.config.RunConfig.from_jsonable`
and rendered back into the result by `to_jsonable`:

| key | form | meaning |
|---|---|---|
| `entry_deadline` | `{kind, value, unit, source, formula}` | the entry-order cutoff, epoch ms UTC, with the rule that produced it; never null |
| `walk_start` | the same shape | the first timestamp of the walk, with the rule that produced it |
| `entry_trail_bps` | integer >= 1, or `null` | the native entry-trail distance; `null` is trailing OFF |
| `ceiling_price` | number > 0, or `null` | the take-profit cap |
| `time_stop_t` | epoch ms UTC, or `null` | the position time stop |
| `oco` | `false` | v1 models no OCO pair; `true` is refused |
| `costs` | five keys, see the spec | the threshold the take-profit cost gate compares against |

A missing key is `config_incomplete` (`details.keys` names every missing key).
A stated value nothing can use — the wrong type, a non-finite number, a wrong
unit, an unknown key, `oco: true` — is `config_invalid`. Nothing is defaulted.

Design: `docs/superpowers/specs/2026-09-23-intent-replay-design.md`.
Implementation plan: `docs/superpowers/plans/2026-09-25-intent-replay-step1.md`.
