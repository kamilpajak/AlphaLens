# newsvault

Point-in-time news archive. Two jobs the Python `thematic_news` store cannot do:

1. **Be complete.** The Python ingest caps a day at 200 items because that is the
   LLM budget for briefs, and it overwrites the day's view on every run. Measured on
   the production stores 2026-11-11: the capped view holds 200 items for days whose
   append-only lake holds 287 / 560 / 678 distinct items (2026-07-01 / 07-15 / 08-12),
   so a count taken from the view reads 30-70% of what was fetched, and the truncation
   rate moves day to day. A count is not a brief: the archive keeps everything.

2. **Be honest about when we could have known.** Every item carries two instants -
   when the world published it, and when we could first have seen it. Backfilled
   history cannot claim those are the same, and the type system here does not let it.

Statistics stay in Python. This service owns the archive and the queries over it.
