# Brief intent parity fixtures (#1469)

Each file is one real brief row and the document the old `alphalens broker arm` journaled for it,
captured before that command was deleted:

- `brief_trade_setup`: the row's `brief_trade_setup`, read with `paper.brief_loader.load_brief`
  from `~/.alphalens/thematic_briefs/<brief_date>.parquet`.
- `armed_intent`: the `intent` of the one line `broker arm <ticker> --date <brief_date>
  --frame 24000 --currency PLN` appended to `picks.jsonl` under a temporary `HOME`, at `24ace5e9`.

The shapes cover 3 tiers with 3, 2 and 1 take-profits, and 2 tiers with 1.

`test_brief_intent_parity.py` sends the new producer's document for each row through the door and
compares the journaled intent with `armed_intent`, except `meta.armed_ts`. The exit declaration is
read from the live `breakeven_trail` registry entry, so a deliberate change to that entry must
regenerate `armed_intent` here, not loosen the test.
