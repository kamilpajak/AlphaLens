# `docs/research/` — design memos and the gates they must clear

This file loads once Claude reads, writes or edits a file under `docs/research/`, which is exactly
when a design memo is being written or reviewed. It is NOT re-injected after `/compact`.

- **A memo carries a `Status:` line** — `DRAFT` / `LOCKED` / `REJECTED` / `SUPERSEDED` — and you
  update it when reality moves past the memo. After a multi-memo session, scan
  `docs/research/*design*.md` (95 files on 2026-10-14, of which 84 carry the line) and fix the ones
  that drifted. The narrower `v*_design_*.md` glob matches only 13 and is not the right sweep.
- **Before any run over 1h of compute**, the locked memo clears the gates in
  [`methodology-gates.md`](methodology-gates.md): the pre-audit smoke, the Layer 4 overlay
  cyclicality pre-screen, the data-vendor PIT validation, the Little's-Law cost-model audit and the
  true-PIT-universe requirement. Each is a HALT condition, not a suggestion.
- **The adversarial review of a memo comes before the compute, not after.** Your own lens-split
  pass for the design's internal logic, plus Perplexity for the literature it rests on. This
  pipeline has caught fatal flaws in designs that read as sound.
- **A number in a memo is a claim.** Publish it with the construction that produced it and the date
  it was measured, and never carry a figure from another memo without re-deriving it — on
  2026-09-24 four studies shared one upstream count and were wrong together.
- **A finished memo is committed the same day**, through a throwaway docs worktree off fresh
  `origin/main`. An untracked memo in the main checkout rots: the 2026-08-07 pair sat three weeks
  and a later memo had to restate it from memory.
