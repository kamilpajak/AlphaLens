# `docs/research/assets`

Rendered figures for the research memos in `docs/research/`.

## `channel_feature_selection_flow.{mmd,svg,png}`

One mermaid flowchart of how the thematic pipeline selects candidates after the
"channel as a feature, not a gate" change (branch `feature/channel-as-feature`, PR #1066).

**What it shows**

- The vertical spine on the left is the real call order inside
  `alphalens_pipeline/thematic/mapping/orchestrator.py::_rows_for_theme`:
  catalyst resolution, STAGE A proposal, deterministic market-cap bracket, confidence
  sort, assessment cap, STAGE B channel assessment, ordinal median, derived shadow
  verdict, the unchanged press / 10-K / insider gates, the stamped brief row, and the
  parquet plus three sidecars.
- **Purple with a thick dashed border = a call to the LLM. Blue with a solid border =
  deterministic Python.** That split is the point of the redesign, so it is the strongest
  visual contrast on the page.
- The two dark cards on the right hold the **full prompts, verbatim**, exactly as
  `theme_mapper.build_prompt` and `channel_assessor.build_assessment_prompt` produce
  them, each followed by its JSON response schema and its sampling parameters. Nothing is
  abbreviated. They are rendered for one real probe pair (theme `government_funding`, the
  D-Wave / QBTS government-funding event of 2026-05-22, `brief_date` 2026-05-28).
- The other dark cards are real outputs of that same event: the 12 stage-A proposals with
  the model's own confidences, the 12 real market caps and their bracket verdicts, and the
  3 stage-B assessments.
- The green row at the bottom holds three real worked examples of the acceptance probe of
  2026-08-19: a `verified` candidate (OLN), an `unverified` candidate that still ships
  (ASH), and the derived shadow verdict on both sides (a `keep` theme and two `refuse`
  anchors where every candidate was still kept).
- The two amber boxes are the invariants the design turns on: **no market-cap number ever
  enters a prompt**, and **assessment never removes a candidate**.
- The amber card at the very bottom carries the acceptance-probe numbers, including the
  crowd-out repair (27/94 = 28.7% against the frozen strict gate's 14/348 = 4.0%), and the
  caveats. None of those numbers is a performance result; the probe is an engineering gate
  and is not joined to any return series.

**Which code it depicts**

Branch `feature/channel-as-feature`, commit `2605f710`
("docs(research): record the review-fix increment and its pre-registration effects").

The prompt text is valid for a wider window than that single commit: both prompt template
literals are byte-identical between the acceptance-probe build `1a29fe95` and `2605f710`.
`sha256[:12]` of `_PROMPT_TEMPLATE` is `b980490a830e` (8030 bytes) and of
`_ASSESS_PROMPT_TEMPLATE` is `03962f7ddcbe` (5015 bytes) at both commits.

**How to regenerate**

`channel_feature_selection_flow.mmd` is the source. Run both commands from this directory:

```bash
npx -y @mermaid-js/mermaid-cli@11 -c mermaid-config.json \
  -i channel_feature_selection_flow.mmd -o channel_feature_selection_flow.svg \
  -b transparent

npx -y @mermaid-js/mermaid-cli@11 -c mermaid-config.json \
  -i channel_feature_selection_flow.mmd -o channel_feature_selection_flow.png \
  -w 3417 -s 2 -b '#0d1117'
```

`mermaid-config.json` only raises `maxTextSize`; the default of 50000 characters is far
below what two full prompts need, and mermaid refuses to draw anything above it (it
renders a red "Maximum text size in diagram exceeded" box instead of failing). The limit
cannot be raised from an `%%{init}%%` directive or from the frontmatter, so the config
file is required.

`-w 3417` matches the SVG's own width in pixels, so `-s 2` really is 2x. Without `-w`,
mermaid-cli renders into an 800-pixel viewport and the prompt text is scaled down to a few
pixels per line.

**Editing notes**

The layout is `layout: elk` (set in the file's frontmatter). The dagre default sprawls on a
graph this shape and does not keep the spine in one column.

Escaping rules that were established by trial on this material, and that a future edit must
keep:

| Character | Write it as | Why |
|---|---|---|
| `"` | `&quot;` | a raw quote ends the label |
| `<` `>` | `&lt;` `&gt;` | with `htmlLabels: true` they are swallowed as tags |
| `#` | `#35;` | a bare `#` opens an entity code |
| `{` `}` `\|` | literal | numeric entities such as `&#123;` are processed twice and come out as `&{` |
| space | `&nbsp;` | HTML collapses runs of spaces, which destroys the prompt indentation |
| newline | `<br/>` | with `wrappingWidth: 1400`, explicit breaks are honoured exactly |

Both failure modes worth knowing about produce a **successful render of a wrong picture**,
so check the PNG by eye after every change rather than trusting the exit code.
