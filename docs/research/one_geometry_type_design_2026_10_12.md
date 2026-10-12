# Design: one geometry type — the brief emits the unsized trade, and `intent-replay` becomes the only what-if engine

Status: **REJECTED**, 2026-10-12. Epic #1761. The proposal in this memo — splitting `size` out of `TradeSpec` so the brief emits a shared geometry type — was refused by the adversarial review in section 9, on three grounds this memo's own measurements support. Sections 1 to 8 are kept as written, because the survey in them is what made the refusal possible and is reusable by whatever replaces it. **What survives is the destination, not the mechanism**: the brief's geometry should be validated, and the duplicate what-if engine should be retired. Section 9 names the three smaller, orthogonal changes that get both. Code facts come from `24c725b8`; every claim below carries the `file:line` or the command that produced it, and where a fact is missing rather than known the word is **GAP**. Nothing in the repository was modified while this was written.

The proposal in one paragraph. `TradeSpec` has seven fields: six describe the chart and one, `size`, describes the account. Its own docstring calls it `compute_setup_plan`'s UNSIZED input. Split `size` out, leaving an account-free geometry type. The brief then emits that geometry instead of its own parallel `brief_trade_setup` shape; arming composes geometry plus size, instrument and meta into a `TradeIntent` unchanged on the wire it places from; and a what-if states the size as a declared run value. The deprecated `/edge` what-if lenses and their engine are then replaceable by `intent-replay` with no translation layer, because the brief already speaks the engine's language.

This memo does not decide the questions in section 6. Those are the owner's.

---

## 1. Two shapes for one geometry, and only one has a door

### 1.1 `TradeSpec`, field by field

`TradeSpec` is the geometry half of an armed pick. It has seven fields (`apps/alphalens-broker-contract/broker_contract/trade_intent/schema.py:229-266`):

| Field | Type | What it means (quoting the field's own `meaning`) |
|---|---|---|
| `entry_tiers` | `tuple[EntryTierSpec, ...]` | "The entry ladder, rung by rung." (`schema.py:237`) |
| `disaster_stop` | `float` | "Stop price in the instrument's currency; sits below every entry rung." (`schema.py:238-240`) |
| `tp_tranches` | `tuple[TpTrancheSpec, ...]` | "The take-profit ladder. Empty is legitimate: a pick with no take-profit that runs to its disaster stop." (`schema.py:241-244`) |
| `size` | `PickSize` | "How much the pick spends: an account-currency amount for the whole entry ladder." (`schema.py:245-247`) |
| `order_ttl_days` | `int`, default `DEFAULT_ORDER_TTL_DAYS` = 7 | validity window in TRADING days — "sessions of instrument.mic, not calendar days … counted from meta.trade_date … 0 is the 'field absent' value and means the default (7). Allowed range 0..60." (`schema.py:248-256`; `broker_contract/constants.py:30`, `:38`) |
| `side` | `Literal["long"]`, default `"long"` | "Only long entries are armed today. Pinned to one literal as a barrier against the client-side 'Sell'->Buy footgun rather than left open." (`schema.py:257-261`) |
| `schema_version` | `str`, default `SCHEMA_VERSION` = `"3"` | "Wire version of this spec. The same constant as meta.schema_version, not an independent dial." (`schema.py:262-266`, `:122`) |

The three sub-objects that matter to the proposal:

**`EntryTierSpec`** (`schema.py:141-175`) — four fields: `limit_price`, `alloc_pct`, `tag`, `entry_mode`. Its docstring states the percentage convention and, in the same breath, the correspondence this memo is about:

> `alloc_pct` is a PERCENTAGE (0-100), not a fraction; the tiers sum to ~100 across the ladder. This matches `compute_setup_plan`, which sizes each tier as `total_notional * (alloc_pct / 100)` (paper/sizing.py). `tag` mirrors the brief entry tier's free-text label (e.g. "T1"); it carries no sizing semantics.
> — `schema.py:145-150`

`entry_mode` defaults to `"pullback"`: `'"pullback" rests below the market; "immediate" is bought at drain, capped at limit_price.'` (`schema.py:172-175`). The docstring expands it: `"pullback"` is "a resting rung below the market (today's only shape)" and for an immediate tier `limit_price` is "the operator's CAP (max acceptable fill), not a pullback level" (`schema.py:151-156`). The field-level `meaning` on `alloc_pct` repeats the convention so the generated JSON Schema carries it: "PERCENTAGE of the pick's notional, 0-100 — not a fraction. The rungs sum to 100." (`schema.py:163-165`).

**`TpTrancheSpec`** (`schema.py:178-207`) — `price`, `tranche_pct`, `r_multiple`, `tag`. Its docstring makes the correspondence explicit: *"Mirrors the brief TP tranche shape exactly"* (`schema.py:182`). Two asymmetries matter. `tranche_pct` "MAY sum to less than 100: that is a deliberate runner, not an error" (`schema.py:191-194`), where the brief always equal-weights to 100 (`apps/alphalens-pipeline/alphalens_pipeline/thematic/trade_setup/builder.py:208-212`). And `r_multiple` carries `door="derived"` — "Label only … computed by the arming door as (price - planned blend) / (planned blend - disaster stop)" (`schema.py:195-201`).

**`PickSize`** (`schema.py:210-226`) — `notional_acct` and `currency`. Its docstring is the history of the split this proposal continues:

> An amount, not a percent: the sizing frame used to live in the daemon's environment, so the same document sized differently on SIM and LIVE and could change size between arming and draining.
> — `schema.py:213-216`

`notional_acct` is "Budget for the WHOLE entry ladder, in the account currency. Each rung gets notional_acct x alloc_pct / 100." (`schema.py:219-222`); `currency` is an ISO 4217 code and "The daemon refuses a pick whose currency is not its account's." (`schema.py:223-226`).

### 1.2 The contradiction the contract already carries

Two docstrings call the same object unsized while it carries size.

`TradeSpec` itself:

> Formalizes `compute_setup_plan`'s UNSIZED dict input (memo section 2.3).
>
> `size` is an account-currency amount for the whole entry ladder, which `compute_setup_plan` spends as-is (#1467).
> — `schema.py:231-234`

And the `spec` field on `TradeIntent`:

> `spec: TradeSpec = contract_field("The unsized trade: entry ladder, stop, TP ladder, size.")`
> — `schema.py:446`

The second sentence lists `size` as one of the things in the unsized trade. That text is not confined to the source: it is the `description` of `properties.spec` in the published input schema (`apps/alphalens-broker-contract/docs/trade-intent-input-v3.schema.json`, `properties.spec.description` = "The unsized trade: entry ladder, stop, TP ladder, size."), so a third-party producer reads it too.

Six of the seven fields say nothing about an account; one, `size`, says nothing but the account. Being precise about the six: five describe the trade (`entry_tiers`, `disaster_stop`, `tp_tranches`, `order_ttl_days`, `side`) and the sixth, `schema_version`, describes the wire (`schema.py:262-266`). None of the six names a book, a balance or a currency. `PickSize` names both the amount and the currency the account is kept in, and it is the only field a daemon has to check against its own environment (`schema.py:223-226`).

One caveat for the split: `order_ttl_days` is account-free but not self-contained. It counts "sessions of instrument.mic" (`schema.py:248-250`), and `instrument` lives on `TradeIntent`, not on `TradeSpec` (`schema.py:445`). An account-free geometry type still cannot be walked forward without the venue it is paired with.

### 1.3 Field-by-field mapping, brief payload to `TradeSpec`

The brief payload is produced by `TradeSetup.to_dict()` (`apps/alphalens-pipeline/alphalens_pipeline/thematic/trade_setup/model.py:105-126`). Its key order in the quoted VPS payload is exactly that function's, which also means the quote is truncated before the tenth key: `to_dict` always emits `builder_config_version` last (`model.py:125`).

**Top level:**

| Brief key | `TradeSpec` | Verdict |
|---|---|---|
| `schema_version` `"1.1.0"` | `schema_version` `"3"` | Same name, different registry. `"1.1.0"` is `model.py:13`; `"3"` is `schema.py:122`. A document carrying `"1.1.0"` is refused by the version gate (`broker.py:1579-1600`). |
| `status` `"OK"` | — | **No home.** The contract has no status; the brief's other value, `NO_STRUCTURE` (`model.py:16`, `:86-103`), means there is nothing to arm. Its contract equivalent is emitting no document at all. |
| `asof_close` | — | **No home.** Provenance (the reference close the levels were anchored to). It belongs beside the document, or as a declared run value — the `exit_geometry` leaf already reads it out of a brief-shaped `Mapping` rather than off a contract type (`broker_contract/exit_geometry/levels.py:15-17`, `:44`). |
| `atr` | — | **No home on `TradeSpec`.** The contract carries an ATR snapshot only inside a reaction primitive, `ReanchorOnFill.atr` (`schema.py:290-293`), i.e. as an instruction input, not as a geometry fact. |
| `disaster_stop` | `disaster_stop` | Same name, same meaning. Types differ: `float \| None` in the brief (`model.py:75`), plain `float` in the contract, and `validate_intent` refuses a non-positive one (`broker_contract/trade_intent/validate.py:279-285`). |
| `suggested_size_pct` | — (`size`) | **Must be dropped.** It is a percent of BOOK: "Total book exposure % if ALL tiers fill, given a risk budget" (`thematic/trade_setup/sizing.py:56-68`). That is the exact representation #1467 removed from the contract: `'"3": TradeSpec.suggested_size_pct replaced by TradeSpec.size (#1467). A "2" document does NOT decode: its percent needs a frame the daemon no longer has.'` (`schema.py:118-121`). At composition it becomes a `PickSize`. |
| `order_ttl_days` | `order_ttl_days` | Same name, same meaning. The contract adds the 0..60 range and the "0 means the default" rule (`schema.py:248-256`; `validate.py:324-331`). |
| `entry_tiers` | `entry_tiers` | Same name; element shape differs (below). |
| `tp_tranches` | `tp_tranches` | Same name; element shape differs (below). |
| `builder_config_version` | — | **No home.** A poolability token over the geometry CONSTANTS, not a geometry value: "a canonical JSON of every constant that shapes ladder geometry, so two setups built under different constants can never pool silently" (`thematic/trade_setup/config_version.py:1-8`). It belongs with the run record, next to the ladder config string. |

**Entry tier element** (`model.py:29-46`, `EntryTierSpec` at `schema.py:141-175`):

| Brief key | `EntryTierSpec` | Verdict |
|---|---|---|
| `limit` | `limit_price` | **Renamed.** Same meaning (`schema.py:159-162`). |
| `alloc_pct` | `alloc_pct` | Same name, same convention: percent 0-100 summing to 100 on both sides (`thematic/trade_setup/sizing.py:30`, `:53`; `schema.py:163-165`; enforced at `validate.py:244-251` with tolerance `1e-6` at `validate.py:89`). |
| `atr_distance` | — | **No home.** "(close − limit)/ATR (>0 = below close)" (`model.py:31`) — derivable from `asof_close` and `atr`, and a description of the rung rather than an instruction. Belongs with provenance. |
| `tag` | `tag` | **Same name, different content.** The brief's `tag` is a reason phrase: `"swing-low"`, `"shallow pullback"`, `"volatility support"` (`thematic/trade_setup/builder.py:93`, `:98`, `:99`). The contract's `tag` is a rung label, default-filled by the door as `"T<n>"` (`schema.py:166-171`). |
| `label` | (`tag`) | **Renamed into the field above, with a vocabulary clash.** The brief's ordinal is `E<n>` (`model.py:19-21`, stamped at `model.py:117-120`); the door fills `T<n>` (`schema.py:170`). |
| — | `entry_mode` | No brief counterpart; the default `"pullback"` is correct for every brief rung, which sits below the close by construction (`builder.py:93-99`). |

**TP tranche element** (`model.py:49-64`, `TpTrancheSpec` at `schema.py:178-207`):

| Brief key | `TpTrancheSpec` | Verdict |
|---|---|---|
| `target` | `price` | **Renamed.** Same meaning (`schema.py:190`). |
| `tranche_pct` | `tranche_pct` | Same name, same convention; the contract permits a sum under 100, the brief never produces one (`schema.py:191-194` vs `builder.py:208-212`). |
| `r_multiple` | `r_multiple` | **Must be dropped.** `door="derived"` (`schema.py:195-201`); the brief computes its own (`model.py:51`, `thematic/trade_setup/ladder.py:73`). |
| `tag` | `tag` | **Same name, different content.** Brief: `"2R volatility target"` (`ladder.py:99`). Contract: `"TP<n>"` (`schema.py:202-207`). |
| `label` | (`tag`) | **Renamed into the field above.** Brief ordinal `TP<n>` (`model.py:24-26`) — here the two vocabularies agree. |

The door rules that forbid smuggling the "no home" fields in are two, and they are not the one most people assume:

1. **`derived_field_supplied`**, the first gate after parsing. `refuse_derived_fields` reads `supplied_derived_paths(document)` and refuses with "the door computes these; remove them from the document" (`apps/alphalens-pipeline/alphalens_pipeline/brokers/automanager/intent_door.py:116-122`, class at `:60-61`; called at `apps/alphalens-pipeline/alphalens_cli/commands/broker.py:1673-1675`). This is what stops `tp_tranches[].r_multiple`.
2. **`key_discarded`**, the fixed-point gate. Everything else — `status`, `asof_close`, `atr`, `suggested_size_pct`, `builder_config_version`, `atr_distance`, `label` — would be dropped by the decoder with only a log line (`broker_contract/trade_intent/codec.py:192-209`), so the door compares what was sent against what decode+re-render gives back and refuses the difference: "the decoder would discard … — the pick would not carry what you sent" (`broker.py:1717-1724`, using `codec.discarded_paths` at `codec.py:121-129`).

It is **not** the JSON Schema gate. The generated input schema sets no `additionalProperties`: `grep -c additionalProperties` returns 0 both in the generator (`broker_contract/trade_intent/json_schema.py`) and in the published document (`apps/alphalens-broker-contract/docs/trade-intent-input-v3.schema.json`). An extra brief field passes the shape gate and dies two gates later, at the fixed point.

### 1.4 What validates `brief_trade_setup` today

**Written by** one line: `"brief_trade_setup": json.dumps(setup.to_dict())` in the brief orchestrator's row builder (`apps/alphalens-pipeline/alphalens_pipeline/thematic/argumentation/orchestrator.py:723`), where `setup` comes from `trade_setup_builder.build_trade_setup(...)` (`orchestrator.py:807`, builder at `thematic/trade_setup/builder.py:228-252`). Nothing validates the dict between `to_dict()` and the parquet cell.

**Nothing constrains its shape on the way in.** What I searched, and what each search returned:

| Search | Result |
|---|---|
| `grep -rn "brief_trade_setup"` over `*.py *.ts *.svelte *.json` | 30 code hits; every one is a producer (`orchestrator.py:723`), a reader, a docstring, or a test fixture. No writer-side validator. |
| `find . -name "*schema*.json"` (excluding OpenAPI) | three files, all contract-side: `trade-intent-v3.schema.json`, `trade-intent-input-v3.schema.json`, `broker-trades-v1.schema.json`. None for `brief_trade_setup`. |
| `grep -rln jsonschema --include="*.py" apps/` | 14 files: the arming door, the trade-intent generator and its tests, `intent-replay`, the extraction-template YAML schema, dependency gates. No brief/thematic consumer. |
| Django model | `brief_trade_setup = models.JSONField(null=True, blank=True)` with the comment "Consumers must check the JSON's schema_version" (`apps/alphalens-django/briefs/models.py:185-189`). The field accepts any object. |
| Django ingest | `coerce_json_obj` asks only "is it a dict": a dict passes through, a JSON string is parsed, anything else becomes `None` (`apps/alphalens-django/briefs/ingest/coerce.py:107-128`); the column is listed in `_OBJECT_JSON_FIELDS` (`ingest/parquet.py:68`). |
| Django parity test | `briefs/tests/test_schema_parity.py:110` pins that the COLUMN exists. It says nothing about the JSON inside it. |
| Tests under `tests/thematic/trade_setup/` | `test_model.py:54-68` pins the per-element key sets exactly (`limit/alloc_pct/atr_distance/tag` and `target/tranche_pct/r_multiple/tag`). The TOP-LEVEL key set of `to_dict()` is pinned nowhere. |

The nearest things to a gate are both something else:

- `validate_trade_setup` (`apps/alphalens-pipeline/alphalens_pipeline/paper/sizing.py:30-77`) is a READ-side plannability question, run by the population monitor, not by the producer. It checks `status == "OK"`, `schema_version in ("1.0.0", "1.1.0")`, a positive `suggested_size_pct`, a positive `disaster_stop`, and a non-empty entry ladder with at least one `limit > 0`. It never looks at `tp_tranches`, `atr`, `asof_close`, `order_ttl_days`, or any key it does not name — and it runs after the row is already on disk and in Postgres.
- `tests/thematic/test_pre_open_setup.py:46-96` does check real geometry invariants (tiers below the close and descending, allocations summing to 100, stop below every entry, targets above the close and ascending, positive ATR). It checks them over a FROZEN in-repo artefact of 94 recovered setups (`test_pre_open_setup.py:19-27`), not over the briefs the pipeline publishes each day.

The cost of there being no gate is already visible downstream. `apps/web/src/lib/types.ts:300-316` declares the TypeScript `TradeSetup` interface with the comment "mirrors the Python `TradeSetup.to_dict()`" and lists nine fields — `builder_config_version`, added in schema 1.1.0 (`model.py:13`, emitted at `model.py:125`), is missing. The generated OpenAPI types give the field as `brief_trade_setup?: unknown` (`apps/web/src/lib/api-types.gen.ts:380`, `:535`), so nothing catches the drift in either language.

**Against that, the gates a `TradeIntent` passes.** The authoritative order is the `arm` docstring, "GATES, in this order, and none of them is redundant" (`apps/alphalens-pipeline/alphalens_cli/commands/broker.py:1754-1771`), preceded by the parse step at `broker.py:1799`:

| Gate | Where | What it catches |
|---|---|---|
| parse | `_parsed_document`, `broker.py:1536-1548` | `not_json`; and `duplicate_key`, because `json.loads` silently keeps the LAST of a repeated key (`_object_without_duplicate_keys`, `broker.py:1485`) |
| derived | `intent_door.refuse_derived_fields`, `intent_door.py:116-122`, called `broker.py:1673-1675` | a field the door computes was sent: `intent_id`, `meta.armed_ts`, any `r_multiple` |
| shape | `_assert_published_shape`, `broker.py:1560-1576` | the published INPUT JSON Schema, on the wire, via `Draft202012Validator(generate_schema("input"))` |
| version | `_assert_version_is_spoken`, `broker.py:1579-1600` | a stated `meta.schema_version` other than `"3"`; an absent one means the current version |
| venue | `ensure_supported_venue`, `broker.py:1680-1684` | a MIC this deployment does not trade — before anything reads a calendar |
| identity | `intent_door.check_identity_shapes`, `intent_door.py:125-140` | `generation` a real integer ≥ 1 (the schema calls `1.0` an integer), `trade_date` a parsable date |
| the key | `intent_door.complete`, `broker.py:1688-1696` | one armed, unplaced pick per ticker and venue; a spent or already-placed generation; a validity window that has already ended (#1734) — refusals `pick_already_armed` / `pick_not_writable` (`intent_door.py:76-102`) |
| decode | `intent_from_jsonable`, `broker.py:1712-1715` | an undecodable document (`intent_malformed/undecodable`) |
| nothing lost | `discarded_paths`, `broker.py:1717-1724` | every key the client sent that decode+re-render did not give back — the typo'd `limit_pirce` case (`codec.py:121-129`) |
| coherent | `validate_intent`, `broker.py:1803-1808` | allocations sum to 100 with no silent rescaling, duplicate tier prices, at most one immediate tier and it must be first, stop positive and below every real rung, a positive `notional_acct` in a three-letter currency code, `order_ttl_days` in 0..60, and the exit declaration (`validate.py:208-332`) |

The last row is the plain contrast. `validate_intent` refuses an entry ladder whose allocations sum to 99.9 (`validate.py:244-251`) and a stop that sits at or above the lowest rung (`validate.py:290-300`). On the brief side those same two invariants are produced correctly by construction and checked by nothing at write time.

### 1.5 The precedent: `exit_geometry` is already an account-free leaf

`broker_contract/exit_geometry/` holds four modules and 708 lines:

| Module | Contents |
|---|---|
| `levels.py` (223 lines) | pure price math — `ceiling_from_52w_high`, `atr_bracket_levels`, `reanchor_target`, `fractional_giveback_target`. "Stdlib-only (`math` + typing). No I/O, no broker/replay dependency — every function here is a total, side-effect-free mapping from primitive inputs to either a price tuple or `None` on a degenerate / missing input." (`levels.py:1-6`) |
| `policy.py` (317 lines) | `ExitGeometryPolicy` (the numeric parameter bundle), the `ExitPolicy` protocol, `SetupStaticPolicy`, `AtrBracketPolicy`. "An `ExitPolicy` is a PURE price oracle: it proposes exit levels and a reanchor target and NEVER emits a broker Action." (`policy.py:1-4`) |
| `registry.py` (138 lines) | the `(name, version)` table and its resolvers; `resolve_exit_policy` |
| `__init__.py` (30 lines) | `__status__ = "ACTIVE"` and the five re-exports |

Four properties make it the shape to copy for an account-free geometry type.

1. **It names no account and no deployment.** It takes prices, an ATR and multipliers, and returns prices. `policy.py:1-10` draws the line in its own words: the policy proposes, the daemon acts.
2. **The package it sits in has zero third-party dependencies, on purpose.** "INTENTIONALLY zero third-party dependencies — this is the A-tier shared leaf … stdlib-only value types + pure math so it can be pulled out into a standalone published package later without carrying any pipeline/research/broker-vendor dependency along with it." (`apps/alphalens-broker-contract/pyproject.toml:11-16`)
3. **Both sides already consume it without either importing the other.** Pipeline-side: `brokers/automanager/position_manager.py`, `brokers/automanager/control_loop.py`, `feedback/breakeven_lenses.py`, `feedback/ladder_replay.py`. Research-side and `intent-replay`: 16 further files, including `tests/intent_replay/test_no_environment_reads.py`. The leaf's own docstring states that as its reason for existing: "a dependency-free leaf consumed by both `alphalens_pipeline` and `alphalens_research` without carrying any research-side or broker-side dependency along with it" (`exit_geometry/__init__.py:8-11`).
4. **It already reads the brief's shape by structural typing, not by contract type.** `ceiling_from_52w_high(trade_setup: Mapping[str, Any] | None, pct_off_52w_high: float | None)` reads `trade_setup.get("asof_close")` (`levels.py:15-17`, `:44`). The account-free half of the contract is therefore already a consumer of `brief_trade_setup` — through an untyped `Mapping`, which is the translation layer the proposal removes.

One further piece of evidence that the two shapes were meant to correspond, written down as a standing obligation rather than inferred: the execution side keeps its own copy of the brief's ordinal-label helpers, with a note saying so.

> NOTE: `thematic/trade_setup/model.py` (brief generation) already defines `entry_tier_label` / `tp_tranche_label`. That is a DIFFERENT subsystem; importing it here would couple execution to brief generation, so this module keeps its own copy. The two MUST agree on the `E{i+1}` / `TP{i+1}` convention (1-based, ordinal).
> — `apps/alphalens-pipeline/alphalens_pipeline/brokers/automanager/labels.py:10-14`

`human_entry_label` (`labels.py:33-35`) and `entry_tier_label` (`model.py:19-21`) are the same function, written twice, with a comment asking a future reader to keep them equal by hand. That duplication, and the `T<n>` / `E<n>` divergence in §1.3, are what one shared geometry type would make unrepresentable.

**GAP** — I could not establish whether anything outside this repository validates `brief_trade_setup`. Every consumer I found is in-tree. What would settle it: the brief parquet has no reader outside `alphalens-pipeline`, `alphalens-django` and `apps/web`, which a check of the VPS `~/.alphalens/thematic_briefs/` consumers would confirm; I was instructed not to reach the VPS.

**GAP** — I could not establish whether a `brief_trade_setup` cell has ever actually been malformed in production. The claim proved here is that nothing would catch it, not that it has happened. What would settle it: running the §1.4 invariant checks (`test_pre_open_setup.py:46-96`) over all published brief parquets rather than over the 94-setup frozen artefact.

---

## 2. The blast radius, consumer by consumer

### 2.0 The headline number

The epic's claim of four consumers is wrong by about an order of magnitude on one
side and by a factor of four on the other.

| What | Count | How counted |
|---|---|---|
| Production sites that read or write `brief_trade_setup` | **43** (30 modules + 13 research scripts) | enumerated in §2.1 |
| Production modules that name `TradeSpec` or `TradeIntent` | **26** | `grep -rln "TradeSpec\|TradeIntent" --include="*.py"` over `apps/alphalens-pipeline`, `apps/alphalens-broker-contract`, `apps/intent-replay`, `apps/alphalens-research/{alphalens_research,scripts}`, plus `broker_contract/trade_intent/legacy.py`, which names `spec`/`size` without naming either class |
| Of those, modules that read `size` by path or by passed object | **16** | §2.3 |
| Published or committed artefacts that state `size` | **9** | 2 generated JSON Schemas, 1 trades schema, 3 example picks, 2 READMEs, 1 LOCKED spec |
| Test modules that would move with a `size` split | **30** | `grep -rl "spec\.size\|notional_acct\|PickSize" --include="*.py" apps/alphalens-research/tests` |

Two numbers matter more than the totals. **Fourteen production modules name a
literal `spec.size` path**, and **removing `TradeSpec.size` is classified BREAKING
by the repository's own schema-drift gate**, which forces `SCHEMA_VERSION` from
`"3"` to `"4"` (§2.5). The gate is not advisory: it is a CI test that reads the
committed artefact.

### 2.1 Everything that reads or writes `brief_trade_setup`

`brief_trade_setup` is exactly `TradeSetup.to_dict()`
(`apps/alphalens-pipeline/alphalens_pipeline/thematic/trade_setup/model.py:100-124`),
version `"1.1.0"` at `model.py:13`.

**Producer side (6 modules)**

| Site | What it does with the shape | Verdict |
|---|---|---|
| `thematic/trade_setup/model.py:100-124` | defines the shape; `to_dict()` stamps `label` per rung at `:113-120` | **decision** — this is the type being replaced |
| `thematic/trade_setup/builder.py:214-226` | constructs `TradeSetup`; `:204` computes `suggested_size_pct` | **decision** — if geometry is account-free, this call stops belonging here |
| `thematic/trade_setup/sizing.py:56` | `suggested_size_pct(entries, stop, risk_budget_pct, …)`, capped at `_MAX_EXPOSURE_PCT = 25.0` (`:21`) | **decision** — the only account-bound number the brief computes |
| `thematic/trade_setup/ladder.py:18,58` + `levels.py` + `entry_primitives.py` + `config_version.py` | build the rungs, tranches and the `builder_config_version` token | **untouched** — pure geometry already |
| `thematic/argumentation/orchestrator.py:428` (column in `_BRIEF_OWN_COLUMNS`), `:723` (`json.dumps(setup.to_dict())`) | writes the parquet cell | **rename** |
| `alphalens_cli/commands/thematic.py:220,261,264` | `_has_usable_ladder` reads `status` + `entry_tiers` for the publish-completeness telemetry | **rename** if the keys move |

**Store and publish (5 sites)**

| Site | What it does with the shape | Verdict |
|---|---|---|
| `apps/alphalens-django/briefs/models.py:189` | `JSONField(null=True, blank=True)`; the comment at `:188` tells consumers to check `schema_version` | **untouched** — Django stores the blob, never its inner keys |
| `briefs/ingest/parquet.py:68` | `_OBJECT_JSON_FIELDS = {"brief_trade_setup", "brief_template_facts"}` — string-to-object coercion only | **untouched** |
| `briefs/ingest/coerce.py:111` | decodes the JSON string; non-object becomes null | **untouched** |
| `briefs/api/serializers.py:47,58` | `exclude = ("pk",)` — every model field is auto-exposed, including this one | **untouched** |
| `apps/alphalens-django/docs/openapi-parity/django.json:1120,1559` | published as `{"nullable": true}`, nothing more | **untouched** |

This is the one pleasant finding: Django and the OpenAPI artefact are shape-blind.
A geometry change needs no migration and no `spectacular` regeneration.

**SPA (5 sites)**

| Site | What it does with the shape | Verdict |
|---|---|---|
| `apps/web/src/lib/types.ts:306-316` (`TradeSetup`), `:279-290` (`EntryTier`), `TpTranche` below it | hand-written mirror of `to_dict()`; reads `limit`, `target`, `suggested_size_pct`, `asof_close`, `atr`, `atr_distance` | **decision** — see §2.2 |
| `components/TradeSetup.svelte:166` (`tier.limit`), `:186` (`tp.target`), `:85` + `:29` (`suggested_size_pct`), `:63` (`asof_close`), `:67` (`atr`), `:47` (`order_ttl_days`) | renders every one of those keys | **decision** |
| `lib/tradeSetupRisk.ts:74` | `impliedRiskPctOfBook(suggested_size_pct, stopFrac)` — the card's "implied risk of book" number | **decision** — it has no input if size leaves the geometry |
| `components/CandidateCard.svelte:670` | `<TradeSetup setup={c.brief_trade_setup} />` | **rename** only |
| `lib/api-types.gen.ts:380,535` | `brief_trade_setup?: unknown` | **untouched** |

**Pipeline read side (8 modules)**

| Site | What it does with the shape | Verdict |
|---|---|---|
| `feedback/ladder_replay.py:188-232` `parse_ladder` | the canonical reader: requires `status == "OK"`, `disaster_stop`, `entry_tiers[].limit`, `tp_tranches[].target`, optional `atr` | **rename** (`limit`→`limit_price`, `target`→`price`) |
| `feedback/ladder_replay.py:345,443,456` `_with_tp_tranches` / `_with_entry_tiers` / `_with_disaster_stop` | build the nine what-if variants by swapping ONE key of the setup dict and re-replaying | **decision** — these are dict surgery on the brief shape; a typed geometry object makes them `dataclasses.replace`, which is the point of the epic |
| `paper/brief_loader.py:100-126` `_coerce_trade_setup` | str-or-dict best-effort decode, warns at `:118` | **rename** |
| `paper/sizing.py:30` `validate_trade_setup`, `:80` `planned_blended_entry`, `:115` `first_brief_tp_target` | the plannability predicate and two geometry reads; `:49` rejects an unknown `schema_version` | **decision** — `validate_trade_setup` is the brief's only validator, and it is a hand-rolled one |
| `feedback/breakeven_lenses.py:326,358,389,409` | threads the setup dict into each breakeven / ATR-bracket lens | **rename** |
| `feedback/ladder_chart.py:606` | draws the levels | **rename** |
| `feedback/ladder_config.py:45-65` | folds the row's actual `order_ttl_days` into the `ladder_config_version` token | **untouched** — it reads one scalar the geometry keeps |
| `feedback/population_ladder_monitor.py:423-426,1862,3169` | the enumeration predicate (`validate_trade_setup`), the engine cutoffs, and the `{TICKER: trade_setup}` map | **rename** |
| `feedback/pre_open_population.py:68,78` | carries `trade_setup` on the pre-open record | **rename** |

**One shared-leaf reader of the brief shape (1 module)**

`apps/alphalens-broker-contract/broker_contract/exit_geometry/levels.py:16,40,44` —
`ceiling_from_52w_high(trade_setup, pct_off_52w_high)` reads `trade_setup["asof_close"]`.
**This is already a brief-shaped reader living inside the shared contract package.**
`asof_close` has no `TradeSpec` counterpart (§2.2), so an account-free geometry
type either keeps `asof_close` or this function loses its input. **Decision.**

**Research lab (5 modules + 13 scripts)**

| Site | What it does with the shape | Verdict |
|---|---|---|
| `diagnostics/exit_policy_replay.py:133,141,160,187,194,232,239,247,387,400,416,570,581,589` | the retired what-if engine; calls `parse_ladder`, `planned_blended_entry`, `ceiling_from_52w_high`, `first_brief_tp_target` | **decision** — this is the code the epic proposes to retire |
| `diagnostics/fill_partition.py:53,230,287` | `entry_tiers_from_setup` | **rename** |
| `diagnostics/exit_policy_missingness.py:72,83,92` | `parse_ladder` + `replay_ladder` | **rename** |
| `diagnostics/edge_stores.py:35,44` | `(brief_date, TICKER) → decoded setup` via `brief_loader._coerce_trade_setup` | **rename** |
| `diagnostics/exit_policy_analysis.py:44` | carries a `trade_setup_json` column | **rename** |
| 13 scripts: `build_pre_open_setups.py:61,115`, `verify_pre_open_setups.py:112`, `replay_bracket_arms.py:151-152`, `whatif_trailing_tp.py:415,425`, `whatif_trailing_entry.py:181,183`, `exit_policy_analysis.py:110,124,136`, `measure_fill_partition.py:110,119`, `diagnose_exit_geometry.py:97,101`, `diagnose_nofill.py:34`, `backfill_breakeven_whatif.py:81,85`, `backfill_ttl_lens_whatif.py:98,102`, `exit_policy_planning_sd.py:122`, `exit_policy_missingness.py:57` | read the parquet column directly, three of them by sniffing the column name (`next((c for c in brief.columns if "trade_setup" in c), None)`) | **rename**, except the three sniffers, which survive a rename silently and will then read the WRONG column if a second `*trade_setup*` column ever exists |

**Test and fixture surfaces (not counted in the 43):** about 30 test modules, four
SPA api-mock day fixtures (`apps/web/tests/fixtures/api-mock/days/{2023-01-23,
2026-04-14,2026-05-18,2026-09-05}.json`), two Storybook story files
(`TradeSetup.stories.svelte:22,36`, `CandidateCard.stories.svelte:126`), the golden
projection (`tests/golden/fixtures/brief_day/golden/projection.json`), and the
Django parity list (`briefs/tests/test_schema_parity.py:110`).

### 2.2 The brief does not speak the engine's language yet

The epic's premise — "the brief already speaks the engine's language" — is not true
of the field names. Read side by side (`model.py:30-124` against
`broker_contract/trade_intent/schema.py:142-255`):

| Concept | `brief_trade_setup` | `TradeSpec` | Status |
|---|---|---|---|
| entry price | `entry_tiers[].limit` | `entry_tiers[].limit_price` | **rename** |
| take-profit price | `tp_tranches[].target` | `tp_tranches[].price` | **rename** |
| entry weight | `alloc_pct` | `alloc_pct` | same name, same 0-100 unit |
| tranche weight | `tranche_pct` | `tranche_pct` | same |
| rung label | `tag` + `label` (`label` stamped in `to_dict`) | `tag` only | brief has one extra |
| R label | `r_multiple` | `r_multiple` (door-derived, `schema.py:170-175`) | same name, different owner |
| stop | `disaster_stop` | `disaster_stop` | same |
| TTL | `order_ttl_days` | `order_ttl_days` | same |
| version | `schema_version: "1.1.0"` | `schema_version: "3"` | two independent dials |
| **brief-only, no `TradeSpec` home** | `status`, `asof_close`, `atr`, `atr_distance`, `builder_config_version`, `suggested_size_pct` | — | **decision** for each |
| **`TradeSpec`-only** | — | `entry_mode` (`schema.py:134-141`), `side`, `size` | brief emits no `entry_mode` |

So a unified geometry type is two renames plus six fields that need a ruling each.
`asof_close` and `atr` are not decoration: `exit_geometry/levels.py:44` reads
`asof_close`, `parse_ladder` reads `atr` (`ladder_replay.py:215`), and
`ladder_replay.py:1622-1624` calls `arm_disaster_stop(fill_price, atr, close)`.

Note also that the project already carries the SAME function twice, once per
shape, in one file: `paper/sizing.py:80` `planned_blended_entry(setup_dict)` and
`broker_contract/sizing.py:79` `planned_blended_entry_from_spec(spec)`, sharing
`_blend_priced_tiers` and documented as mirrors at `sizing.py:82-93`. That pair is
the cost of the split the epic wants to end, measured.

### 2.3 Everything that constructs or consumes `TradeSpec` / `TradeIntent`

**Nothing in production constructs a `TradeSpec` except the codec.**
`grep -rn "TradeSpec(\|EntryTierSpec(\|TpTrancheSpec(\|PickSize(" --include="*.py"`
outside `tests/` returns exactly `codec.py:246,250,259` and two docstring
mentions. Every real document is hand-authored JSON from
`apps/alphalens-broker-contract/examples/manual-pick/` (per `CLAUDE.md`, #1552).
That is the single best fact in this section: the producer side of `TradeSpec` has
one door and one decoder, not many.

| Site | What it does with the shape | Verdict |
|---|---|---|
| `broker_contract/trade_intent/schema.py:211-227` (`PickSize`), `:230-268` (`TradeSpec`, `size` at `:245`) | the type being split | **decision** |
| `broker_contract/trade_intent/codec.py:243-259` `_decode_spec` | generic for the leaves, but `size` is a **hard-coded branch** at `:256-258` | **rename** (one edit, must move with the field) |
| `codec.py:84-105` `author_jsonable`, `:192-209` `_filtered` | both walk `dataclasses.fields` | **untouched** |
| `codec.py:117-143` `discarded_paths` (the fixed point) | path-generic | **untouched** |
| `codec.py:146-168` `supplied_derived_paths` | hand list of `intent_id`, `meta.armed_ts`, `spec.tp_tranches[].r_multiple` — does not mention `size` | **untouched** |
| `broker_contract/trade_intent/json_schema.py:188-200` `_define`, `:225-250` `_as_input` | generate both schemas from the dataclasses | **untouched** code, **regenerated** artefacts |
| `broker_contract/trade_intent/validate.py:156-168` `_numeric_fields` | **hand list**; `size.notional_acct` at `:163` | **rename** |
| `validate.py:301-320` | `size_notional_not_positive` + `size_currency_invalid` | **rename** (see §2.4) |
| `broker_contract/trade_intent/legacy.py:77-79` `_carries_size_pct` | `"suggested_size_pct" in spec and "size" not in spec` | **decision** — after a split, `"size" not in spec` is true of every new document; the predicate stays correct only because it also requires `suggested_size_pct` |
| `broker_contract/sizing.py:280` `compute_setup_plan` | `total_notional = float(spec.size.notional_acct)`; `SetupPlan` docstring at `:160-168` | **rename** |
| `broker_contract/constants.py:18-19` | `DEFAULT_ORDER_TTL_DAYS`, comment names `brief_trade_setup` | **untouched** |
| `alphalens_cli/commands/broker.py:1624-1654` `_echo_armed_intent` | `intent.spec.size` for the dry-run render | **rename** |
| `broker.py:1579-1600` `_assert_version_is_spoken` | refuses a stated version other than `SCHEMA_VERSION`; `:1586` states that a `"2"` document "has no `spec.size`" | **decision** — the comment becomes false after a split |
| `broker.py:1658+` `_gated_completion` | the published gate order | **untouched** |
| `automanager/intent_door.py:416-419` `tier_amounts` | `intent.spec.size.notional_acct × alloc_pct / 100` | **rename** |
| `automanager/intent_door.py:398-413` (r-multiple derivation) | geometry only | **untouched** |
| `automanager/control_loop.py:6088-6090` | passes `spec.size` to the money gate; `:674,:682` alert text names `spec.size` | **rename** |
| `automanager/pick_money_gates.py:70-109` `_check_pick_size` | takes `size: Any` as a **parameter**, reads `size.currency` (`:85`) and `size.notional_acct` (`:104`) | **untouched** if the caller keeps passing the object |
| `automanager/picks.py:505-512,529-534,535-543` | the journal reader: skips a percent-sized line (`LEGACY(size_pct_v2)`), and `iter_legacy_size_pct_picks` surfaces them for the one-time refusal | **decision** — needs a second legacy allowance (§2.6) |
| `automanager/trades.py:243` `SIZE_SHAPES`, `:814-817` `_plan_size_shape`, `:3011` (`EXCLUDE_LEGACY_PLAN`) | classifies a record's plan as `by_amount` / `by_percent` through the legacy predicate | **decision** — a schema-4 plan is neither shape today |
| `automanager/trades_schema.py:353-362` + `apps/alphalens-broker-contract/docs/broker-trades-v1.schema.json:1281` | the trades record embeds "the last armed `TradeIntent` document, verbatim" and names `trade-intent-v3.schema.json` | **rename** — the filename reference moves on a version bump |
| `automanager/{pick_window.py:81, service.py:167, stop_journal.py:831, trade_alerts.py:62}`, `brokers/journal.py:88` | name the type, never `size` | **untouched** |
| `intent-replay/door.py:58,65,118,175-178` | decodes through the contract codec | **untouched** |
| `intent-replay/classification.py:73-85` `CONTAINERS` (includes `"spec.size"` at `:77`) | the exhaustive path census; **a hand list pinned to the schema AND to the LOCKED spec** | **decision** — three places move in lockstep (§2.4) |
| `intent-replay/interpreter.py:382-383` | `reader.number("spec.size.notional_acct")`, `reader.text("spec.size.currency")`; `:345-352` `_check_one_account` | **rename** |
| `intent-replay/envelope.py:204,289` | `currency=intent.spec.size.currency` | **rename** |
| `intent-replay/cli.py:614,638` | derives `account_currency` from `admitted.intent.spec.size.currency` | **rename** |
| `intent-replay/config.py:70,267,524` | docstrings naming the path | **rename** |
| `intent-replay/{measures.py:40-41,65,133, cost_gate.py:15-27,54-80, walk.py}` | consume the notional as a number, never by path | **untouched** |
| `apps/alphalens-research/scripts/replay_from_pick.py:218` | `account_currency = spec["size"]["currency"]`, read off a `broker trades` record | **rename**, and see §2.6 |
| `docs/superpowers/specs/2026-09-23-intent-replay-design.md:554-555,585-587,1151,1182,1434,1445,1465` | the LOCKED spec's path-classification tables, with three rows for `spec.size`, `spec.size.currency`, `spec.size.notional_acct` | **decision** — a LOCKED spec changing |
| `apps/alphalens-broker-contract/README.md:264-284,312,482,666,784` + `apps/intent-replay/README.md:99` + `deploy/systemd/README.md:2076` + repo `CLAUDE.md:172` | published prose stating the path | **rename** |
| `examples/manual-pick/{immediate-plus-pullback,pullback-trailing-stop,pullback-two-tiers}.json:10` | each states `"size": {"notional_acct": 1500.0, "currency": "EUR"}` | **rename**; they are executed by `tests/brokers/automanager/test_manual_pick_templates.py:31` and read by five `intent_replay` test modules |

### 2.4 Which gates read `size`?

Seven, in four layers. None of them is in the arming door's gate chain except
through `validate_intent`.

| Gate | Site | What it reads | Where it would read it after a split |
|---|---|---|---|
| JSON Schema, both shapes | generated; committed at `docs/trade-intent-v3.schema.json`, `TradeSpec.required = ["entry_tiers","disaster_stop","tp_tranches","size"]` | requires `spec.size`, requires `PickSize.{notional_acct,currency}` | wherever the dataclass puts it; regenerated, not edited |
| `validate_intent` finiteness | `validate.py:163` inside `_numeric_fields` | `size.notional_acct` | a hand list that must be edited |
| `validate_intent` amount | `validate.py:301-307` → `size_notional_not_positive` | `spec.size.notional_acct <= 0` | same |
| `validate_intent` currency shape | `validate.py:312-319` → `size_currency_invalid`, three uppercase letters only | `spec.size.currency` | same |
| **Drain, currency** | `pick_money_gates.py:85-89` via `control_loop.py:6088-6090` | `size.currency != account_currency` → terminal refusal | `_check_pick_size` takes `size` as a parameter, so only the ONE call site at `control_loop.py:6089` moves |
| **Drain, boot rail** | `pick_money_gates.py:104-109` | `size.notional_acct > ALPHALENS_BROKER_MAX_PICK_NOTIONAL` (`MAX_PICK_NOTIONAL_ENV`) | same one call site |
| Sizing | `broker_contract/sizing.py:280` `compute_setup_plan` | `spec.size.notional_acct` → `total_notional` | needs the composed intent, not the geometry |
| Journal reader | `picks.py:505,529-534` via `legacy.py:79` | `"size" not in spec` | §2.6 |
| Replay interpreter | `interpreter.py:382-383`, `envelope.py:289`, `cli.py:638` | both `size` fields | the new path |

The two drain gates are the cheapest part of the whole change: `_check_pick_size`
already takes `size` as an opaque parameter (`pick_money_gates.py:70`), so the
only edit is the one argument expression at `control_loop.py:6089`. The gate order
that `CLAUDE.md` calls load-bearing is unaffected, because the size gate sits
BEFORE the day-1 gate and after the window check (`control_loop.py:6061-6100`),
and none of that ordering depends on where in the document the amount lives.

### 2.5 Does anything iterate `TradeSpec`'s fields generically?

Yes — and the generic walkers are the ones that survive. The ones that need
lockstep editing are the **hand lists**.

**Generic, survive a field move untouched**

| Site | Mechanism |
|---|---|
| `codec.py:99` `author_jsonable` | `dataclasses.fields(value)`, drops `door == "derived"` at every level |
| `codec.py:201` `_filtered` | `{f.name for f in dataclasses.fields(cls)}` |
| `json_schema.py:193` `_define`, `:229` `_as_input` | `dataclasses.fields(cls)`; `required` is derived from "has no default" at `:195-196` |
| `intent-replay/trace.py:229` | "ONE function over `dataclasses.fields` rather than a method per kind" |
| `tests/trade_intent/test_contract_metadata.py:55,118,195` | sweeps every contract dataclass for `meaning`, `json_schema`, `door` |
| `tests/trade_intent/test_codec.py:540`, `tests/intent_replay/test_config.py:279` | field sweeps |

**Hand lists that notice a removal (edit in lockstep)**

| Site | What it lists | Why it notices |
|---|---|---|
| `validate.py:156-168` `_numeric_fields` | every float the rules compare, `size.notional_acct` at `:163` | hand-written yield list |
| `intent-replay/classification.py:73-85` `CONTAINERS` | `"spec.size"` at `:77` | pinned twice: `tests/intent_replay/test_classification.py:374` compares it to `container_paths(generate_schema("input"))`, and `:372` compares it to the LOCKED spec's own table via `spec_paths_classed("container")` |
| `intent-replay/classification.py` interpreted set, via `interpreter.py:382-383` | `check_classified(document, read)` requires EVERY input-schema path to be in exactly one class | a path that exists and is read nowhere raises `PathUnclassifiedError` (`classification.py:68`) |
| `docs/superpowers/specs/2026-09-23-intent-replay-design.md:585-587` | three `spec.size*` rows | the test above reads the markdown |
| `trades.py:243` `SIZE_SHAPES = ("by_amount","by_percent")` | the trades record's plan vocabulary | published in `broker-trades-v1.schema.json:1302` |
| `tests/trade_intent/test_contract_metadata.py:180-189` `EXPECTED` door-role map | the eight fields with a `door` role; `size` is NOT among them | so a pure MOVE does not touch it — only a role change would |

So the lockstep set is five code lists plus one LOCKED markdown document. That is
the real cost of the "generated, not hand-written" claim: the schema is generated,
but the classification census that consumes it is not.

### 2.6 What does the published JSON schema look like, and is it generated?

**Generated, from the dataclasses, by
`python -m broker_contract.trade_intent.json_schema --write`**
(`json_schema.py:39-41`, `main()` at `:291-305`). Two artefacts, both committed:

| Artefact | `$id` | `TradeSpec.required` |
|---|---|---|
| `apps/alphalens-broker-contract/docs/trade-intent-v3.schema.json` | `urn:alphalens:contract:trade-intent:3` | `["entry_tiers","disaster_stop","tp_tranches","size"]` |
| `apps/alphalens-broker-contract/docs/trade-intent-input-v3.schema.json` | `urn:alphalens:contract:trade-intent-input:3` | the same four |

Four CI gates sit on them, all in `tests/trade_intent/test_json_schema.py`:
`test_the_artefact_for_the_current_version_exists` (`:751`),
`test_it_matches_a_fresh_generation` (`:757`),
`test_the_file_is_byte_identical_to_the_renderer` (`:775`),
`test_the_readme_names_the_current_artefacts` (`:783`).

**The split is one edit plus one regeneration — and a forced major-version bump.**
`test_it_matches_a_fresh_generation` does not merely diff; it runs
`breaking_changes(committed, live)` (`:591-627`), which emits
`"<Name>.<field>: field removed or renamed"` for any property the committed
artefact has and the live one does not (`:613-616`), and
`"<Name>: definition removed"` for a dropped `$def` (`:609-611`). Removing
`TradeSpec.size` produces the first; moving `PickSize` out of the reachable graph
produces the second. The test's own failure message is explicit: *"Bump
SCHEMA_VERSION … rather than recommitting this file."*

The cascade of `SCHEMA_VERSION = "3"` → `"4"` (`schema.py:122`):

1. New filenames `trade-intent-v4.schema.json` and
   `trade-intent-input-v4.schema.json` (`json_schema.py:68-69`), with the v3 pair
   frozen beside them.
2. `apps/alphalens-broker-contract/README.md` must name the new filenames or
   `test_the_readme_names_the_current_artefacts` reddens.
3. `_assert_version_is_spoken` (`broker.py:1579-1600`) starts refusing any
   document that states `"schema_version": "3"` with
   `schema_version_unsupported`. Documents that omit the key keep arming.
4. The codec and `validate_intent` stay version-blind by design
   (`json_schema.py:206-214`), so the **journal drain is not version-gated** —
   which is the risk, not the relief, below.

### 2.7 Three things I would push back on

**The journal drain will stop reading live schema-3 picks unless a legacy
allowance is written in the same commit.** `_decode_spec` passes the mapping's
keys through `_filtered` (`codec.py:192-209`), which DROPS an unmodelled key with
only a WARNING. If `size` moves to a required field with no default anywhere else
on the document, a schema-3 journal line loses its amount and the constructor
raises, which `intent_from_jsonable` wraps as `TradeIntentDecodeError`
(`codec.py:290-293`) and `iter_picks` skips. Journals are append-only and never
rewritten (`README.md:264`, `legacy.py:131-133`), so every already-placed pick
would silently leave the drain's view. The precedent is `size_pct_v2` itself: a
named entry in `legacy.py:123-135`, a `# LEGACY(size_pct_v2)` marker at
`picks.py:506`, a one-time refusal path (`iter_legacy_size_pct_picks`,
`picks.py:535`), and a bidirectional gate
(`tests/trade_intent/test_legacy_register.py`). A `size` split needs the same
four pieces, and `legacy.py:77-79` `_carries_size_pct` has to be re-expressed —
its current `"size" not in spec` test becomes true of every new document.

**`broker trades` and `replay_from_pick` read the plan by path, not through the
codec.** `trades.py:838` reads `spec.get("side", …)` off the raw plan dict,
`_plan_size_shape` (`:814`) classifies it, and
`apps/alphalens-research/scripts/replay_from_pick.py:218` does
`spec["size"]["currency"]` on the record. The trades record embeds the document
verbatim (`trades_schema.py:353-356`), so after a split it carries two document
shapes and these three sites must handle both. That is where the epic's "no
translation layer" promise is most exposed: the replay join exists precisely
because reconstructing run values by hand got three of them wrong
(`CLAUDE.md`, #1714), and this is the same class of risk.

**"The brief already speaks the engine's language" is a goal, not a measurement.**
Two field renames (`limit`→`limit_price`, `target`→`price`), six brief-only fields
with no `TradeSpec` home, one `TradeSpec`-only field (`entry_mode`) the brief never
emits, and two independent version dials (`"1.1.0"` against `"3"`). A reviewer will
ask which side renames, and the answer decides whether the SPA
(`TradeSetup.svelte:166,186`) and the nine what-if variants
(`ladder_replay.py:414-435,596,642,869`) move with it.

---

## 3. The versioning path, and what each gate does with it

### 3.1 How the version is declared, and who is allowed to read it

One constant declares the wire version, and it carries its own changelog:

```python
# "2": EntryTierSpec.entry_mode added (#1247 immediate-entry tiers). Old "1"
# payloads decode unchanged (absent key -> the dataclass default applies).
# "3": TradeSpec.suggested_size_pct replaced by TradeSpec.size (#1467). A "2"
# document does NOT decode: its percent needs a frame the daemon no longer has.
SCHEMA_VERSION = "3"
```

(`broker_contract/trade_intent/schema.py:114-122`.) Two fields stamp it, and the
field metadata says outright that they are not two dials:

| field | default | what its `meaning` says |
|---|---|---|
| `IntentMeta.schema_version` | `SCHEMA_VERSION` | "Wire version of the document. A consumer reads this one." (`schema.py:396-398`) |
| `TradeSpec.schema_version` | `SCHEMA_VERSION` | "The same constant as meta.schema_version, not an independent dial." (`schema.py:262-266`) |

The contract README repeats it in prose: "`spec.schema_version` is the same
constant duplicated in a second class, not an independent dial"
(`apps/alphalens-broker-contract/README.md:253-255`).

**Exactly one gate reads it.** `_assert_version_is_spoken` in
`apps/alphalens-pipeline/alphalens_cli/commands/broker.py:1579-1600` imports
`SCHEMA_VERSION` (line 1590) and refuses a stated, different version with
`intent_malformed` / `schema_version_unsupported`. An absent key is the current
version, not an unknown one — the test is `declared is not None and declared !=
SCHEMA_VERSION` (line 1594).

**Everything else is version-blind, on purpose.** The generated schema says so in
its own published description:

> NOTE: this schema does not constrain schema_version and neither does the codec
> — the DOOR does (broker arm refuses a stated version other than its own), while
> the journal drain stays ungated so older documents keep decoding there.

(`broker_contract/trade_intent/json_schema.py:203-212`, repeated at
`README.md:261-266`.) That decision is pinned as behaviour, not as prose:
`test_neither_the_schema_nor_the_codec_reads_the_schema_version` pushes `"1"`,
`"2"`, `"99"` and `"not-a-version"` through both layers and asserts both accept
(`apps/alphalens-research/tests/trade_intent/test_json_schema.py:509-525`). The
replay's own door has no version reason in its vocabulary at all
(`apps/intent-replay/intent_replay/door.py:82-94`), and its path classifier files
both version paths under "the door is the only gate that reads a version"
(`apps/intent-replay/intent_replay/classification.py:105-106`).

**What that bounds for this split.** Version-blindness is what lets an *additive*
version decode in both the door and the drain. It is not a promise that an old
line decodes — `README.md:263-265` states the opposite. And the split is not
additive. The CI classifier `breaking_changes`
(`test_json_schema.py:592-626`) would report two entries for a move of `size`
out of `TradeSpec`:

| diff entry | classifier rule |
|---|---|
| `TradeSpec.size: field removed or renamed` | `test_json_schema.py:614-617` |
| `TradeIntent.size: newly required` | `test_json_schema.py:618-621` |

The gate that consumes that list refuses to let the artefact be recommitted:
"the contract changed in a way that breaks a consumer pinned to v3. Bump
SCHEMA_VERSION (which creates a new artefact and freezes this one) rather than
recommitting this file" (`test_json_schema.py:761-776`). So **a bump to `"4"` is
forced by the existing gate, not a choice.** The artefact filenames carry the
version (`json_schema.py:66-72`), so the bump writes
`trade-intent-v4.schema.json` and `trade-intent-input-v4.schema.json` beside the
frozen v3 pair, and the README must name the new pair
(`test_json_schema.py:788-793`).

### 3.2 The legacy register, and the one precedent that is `size`

`broker_contract/trade_intent/legacy.py` holds four entries today
(`legacy.py:90-153`). Each states what it is, why, and the observation that
retires it — "Not a date: a date is a wish, and a wish cannot be checked"
(`legacy.py:11-13`).

| key | what is kept | observation that retires it |
|---|---|---|
| `brief_date_key` | the codec renames `meta.brief_date` to `meta.trade_date` while decoding | no journal line carries `meta.brief_date` (`legacy.py:99-102`) |
| `reanchor_ceiling_price` | the field, its schema node, and the `ceiling_price_unsupported` refusal | no line carries a non-null `ceiling_price`; removal is then breaking and bumps `SCHEMA_VERSION` (`legacy.py:114-120`) |
| `size_pct_v2` | the picks reader recognises a percent-sized document, skips it without decoding, and the drain refuses an unplaced one once | no line carries `spec.suggested_size_pct` (`legacy.py:131-134`) |
| `source_brief` | `meta.source: "brief"`, its default, the `trade_date_required` rule and the brief day-1 anchor | no line has `meta.source` absent or equal to `"brief"` (`legacy.py:147-150`) |

The register's measured counts are quoted with their dates (2026-09-11,
2026-09-24) and must not be reused as current numbers. The census is a command:
`RetirementCensusOnARealJournal`, opt-in on `ALPHALENS_PICKS_JSONL`
(`apps/alphalens-research/tests/trade_intent/test_legacy_register.py:171-193`).
The gate is bidirectional — a registered entry with no `# LEGACY(<key>)` marker
in the tree is red, and a marker naming no entry is red
(`test_legacy_register.py:78-98`); a blank field raises at construction
(`legacy.py:53-59`).

**`size` has migrated once, and the entry records a refusal to migrate.**
`size_pct_v2`'s predicate is both-sided:

```python
def _carries_size_pct(document: Mapping[str, Any]) -> bool:
    spec = document.get("spec")
    return isinstance(spec, Mapping) and "suggested_size_pct" in spec and "size" not in spec
```

(`legacy.py:77-79`.) Its reason is that the old value cannot be converted: "A
percent needs the frame the daemon used to read from its environment, so an old
line cannot be sized correctly and **must not decode**" (`legacy.py:127-129`).
What the reader does with such a line, site by site:

| site | behaviour |
|---|---|
| `brokers/automanager/picks.py:505-516` | `iter_picks` skips it, logging at DEBUG not WARNING, because the daemon re-reads the journal every ~45 s |
| `picks.py:535-544` | `iter_legacy_size_pct_picks` yields it undecoded, so an armed-but-unplaced one is not invisible to the drain |
| `brokers/automanager/control_loop.py:649-662` | the drain refuses it once, with a special message when only its "now" tranche was placed |
| `brokers/automanager/trades.py:776-777, 813-817` | the trades record keeps it undecoded and nulls only its size-derived fields; `_plan_size_shape` answers `"by_percent"` |

That last row is the one with teeth for this split: `SIZE_SHAPES = ("by_amount",
"by_percent")` (`trades.py:243`) is a closed enum published in the trades schema
(`brokers/automanager/trades_schema.py:361`).

**The matching precedent for a relocation is `brief_date_key`, not
`size_pct_v2`.** A percent is unconvertible; a move of `size` from
`spec.size` to a sibling is lossless — the same two numbers at a new path. The
codec already does exactly that kind of migration:

```python
if "brief_date" in meta_map:
    # LEGACY(brief_date_key) ...
    meta_map.setdefault("trade_date", meta_map.pop("brief_date"))
```

(`broker_contract/trade_intent/codec.py:276-283`.) And its entry records the
other half of the pattern: "The published JSON Schema deliberately does NOT know
the old key: the drain reads history, the door does not" (`legacy.py:96-98`).

### 3.3 Each gate of the door, in the order the door runs them

The order is `_parsed_document` (`broker.py:1536-1548`), then `_gated_completion`
(`broker.py:1657-1699`), then `_decoded_intent` (`broker.py:1702-1725`), then
`validate_intent` (`broker.py:1803-1808`); the published table is
`README.md:342-352`.

| # | gate | site | effect of the split |
|---|---|---|---|
| 1 | parse (`not_json`, `duplicate_key`) | `broker.py:1485-1496, 1536-1548` | **unchanged** — shape-blind |
| 2 | derived fields | `codec.py:161-183` via `intent_door.refuse_derived_fields` | **unchanged** if the wire key `spec` survives; it walks `intent_id`, `meta.armed_ts` and `spec.tp_tranches[].r_multiple`, none of them `size` |
| 3 | JSON Schema, input shape, on the wire | `broker.py:1551-1576` | **needs the new shape** — see below |
| 4 | version | `broker.py:1579-1600` | **needs the new constant** and nothing else; one `SCHEMA_VERSION` edit |
| 5 | venue | `broker.py:1679-1684` | **unchanged** |
| 6 | identity shapes | `broker.py:1687` | **unchanged** — keyed on `generation` and `trade_date` |
| 7 | the pick key | `broker.py:1688-1696` | **unchanged** — keyed on (ticker, trade_date, generation) |
| 8 | the codec | `codec.py:243-259` | **needs the new shape** — `_decode_spec` lifts `spec["size"]` into `PickSize` today; after the move the top-level decode must do it |
| 9 | the fixed point | `codec.py:121-158` via `broker.py:1717-1724` | **the gate that catches a half-done split** — see below |
| 10 | `validate_intent` | `validate.py:163, 277-317` | **needs the new shape** at two sites; its reason codes do not change |

**Gate 3 refuses an old document for an absence, not for a leftover.** The
generated schema sets no `additionalProperties` anywhere — zero occurrences in
`apps/alphalens-broker-contract/docs/trade-intent-input-v3.schema.json`, and
`_define` emits only `type`, `properties` and `required`
(`json_schema.py:186-200`). So an unknown key passes this gate. Today
`TradeSpec.required` is `["entry_tiers", "disaster_stop", "tp_tranches",
"size"]` and the root's is `["instrument", "spec", "meta"]` (read out of the
committed input artefact). After the move, a v3-shaped document carries a
`spec.size` the schema ignores and lacks the root `size` the schema now requires:
the refusal is `schema_violation` on the missing root field.

**Gate 9 is the one the split has to be designed around.** `discarded_paths`
compares every path the client SENT against `intent_to_jsonable` of the decoded
object, because the decoder drops what it does not model with only a log line
(`codec.py:121-129, 192-209`). Three consequences:

- A split that leaves `size` unmodelled on *either* side — geometry type without
  it, `TradeIntent` without it — produces a document whose `size` is dropped by
  `_filtered` and therefore refused as `key_discarded`. A half-finished split
  cannot arm a pick silently. That is the gate working as designed.
- If the codec gains a `spec.size` → `size` migration for the drain (§3.4
  option b), gate 9 *still* refuses the old shape at the door, automatically,
  because the re-render puts the value at the new path and the old path comes
  back missing. This is the same asymmetry `meta.brief_date` has today: the drain
  migrates it, the door refuses it.
- The same gate forbids arming a raw `brief_trade_setup` payload. The brief
  carries keys the contract does not model — see "Concerns".

**Gate 10, concretely.** `_numeric_fields` yields the literal path string
`"size.notional_acct"` into each violation's detail (`validate.py:163`), and
`_stop_and_size_violations` reads `spec.size.notional_acct` and
`spec.size.currency` (`validate.py:301-317`). The reason codes
`size_notional_not_positive` and `size_currency_invalid` (`validate.py:131-132`)
keep their meanings, so the published README rows survive. **Judgement:** whether
the `details` path string stays `"size.notional_acct"` or becomes something else
is a choice; keeping it is free and keeps a client's log grep working.

Three consequential sites that are not gates but read the same field, and move
with it: `intent_door.tier_amounts` (`intent_door.py:416-419`),
`_echo_armed_intent` (`broker.py:1624-1654`), the drain's currency and ceiling
check (`control_loop.py:6090`), and the planner itself —
`broker_contract/sizing.py:280` reads `spec.size.notional_acct`, so the planner
must take the size beside the spec or take the whole intent.

### 3.4 The journal question

Armed picks are journalled as whole documents, and journals are append-only
(`legacy.py:99-101`). After the split a reader meets a line written in the
current shape: `spec.size` present, no sibling `size`. Three options:

| option | machinery | cost |
|---|---|---|
| **(a) skip before decoding**, as `size_pct_v2` does | already exists in full: `iter_picks` skip, `iter_legacy_size_pct_picks`, one-time drain refusal (`picks.py:505-544`, `control_loop.py:649-662`) | every armed, unplaced v3 pick must be hand-rewritten; and `_plan_size_shape` has no value for "v3 location" — `SIZE_SHAPES` is a closed enum in the published trades schema (`trades.py:243`, `trades_schema.py:361`), so (a) drags a second contract into the change |
| **(b) migrate the key in the codec**, as `brief_date_key` does | already exists as a one-line template (`codec.py:276-283`) plus a legacy entry and its markers | one legacy entry, one marker, one census row; the door still refuses the old shape via gate 9 at no extra cost |
| **(c) do nothing** | a v3 line raises `TradeIntentDecodeError` and is skipped with a WARNING per line per tick (`picks.py:519-525`) | the repo has already judged this unacceptable for an expected, inert condition and chose DEBUG instead (`picks.py:493-497`) |

**The machinery supports both (a) and (b), and the register itself picks (b).**
The entry for `size_pct_v2` says why a percent must not decode — the frame is
gone (`legacy.py:127-129`). No such argument exists for a relocation: the value
is the same amount in the same currency. (b) also keeps the trades record whole:
a migrated v3 line decodes fully, `plan_size_shape` stays `"by_amount"`, and
every historical pick keeps its money numbers.

Two notes on (c), derived from the code and **not executed** (this task is
read-only on the worktree): a `size` field declared with `contract_field(...)`
and no default is required, so `TradeIntent(**kwargs)` without it raises
`TypeError`, which `codec.py:300-301` converts to `TradeIntentDecodeError`.
Giving the new field a default to make old lines decode would be worse than
either option here — the pick would size at the default instead of refusing.

### 3.5 A migration order, and the pins that fix it

This repository has mutual pins between a registry and a published artefact. The
ones a `size` move touches:

| pin | both directions? | test |
|---|---|---|
| `schema.py` ↔ both committed artefacts ↔ the README artefact rows | yes; filenames derive from `SCHEMA_VERSION` (`json_schema.py:66-67`) | `test_json_schema.py:761-776, 778-781, 788-793` |
| `legacy.py` entries ↔ `# LEGACY(<key>)` markers in the tree | yes | `test_legacy_register.py:78-98` |
| the three `examples/manual-pick/*.json` templates ↔ the README copy recipe | the templates are armed through the real `broker arm`, and the recipe is read literally out of the README and run | `tests/brokers/automanager/test_manual_pick_templates.py:1-11, 33-37` |
| the replay's path registries ↔ the spec tables in `docs/superpowers/specs/2026-09-23-intent-replay-design.md` | yes, explicitly ("a row in the code with no spec row is red … and a spec row the code dropped is red") | `tests/intent_replay/test_classification.py:363-375` |
| `CONTAINERS` ↔ the input schema's object and array nodes | yes | `test_classification.py:374-375` |
| `intent-replay` refusal codes ↔ its README | yes | `tests/intent_replay/test_cli_codes_published.py:75-96` |
| broker failure codes ↔ contract README table | yes | `tests/brokers/test_broker_failure_codes_published.py` |

All three templates carry `spec.size` today (`{"notional_acct": 1500.0,
"currency": "EUR"}` in each of the three files). `classification.py:77` lists
`spec.size` as a container, and the schema-coverage test asserts the input schema
has 37 paths of which 16 are required (`test_classification.py:334-361`) — a
relocation keeps both counts and changes the names, so the count assertions stay
green while the table comparisons go red. That is the right failure.

**Order.**

1. **Nothing wire-visible first.** There is no wire-neutral refactor step
   available: `_node` turns a nested dataclass into a `$ref`
   (`json_schema.py:157-159`), so introducing a `TradeGeometry` dataclass that
   `TradeSpec` composes *is already* an artefact change and already breaking
   (`TradeSpec.entry_tiers` and friends would read as "field removed"). **Options,
   with costs:** (i) make the geometry type structural — a `Protocol` or a plain
   function signature over the existing flat `TradeSpec` fields — which is free
   on the wire and gives the brief producer something to target, but is not a
   wire type; (ii) accept that step 1 does not exist and fold everything into the
   bump. (i) is the cheaper first commit if the brief-side work is to start before
   the bump lands.
2. **The bump, as one commit, by force of the pins.** `SCHEMA_VERSION = "4"` with
   its changelog comment line, the field move, both regenerated artefacts, the
   README artefact rows and the `spec.size` prose at `README.md:267-290`, the
   three templates, the codec migration (§3.4 option b) with its `legacy.py`
   entry and `# LEGACY(...)` markers, the `validate.py` / `sizing.py` /
   `intent_door.py` / `control_loop.py` / `broker.py` read sites, and the replay's
   `classification.py` rows together with the spec-table rows they are pinned to.
   Nothing in that list can land separately: each item is held by a gate to one of
   the others.
3. **The brief-side producer, after, with its own version.**
   `thematic/trade_setup/model.py:13` declares `SCHEMA_VERSION = "1.1.0"` and the
   literal is asserted in
   `tests/thematic/trade_setup/test_config_version.py:69`, so a brief-side shape
   change is its own bump and its own test edit — independent of step 2's pins.
   Note the complementary token: `setup_builder_config_version` bumps `_STAMP_SCHEMA`
   only when the stamp's *shape* changes, never on a value change
   (`trade_setup/config_version.py:18-22`).
4. **The replay taking size as a declared run value, after that.** Today the size
   comes off the document: `cli.py:638` passes
   `admitted.intent.spec.size.currency` into `RunConfig.from_jsonable`
   (`config.py:261-270`), and the interpreter reads both leaves by path
   (`interpreter.py:382-383`). Moving the amount into the configuration changes
   the replay's config contract and its published tables, not the wire, so it can
   follow. The machinery for "the adapter supplies what the document does not
   carry" already exists: `door.complete` injects the `intent_id` and `armed_ts`
   sentinels before validation (`door.py:122-144`), and the same shape can compose
   a `TradeIntent` from a geometry document plus a declared size, which avoids
   publishing a third schema shape.

---

## 4. Replacing the what-if engine, and the basis change it brings

The two engines do not compute the same quantity. The deprecated one computes a
dimensionless price-geometry number over an idealised fill; `intent-replay`
computes cash and an R against a stated account, a stated currency and a stated
cost card. Swapping one for the other changes the **basis** of every number on
`/edge`, so this section states what each basis is before it states what to do
with the old rows.

### 4.1 What the deprecated engine models

The fill model is one sentence, and the module states it as the only modelling
assumption it makes:

> "a level is "executed at that level" the first time price touches it -- entry
> on ``low <= limit`` (a dip), TP on ``high >= target`` (a rally), SL on
> ``low <= disaster_stop``. This is the exact price the strategy's geometry
> assumes (resting-limit price-improvement), so the realized R it produces is
> the clean, slippage-free number"
> — `apps/alphalens-pipeline/alphalens_pipeline/feedback/ladder_replay.py:10-16`

What it deliberately leaves out follows from that word *slippage-free*. The
module has no money dimension at all: a case-insensitive grep for
`commission`, `_bps`, `currency`, `notional` and `whole_share` across its ~1400
lines returns **zero matches**. `realized_r` is built only from prices —
`risk = blended - stop` (`ladder_replay.py:1030`) and
`contrib += share * (t.price - blended) / risk` (`ladder_replay.py:1138`). There
is no commission, no exchange fee, no FX leg, no share lattice and no account.
Three further omissions are stated in the file: intra-bar ordering is not
resolvable at minute granularity and is settled conservatively as
`TIE_BREAK_SL_FIRST = "sl_first"` with the bar flagged (`ladder_replay.py:77-82`);
the realised-fill anchor is the blend of tier **limits**, not of broker fill
prices, and the file says so — "a third anchor (the realised average fill)
exists in the live journal and is not modelled here" (`ladder_replay.py:68-71`);
and pre/post-market fills are excluded by design because "resting-limit geometry
is an RTH construct" (`population_ladder_monitor.py:749-755`).

Its three measurement layers are declared in priority order
(`ladder_replay.py:18-32`):

| layer | what it is | key |
|---|---|---|
| 1. substrate, policy-free | crossing sequence, MFE/MAE anchored to the blended entry, `forward_return` (close-to-close, fill-independent) | re-derivable with no vendor call |
| 2. as-specified, headline | the ladder replayed exactly as `brief_trade_setup` states it — static stop, fixed TP targets | `realized_r`, the only number that maps 1:1 to what the tool emits |
| 3. what-if, namespaced | the same path under another exit policy; "NEVER overrides the layer-2 headline" | `ratchet_realized_r`, `grid_realized_r_json`, `breakeven_realized_r_json` |

The nine variants split across two registries. Three are a fixed tuple,
`GRID_CONFIGS = ("single_tp_first", "single_tp_last", "no_tp_ride")`
(`ladder_replay.py:342`). Six are the lens registry
`BREAKEVEN_LENSES` (`breakeven_lenses.py:170-321`), and that registry declares
its own cap:

> `MAX_REGISTERED_LENSES = 6` — "the registry is bounded — at most this many
> concurrently registered lenses (one-in-one-out beyond it); every registered
> lens, retired ones included, counts toward the walk-forward multiplicity
> budget." Raised from 5 on 2026-09-01 under the ADR 0013 R4 amendment
> (`breakeven_lenses.py:155-163`).

So the registry is **full**. A tenth variant is not an addition; it is a
one-in-one-out that does not refund the multiplicity budget of the lens it
replaces. That is the structural reason this engine cannot grow toward the
engine — not a preference.

Two properties of the stored lens values matter for anything downstream. The
store records **no provenance for a lens value**, so a forward-stamped cell and
a backfilled cell are indistinguishable; and a from-scratch rebuild *replaces*
lens history rather than adding to it — on the 591 rows terminal both before and
after the 2026-09-12 rebuild, every lens lost between 5 and 34 values it used to
hold (`breakeven_lenses.py:44-64`). The same rebuild also moved frozen terminal
rows outside the lens layer: `realized_r` on 21.1% of the 418 comparable rows,
and 38 ladder classifications (`breakeven_lenses.py:52-55`). ADR 0013 R3 permits
recomputation of the lens layer and not of that.

### 4.2 What `intent-replay` charges that the lens assumes away

| | what the lens assumes | what the engine charges |
|---|---|---|
| session grid | `close_ms = open_ms + span_min*60_000` and membership is `lo <= ts <= hi`, close **inclusive** (`population_ladder_monitor.py:727-736, 775`) | both bounds read from the calendar per session, never `open + 390` — "measured over 29 venues, that assumption is wrong on 21 of them in both directions" (`replay_from_pick.py:744-753`); membership is `open_ms <= t < close_ms` (`replay_from_pick.py:741`, `SessionBounds.member` at `:773-780`) |
| the close-stamped bar | kept, and its high/low can carry post-market trading | placed but excluded, with its count and its full quadruple in the sidecar (`DroppedBar`, `replay_from_pick.py:832-845`) |
| off-grid bars | dropped by the RTH filter at replay time; the cache keeps them (`population_ladder_monitor.py:740-779`) | dropped with a count on the fetch path — "pre-market, post-market, 12.08% of a raw payload" — and **refused** on an operator-supplied file (`OffGridBar`, `replay_from_pick.py:816-826`) |
| intra-bar tie | `sl_first`, claimed conservative (`ladder_replay.py:77-82`) | `intrabar_rule = "entries_then_stop_then_ladder"` (`envelope.py:56`), published as a NAME and explicitly not a bound; `snu_bars` counts the bars where the order decided money, and the count "only goes one way" (`apps/intent-replay/README.md:231-270`) |
| cost of exiting | nothing; every touched TP sells | a tranche fires only if its own level clears its own round trip plus the stated edge: `clears_cost` / `min_profitable_exit_price` (`cost_gate.py:97-128`), where the daemon's own declared parameter is `EXIT_EDGE_MIN_BPS = 50.0` (`brokers/automanager/costs.py:203`) |
| currency | no currency anywhere | `fx.instrument_currency` always required; a cross-currency run also requires `mid_rate` (unit spells the direction, `USD_per_EUR`), `round_trip_cost_rate` and `sizing_buffer_pct`, and stating any of them on a same-currency run is `unknown_key` (`README.md:35-60`; `fx.py:40-112`) |
| share lattice | fractional shares implied by alloc weights | also fractional — a **recorded scope cut**, not a modelled fact: "The live drain buys whole shares; the replay works in fractional units", measured at 17.50 on a 1500 budget, 130.00 on 8000 and 60.81 on 1000 where the entry anchor itself moves ~30 bps (`README.md:285-296`) |
| exit policy | one static stop in layer 2; alternatives live in nine columns | the policy the **document declares**, one per run, resolved through `broker_contract.exit_geometry.registry.resolve_declared_policy` (`walk.py:21`, `envelope.py:111`) |
| refusal | a lens that cannot resolve maps to `None`, silently (`breakeven_lenses.py:415-428`) | 13 published codes, none retryable, each with a closed `details.reason` vocabulary (`README.md:559-593`; `refusal.py:29-45`) |

**Direction.** The expected direction is lower, and two terms are the reason.
The first is the **entry fill price**: with `entry_trail_bps` stated — and on
both deployments the flag is set, so `null` "replays an entry ladder production
does not use" (`README.md:23`) — nothing rests at a rung; the fill is
`trough + distance`, which can sit above the rung's limit, where the lens fills
at the limit exactly (`README.md:416-428`). A higher average entry both enlarges
the R denominator and shortens the distance to each target. The second is the
**close-bar drop**: the lens's extra range manufactures touches, and on real
CRSR bars keeping that bar flipped the outcome word from `open` to `closed_tp`.

Two corrections to that direction, both measured, both of which a migration memo
must carry:

- **The cost gate does not bind at brief-scale target distances.** On the quoted
  `brief_trade_setup` payload the planned blend is 72.44977 and TP1 sits
  **2404.4 bps** above it (the payload's own `r_multiple: 2.0` reproduces
  exactly, confirming the brief's R is blend-anchored). Against that, 50 bps of
  declared edge plus a round-trip commission is roughly two orders of magnitude
  from binding. The gate is a real term that will matter on tight ladders and
  does not move this one.
- **A worse exit price is not a worse answer.** The repository measured both
  channels over the burnt panel at the live setting: −0.644 R across 91 cut
  winners against +1.175 R across 43 saved losers, net inside its
  cluster-bootstrap interval (`README.md:509-517`). So no scalar correction may
  be applied to an R figure to bridge the two series.

Also note that `pnl_cash` is **gross**: "The `costs` block decides which
take-profit tranches fire. It never reduces the cash" (`README.md:272-273`). The
new series is not a net-of-cost series; it is a differently-gated gross series.

### 4.3 The declared divergences

`intent-replay` publishes, per run, the places it is known to differ from the
daemon for a reason no configuration value can close — "because the replay lacks
a FACT rather than a setting" (`README.md:371-376`). The registry is four
entries and each is emitted only on the runs it applies to (`envelope.py:75-119`):

| entry | emitted when | what it means for a population study |
|---|---|---|
| `daemon_trail_guards` | the declared policy moves the stop at all; covers both stop arms (`envelope.py:114`) | the daemon's two order-state guards cannot be evaluated, so any stop-moving policy is replayed without them. A population run declaring `be@0.5R/trail0.6` carries this on **every** row, so it is a property of the study, not of a row |
| `daemon_reanchor_latch_is_journal_lifetime` | the reaction is `ReanchorOnFill` (`envelope.py:115`) | the idempotence latch is recomputed from the run's own trace, with exact equality and only within one run — differs in lifetime and tolerance. Only fires if the study declares a re-anchoring policy |
| `native_entry_trail_is_a_broker_model` | the run states an entry-trail distance (`envelope.py:116`) | the weakest evidence in the tool: a vendor's documented behaviour plus a handful of live fires, with no local implementation the parity test can reach. Two of its omissions are marked **FLATTERS** — the trail riding the whole entry window where live it is a DayOrder, and the server ratcheting from placement where the replay's trough includes the whole arming bar (`README.md:402-477`). Since the production flag is always set, this fires on every row of any realistic population run |
| `take_profit_observation_time` | the resolved take-profit ladder is non-empty (`envelope.py:117`) | the poll time and the quote are unknown, so the moment a tranche is observed is not the daemon's moment. Fires on every brief candidate, because every `brief_trade_setup` carries `tp_tranches` |

A fifth entry, `cost_gate_prices_the_account_currency`, was **retired** when the
FX keys landed; the whole-share half of what it named survives unreported,
because a scope cut is not a missing fact (`envelope.py:67-74`;
`README.md:392-399`). The practical consequence for a population study: three of
the four entries fire on essentially every row, so they are study-level caveats
to be stated once in the memo, and only `daemon_reanchor_latch…` is row-dependent.

### 4.4 The series question

**The job and the rebuild path.** Postgres is derived, not authoritative.
`python manage.py rebuild_ladder_outcomes_cache`
(`apps/alphalens-django/edge/management/commands/rebuild_ladder_outcomes_cache.py`)
is a thin wrapper over `edge.ingest.parquet.rebuild_from_parquet`
(`apps/alphalens-django/edge/ingest/parquet.py:234`). The path is:
`~/.alphalens/population_ladders/<YYYY-MM-DD>.parquet`
(`DEFAULT_LADDER_OUTCOMES_DIR`, `parquet.py:50-54`) → the settled-watermark gate
(a parquet newer than `.ingest_watermark.json` is skipped as unsettled,
`parquet.py:267-273`) → the per-date mtime gate (`parquet.py:274-276`) →
delete-then-`bulk_create` for that date (`parquet.py:277`). It runs hourly from
`alphalens-edge-mirror.service` and on the compute job's `OnSuccess=` handoff
(`rebuild_ladder_outcomes_cache.py:7-8`). So **a decision about the old rows is a
decision about the 146 parquets**; the table follows within the hour either way.

**The three options.**

| option | what it costs | what it preserves |
|---|---|---|
| **Recompute** all 916 episodes on the engine | No vendor spend — the 1652 bar parquets (406 MB) are already on disk. But: eight configuration keys must be derived per episode across 146 dates; the nine what-if columns have no engine equivalent, because the engine replays the one policy a document declares, so `grid_realized_r_json` and `breakeven_realized_r_json` and the `/edge` lens selector all go away in the same change; and it restamps frozen terminal rows outside the lens layer, which ADR 0013 R3 does not permit (`breakeven_lenses.py:44-55`) — so it needs an ADR amendment, not a script | one basis, no boundary. Destroys the ability to ask the old question of the past |
| **Cut at a date** | Cheapest to execute. But every pooled mean on `/edge` then silently spans two bases, and **nothing today can separate them**: `ladder_config_version` is written (`edge/models.py:156`; stamped at `population_ladder_monitor.py:1047` and `:1335`) and a grep across `apps/` finds only writers, tests, a research script and docs — **no Django view and no SPA component reads it**. A cut therefore needs a reader built before the cut, or the boundary is invisible in the UI | nothing, across the boundary, until that reader exists |
| **Keep both under a version column** | The column already exists and the token is already the serialised config, byte-identical for identical configs so a plain SQL `GROUP BY` partitions by geometry (`ladder_config.py:1-23, 45-66`). Costs: extend the token (or add a sibling column) so an engine row cannot collide with a lens row; build the reader and the SPA facet that still do not exist; and carry the registry's own hard-learned cohort rule — compare only on rows where **both** sides are non-null, because the two sides share some gates and not others (`breakeven_lenses.py:260-267`) | both series side by side, and the only way to measure the size of the basis change |

**Recommendation: keep both under a version column.** `/edge` exists to answer
"does the pipeline select well?", and that answer is a mean over candidates. A
mean is a statement about selection only if every row inside it was measured the
same way; mixing two bases in one mean produces a number that reads as a change
in selection and is a change in measurement. Option (a) answers the question
going forward and makes it unaskable about the past. Option (b) cannot answer it
across the boundary at all today, because the key that would split the cohorts
has no reader. Option (c) is the only one under which the overlap — the 740
episodes that carry the full comparison pair — can be run on both engines and
the shift measured rather than assumed. That measurement does not exist, and the
migration should not start without it.

### 4.5 What a population run needs that `replay_from_pick` does not provide

**The seam.** `BarFetch = Callable[[str, dt.datetime, dt.datetime], Sequence[dict[str, Any]]]`
(`apps/alphalens-pipeline/alphalens_pipeline/feedback/bar_window.py:44`), whose
production default is `default_bar_fetch`, calling
`get_default_polygon_client().get_agg_range` (`bar_window.py:47-53`).
`replay_from_pick.main` takes it as a keyword argument with that default stated
in the signature rather than resolved inside, "because a `None` would leave the
vendor call untested in every direction" (`replay_from_pick.py:2323, 2332-2340`).
So the seam for a population run is **already the right shape** and needs no new
type: a reader over the store satisfies `BarFetch`.

Four things such a reader has to handle, each of which is a real obstacle rather
than plumbing:

1. **The key is not the window.** Files live at
   `population_ladders/bars/<TICKER>_<arrival_session>.parquet`, keyed by
   `(ticker, arrival_session)` so that "a later-arrival brief can never set the
   cache floor for an earlier-arrival one" (`_bars_cache_path`,
   `population_ladder_monitor.py:508-517`). `BarFetch` carries no arrival
   session, so the reader must be a closure over the episode's brief date.
2. **The columns differ.** The cache writes `("t","o","h","l","c","v")`
   (`population_ladder_monitor.py:230`); the engine's bar shape is the closed key
   set `("t","open","high","low","close")` (`intent-replay/intent_replay/bars.py:74`),
   and a key a bar does not model is refused. The converter already exists and is
   additive by construction — `published_bars`, built key by key from the
   engine's own tuple via `_VENDOR_KEY` (`replay_from_pick.py:1026, 1038-1056`) —
   so `v` is dropped rather than refused.
3. **The parquet is raw, including pre/post-market.** "The cache itself keeps
   every fetched bar (a faithful raw record); this filter applies at replay time
   only" (`population_ladder_monitor.py:753-755`). Off-grid bars are 12.08% of a
   raw payload, and `replay_from_pick` **refuses** them on a supplied file while
   dropping them with a count on the fetch path (`replay_from_pick.py:816-826`).
   So the store read must enter through the drop-with-count path. Handing a
   cache parquet to `--bars` would refuse.
4. **Two basis mismatches to check, not assume.** The store's RTH filter keeps
   the close-stamped bar and uses `open + span`
   (`population_ladder_monitor.py:736, 775`); the engine's grid excludes it and
   reads both bounds per session. The same raw file read under the two rules is
   two different series.

**What the provenance sidecar must record.** Today `bars_provenance`
(`replay_from_pick.py:1258-1349`) knows exactly two sources,
`_BAR_SOURCE_VENDOR = "polygon"` and `_BAR_SOURCE_FILE = "file"`, and the comment
beside them states why they are not interchangeable: "the vendor path can name an
endpoint and assert a basis, a supplied file cannot"
(`replay_from_pick.py:1032-1035`). Accordingly `vendor` is the `_VENDOR_BLOCK`
only on the vendor path and `None` otherwise (`:1289`), and `adjusted` likewise
(`:1298`), because "asserting one would be the construction this file exists to
prevent" (`:1286-1288`). A store read is a **third** source and must be recorded
as one:

| field | why |
|---|---|
| a `source` token distinct from both | so `vendor` stays `null` and no endpoint is asserted for a file nobody fetched in this run |
| `source_path` + `source_sha256` | both fields already exist (`:1283-1285`); the sha is what makes a re-read checkable |
| the `(ticker, arrival_session)` key | the file is selected by that key, not by the window; without it the read is not reproducible |
| the store file's own write time, labelled as such | the cache is tail-extended (`_extend_bar_cache`, `population_ladder_monitor.py:548+`), so its mtime is the time of the **last** tail fetch, not of the window — reading it as a fetch time would be wrong |
| `adjusted` as **unknown**, not `false` | `BarFetch` takes three positional arguments and never forwards `adjusted` (`bar_window.py:44`), so the basis is whatever the client defaulted to at write time, and `replay_from_pick` can only assert `_BAR_ADJUSTED = False` for a fetch it made itself (`:1013-1018`). **GAP** — the cache parquet carries no basis column. Settled by stamping one in `_write_cached_bars` (`population_ladder_monitor.py:536-546`), or by refusing to state a basis for a store read |
| this read's own off-grid and close-bar counts | `counts` and `close_bar_drop` (`:1322-1339`) are properties of the read, not of the file; the same file under two calendars yields two series |

**Why the refusal census is part of the result.** The engine refuses by design —
"Every value it needs is read from the document or stated in the run
configuration… A missing value is a refusal, never a default"
(`apps/intent-replay/README.md:7-11`) — across 13 codes, none retryable
(`README.md:559-573`). On a population run those refusals are the **first
validation `brief_trade_setup` has ever had**: the brief payload passes through
no JSON schema, no codec fixed point and no `validate_intent` today, so
`intent_invalid` with its published vocabulary (`INTENT_INVALID_REASONS`,
`broker_contract/trade_intent/validate.py:112-154`) answers a question `/edge`
cannot currently ask — how many brief setups the arming door would have taken at
all. Two shapes a brief payload is most likely to trip: `entry_mode_unsupported`,
since only `pullback` rests as a rung a bar walk can test (`README.md:568`), and
the percent size — the quoted payload carries `suggested_size_pct` and no
`spec.size`, which is exactly `LEGACY(size_pct_v2)`, a shape the reader skips and
the drain refuses (`broker_contract/trade_intent/legacy.py:123-135`). The latter
is the epic's own point: the size must arrive as a declared run value.

**What it is a denominator of: the admitted set.** A mean R over admitted
episodes is comparable to the lens mean only if the refused set is named and
counted. Otherwise the engine's mean is a mean over a silently selected
sub-panel, which is the failure shape
`feedback_shared_upstream_read_is_not_corroboration_2026_09_24` records. So the
result must publish `admitted` and `refused by code` against the stated
denominators — 916 terminal episodes across 287 tickers, 740 of them carrying the
full comparison pair — and every reported mean must travel with the count it was
taken over.

**The configuration keys a population run must state.** The engine needs eight
(`README.md:19-29`). Most are derivable per episode: `walk_start` from
`session_open_utc(day 1)`, `entry_deadline` from the brief's own
`order_ttl_days: 7`, `time_stop_t` from `TIME_STOP_DAYS = 42`
(`alphalens_pipeline/paper/constants.py:22`), `fx` as USD=USD for a US venue,
`costs` from `costs.fee_card_for` by MIC, `ceiling_price: null` and `oco: false`.
`entry_trail_bps` is the exception and is a **GAP**: there is no trades record
for a brief candidate to read a `watch_open` `d_bps` from, and `null` "is NOT the
neutral run" (`README.md:23`), so the study must declare one distance for the
whole panel and say which, in the registration, before it runs.

---

## 5. Alternatives refuted

Four shapes were considered and killed. Each is recorded with the evidence, so a later reader does not reopen it.

### 5.1 Render a `TradeIntent` from `brief_trade_setup` at measurement time, leaving both shapes in place

This was the first proposal. It is inferior because it adds a translation without removing the defect that makes the translation necessary.

**What stays unvalidated.** The brief side keeps every hole it has today. `brief_trade_setup` is stored as a bare `models.JSONField(null=True, blank=True)` (`apps/alphalens-django/briefs/models.py:189`), and the only published JSON Schemas in the repository are the two `TradeIntent` ones and `broker-trades` (`apps/alphalens-broker-contract/docs/`). The one check that exists, `validate_trade_setup`, tests five things: the value is a dict, `status == "OK"`, `schema_version ∈ {"1.0.0", "1.1.0"}`, a positive `suggested_size_pct`, a positive `disaster_stop`, and a non-empty `entry_tiers` with at least one positive `limit` (`apps/alphalens-pipeline/alphalens_pipeline/paper/sizing.py:30-77`). It never reads `tp_tranches`, never checks the stop sits below the entries, never checks the allocations sum, and has no fixed point — a key it does not model passes silently.

**A typo gets three different answers from three readers.** This is the concrete cost of having no fixed point on the brief side:

| Reader | Expression | A typo in that key |
|---|---|---|
| `ladder_replay.parse_ladder` | `float(t["limit"])` (`feedback/ladder_replay.py:205`) | `KeyError`, which propagates out of the replay |
| `paper.sizing.planned_blended_entry` | `float(t.get("limit", 0) or 0)`, then drops non-positive (`paper/sizing.py:102-106`) | the rung silently vanishes from the blend |
| `ladder_replay.parse_ladder` | `float(t.get("alloc_pct", 0.0))` (`feedback/ladder_replay.py:205`) | the rung is silently weighted 0, changing the blend and `total_entry_alloc` |

The arming door refuses exactly this class by name — `key_discarded`, raised because "the decoder otherwise DROPS what it does not model, and a typo'd `limit_pirce` would arm at a price you never sent" (`alphalens_cli/commands/broker.py:1717-1724, 1767-1769`). A render step inherits none of that protection for its input.

**What has to be decided at every call site rather than once.** A render is a translation with at least eight choices, and nothing checks any of them: `limit` → `limit_price`, `target` → `price`, `label` versus `tag`, where `atr_distance` / `asof_close` / `atr` / `builder_config_version` / `status` go (none has a `TradeSpec` home, and the fixed point refuses any of them if passed through), `meta.source`, `meta.trade_date`, `entry_mode`, and the size — which the input schema makes mandatory: `TradeSpec.required` is `["entry_tiers", "disaster_stop", "tp_tranches", "size"]` (read off `apps/alphalens-broker-contract/docs/trade-intent-input-v3.schema.json`). Twenty-two non-test modules read an `entry_tiers` key today; the brief-side geometry readers among them include `feedback/ladder_replay.py`, `feedback/population_ladder_monitor.py`, `paper/sizing.py`, `alphalens_research/diagnostics/{exit_policy_replay,fill_partition}.py` and nine scripts under `apps/alphalens-research/scripts/`.

**And it restores what #1552 deleted on purpose.** The brief-to-spec parse "went with the brief producer in #1552: every pick is a hand-written document now" (`broker_contract/sizing.py:38-39`), the stated reason being "so that every decision in a pick (entries, stop, take-profits, size, exit) is one its author stated" (`CLAUDE.md:170`). A measurement-time render puts the same translation back, with no author to state anything.

Verdict: **refuted.** It leaves the producing side unchecked, adds a per-call-site translation where the split adds one typed boundary, and re-creates a deleted parse.

### 5.2 Have the brief emit a full `TradeIntent`, size included

This pushes account-bound facts and queue identity across a bounded-context line the repository states twice.

> "**Execution is client-agnostic, and its client is a human decision.** The brief and the WhatsApp group are two of its clients; neither is upstream of it." (`CLAUDE.md:72-75`)

> "**`/edge` means 'does the pipeline select well?'**, never 'how is the owner's money doing'. Its subject is every candidate of every brief." (`CLAUDE.md:79-81`)

A brief emitting a full `TradeIntent` would have to know four things it has no business knowing:

1. **The account currency.** It is read off the broker at runtime (`brokers/automanager/control_loop.py:6086`) and compared against `spec.size.currency` with a refusal — "the pick is sized in X but the account is Y — refused" (`brokers/automanager/pick_money_gates.py:85-89`). Measured PLN on 104 of 104 LIVE booking rows (`docs/research/account_cashbook_design_2026_10_05.md:42`), while the committed templates state EUR (`apps/alphalens-broker-contract/examples/manual-pick/pullback-two-tiers.json:10`). The brief has no way to know which.
2. **The amount.** `spec.size.notional_acct` is "the budget for the WHOLE entry ladder, in the account currency" (`broker_contract/trade_intent/schema.py:219-222`), capped on LIVE at 15 000 by a boot rail (`brokers/automanager/live_rails.py:112`). ADR 0013 records that no portfolio-level sizing exists at all: "sizing is per-name equal-risk inside T5 (`suggested_size_pct`, 1% risk budget)" (`docs/adr/0013-trade-side-layer-architecture.md:221`). An amount is a capital decision, not a selection output.
3. **Queue identity.** The door derives `intent_id` and `meta.armed_ts` and *refuses them on input* (`schema.py:440-444`, `:381-386`), and fills `meta.generation` from the queue fold plus the placed-key set (`commands/broker.py:1688-1696`). A brief would have to either omit them — in which case its document is the *input* shape, which is this proposal, not this alternative — or invent them.
4. **The venue.** `InstrumentHint` requires an ISO 10383 MIC (`schema.py:134-138`). The brief carries only a ticker: no `mic`, no `exchange`, and no `primary_exchange` field exists anywhere in `alphalens_pipeline` outside the broker layer. The population monitor runs on the `XNYS` calendar default and never passes an `exchange` (`alphalens_pipeline/market/calendar.py:80`).

There is a volume argument too. A brief publishes many candidates — 554 `OK` setups over 2026-06-13→08-11 (`docs/research/broker_sizing_declared_frame_design_2026_08_12.md:107`). Emitting 554 armable documents makes the brief a producer of picks, which is the thing #1552 removed.

Verdict: **refuted.** Size, currency, venue and queue identity belong to execution and to the owner. An account-free geometry type is exactly the subset a brief *can* state.

### 5.3 Keep the lenses and add `intent-replay` beside them

Two engines answering one question about one panel. The repository's own recorded lesson is against it, and the specific failure has already happened here once.

**The lesson.** "**Studies that share an upstream read do not corroborate each other.** … **A module that computes one quantity on two sides** (burnt/held-out, train/test) **must compute it in ONE function**" (`CLAUDE.md:274`; memory `fb_shared_upstream_260924`). Both engines would read the same minute-bar store (`~/.alphalens/population_ladders/bars/`, 1652 parquets), so their agreement would carry no information and their disagreement would have no adjudicator.

**It already cost a registration slot.** The 2026-08-24 exit-policy head-to-head pre-registration voided itself, and its own ledger row records why: "Primary is NOT computable from the stamped columns (the lenses omit the live take-profit clamp and the two stamped R columns have different denominators)" (`docs/research/edge_hypothesis_budget_2026_07.md:112`). Two R columns with two denominators inside *one* engine was already enough to kill a registration.

**The two engines do disagree, measurably, and on a known set of rows.** They allocate take-profit tranches differently on every partial fill. The lens re-bases a tranche to its share of the *filled* position, `share_of_filled = (tranche_pct / 100) / filled_frac`, capped so cumulative shares never exceed 1.0 (`feedback/ladder_replay.py:1096-1119`). The replay sells `min(tranche.fraction * intended, held)`, where `intended = sum(rung.notional / rung.limit_price)` over **all** rungs (`apps/intent-replay/intent_replay/walk.py:652, 968`) — it does not scale up. So on any partially filled episode the lens realises take-profit gains on a larger share than the replay does. Keeping both means publishing both numbers with no rule for which is the answer.

**Multiplicity does not get cheaper.** ADR 0013 R4, as amended 2026-09-25: "the charge follows the CLAIM, not the tool. A look charges this budget the moment its result is quoted in a decision, a memo, an issue or a PR that changes what ships — whatever produced it, and whether or not a stamped column exists. **Not stamping is NECESSARY for a look to be exploratory and never sufficient**" (`docs/adr/0013-trade-side-layer-architecture.md:119-126`). The lens registry cap is 6 (`:211`), and the budget ledger already recorded it as full at the fifth entry: "5th of cap-5 — the registry is now FULL, any further lens is one-in-one-out" (`edge_hypothesis_budget_2026_07.md:111`).

**The lens store cannot settle a disagreement.** It "records NO provenance for a lens value, so a forward-stamped cell and a backfilled cell are indistinguishable", and a rebuild *replaces* lens history: on the 591 rows terminal both before and after the 2026-09-12 rebuild, "EVERY lens lost some values it used to hold (between 5 and 34 of them)" (`feedback/breakeven_lenses.py:46-68`). Both trailing lenses also still carry `status="in_sample"` (`:216`, `:313`), so the pair would be two engines disagreeing about a number neither is permitted to call validated.

Verdict: **refuted.** Keeping both buys a second way to spend one budget on one upstream read, with no adjudicator and a known systematic divergence.

### 5.4 Teach the lenses the costs instead of migrating

**A CI gate forbids the import outright.** `apps/alphalens-research/tests/test_module_dependencies.py:128-136` declares the rule "feedback must not import brokers (replay stays broker-free per ADR 0012)", with `from_pkg: "alphalens_pipeline.feedback"`, `forbidden_prefix: "alphalens_pipeline.brokers"` and `exemptions: set()`. The cost functions live at `alphalens_pipeline/brokers/automanager/costs.py` (`fee_card_for` at `:142`, `FX_ROUND_TRIP_RATE = 0.0050` at `:60`, `EXIT_EDGE_MIN_BPS = 50.0` at `:203`).

**The fee card cannot be lifted into the shared contract either.** A sibling test pins it out: "The fee card cannot move into the contract either (`test_broker_failure_codes_published`'s sibling `test_broker_contract_has_no_vendor_economics`)" (`apps/alphalens-research/tests/intent_replay/test_cost_gate.py:3-10`).

**So in practice it means a third copy of the arithmetic.** Two already exist: the daemon's, and `intent_replay/cost_gate.py`, which states in its own docstring that it is "a REWRITE of `live_exit_engine._exit_clears_cost` … not an import of them" kept honest "against a GOLDEN TABLE computed from the daemon's own function" (`cost_gate.py:5-13`). A third copy inside `feedback/` would need its own golden table, and the project would be maintaining one formula in three places.

**And "the costs" is not the whole list.** The lens engine today has no money in it at all. Its fill model is declared "the clean, slippage-free number" (`feedback/ladder_replay.py:9-16`), the module "imports nothing from the store / Polygon / broker" (`:36-38`), and the only cost-shaped module beside it charges a half-spread plus a market-impact proxy, one way, entry only, with "All constants … an UNVALIDATED proxy — no real fills exist post-ADR-0012" (`feedback/execution_cost.py:1-8`) — no commission, no per-fill minimum, no FX leg. To answer the same question the lenses would additionally need a declared size, a currency, a MIC, and either the whole-share lattice or a stated decision not to have one. `intent_replay/cost_gate.py:27-34` already measured how much the size matters: the threshold comes out too low by **1.05 bps** at a budget of 1000 against 950 whole shares' worth, **15.24 bps** at 250 against 210, and **38.10 bps** at 100 against 84, with the fee card's knee at `min_commission / commission_rate` = 1250 (`tests/intent_replay/test_cost_gate.py:30-32`).

Verdict: **refuted.** Adding a document-shaped input, a stated size and currency, a venue calendar, a fee card and a cost gate to `feedback/` is building `apps/intent-replay` a second time — except behind no door, and against a CI gate that would have to be deleted first.

## 6. Open questions, which are the owner's

Each of these is a judgement call, not a measurement. They are stated here with their options and costs; none is answered.

### 6.1 `meta.source` for a candidate document

**The question.** What does a brief-derived candidate document write in `meta.source`, given that the honest value is a legacy marker?

| Answer | What it costs | |
|---|---|---|
| `"brief"` | Honest, and the input schema accepts it (`enum: ["brief", "manual"]`). But it is a registered legacy allowance, `source_brief`, whose retirement "makes the door refuse `"brief"` and drops the value from the schema, a BREAKING change that bumps `SCHEMA_VERSION`" (`broker_contract/trade_intent/legacy.py:136-152`). It also makes `meta.trade_date` required — "day 1 of a brief pick is the session after its brief date, which the door cannot know" (`brokers/automanager/intent_door.py:196-199`) — and anchors day 1 on the session **strictly after** `trade_date` (`brokers/automanager/day1_gap_gate.py:99-102, 125`). | |
| `"manual"` | Day 1 becomes the `trade_date`'s own session (`day1_gap_gate.py:99-101`). But the field's own published meaning is that "Journals and later measurement separate the two populations on this marker" (`schema.py:400-407`), so writing `"manual"` on a machine-produced geometry contaminates the marker the owner's own LIVE picks are counted on. | |
| a third enum value | Additive, and names the thing honestly. But the legacy register's retirement predicate counts an **absent** `source` as brief (`legacy.py:82-87`), so the census that retires `source_brief` has to learn the new value, and the day-1 anchor rule has to name it explicitly in two places (`day1_gap_gate.py:125, 180`). | |

**What it blocks.** The `walk_start` a population harness states — the replay engine itself never reads `meta.source`; `walk_start` is a stated config key (`intent_replay/config.py:251`) and it is the laboratory join that derives the anchor from `meta.source` (`CLAUDE.md:182`). So this decides what the harness computes, and whether retiring `source_brief` can proceed.

### 6.2 `entry_mode` for a brief tier

**The question.** May a brief-derived geometry ever emit an `"immediate"` rung, or is a candidate geometry pullback-only by construction?

| Answer | What it costs |
|---|---|
| Pullback only; refuse `"immediate"` | Nothing today. Every rung of the real payload sits below `asof_close` — its three `atr_distance` values are 0.50, 2.00 and 3.41, and the field is defined as `(close − limit)/ATR (>0 = below close)` (`thematic/trade_setup/model.py:31`). It forecloses ever measuring a "buy it now" candidate. |
| Allow `"immediate"` | For an immediate rung `limit_price` is the operator's **CAP**, not a pullback level (`schema.py:159-162, 172-175`); `validate_intent` allows at most one and it must be listed first (`immediate_tier_count`, `immediate_tier_not_first`, `validate.py:127-128, 259-272`); and a now-only pick makes the day-1 gap gate not applicable (`day1_gap_gate.py:169-173`). For a measurement, the replay would need the price a drain-time buy actually got, and no brief field carries it — `asof_close` is the prior session's close. |
| Allow it, with a stated fill rule in the run config | Needs a new config key, which is a contract change rather than a caller convenience: an unmodelled key is refused as `unknown_key` precisely so it cannot be ignored (`intent_replay/config.py:150-153`). |

**What it blocks.** Whether the geometry type's rung carries an `entry_mode` field at all.

### 6.3 The size a what-if states

**The question.** What single account-currency amount does a population what-if declare for 916 terminal episodes?

R itself is size-invariant: the replay divides `pnl_cash` by `units × denominator`, and the FX rate cancels (`intent_replay/measures.py:38-45, 129`). The cost gate is not, and neither is the whole-share lattice — the daemon floors every rung to whole shares (`broker_contract/sizing.py:299`) while the replay deliberately does not (`cost_gate.py:27-34`).

| Candidate | What moves with it |
|---|---|
| The owner's own standard position (8 000 PLN per full position since 2026-09-23) | **GAP — this is not in the repository.** The committed templates state `1500.0 EUR` (`examples/manual-pick/pullback-two-tiers.json:10`), and the only "8 000" under `docs/` is Config 2 of the rejected frame table (`broker_sizing_declared_frame_design_2026_08_12.md:116`). The figure comes from the session memory index, not from a committed file. |
| The brief's own `suggested_size_pct` × the declared frame X = 150 PLN per 1 % (operator verdict, LOCKED 2026-08-12, `broker_sizing_declared_frame_design_2026_08_12.md` §7 decision 1) | 8.6219…% → 1 293 PLN for the sample payload. It resurrects the quantity #1467 deleted, because "a percent needs the frame the daemon used to read from its environment" (`legacy.py:123-134`), and it makes the declared size vary per candidate, so the cost gate binds unequally across the panel — the one property a cross-sectional comparison must not have. The historical spread is wide: median 4.62 %, p75 7.75 %, p90 11.74 %, max 25 % over 554 OK setups (`broker_sizing_declared_frame_design_2026_08_12.md:107`). |
| One fixed amount for the whole panel | A number nobody traded, but the only choice that makes the gate bind equally, and the error is measured and bounded: too low by 1.05 bps at 1000, 15.24 at 250, 38.10 at 100, and zero to full precision above the knee of 1250 (`cost_gate.py:27-34`; `tests/intent_replay/test_cost_gate.py:30-32`). |
| The per-pick LIVE amount off the trades record | Exact where it exists, and it does not cover the panel — it exists only for the picks that were actually armed. |

**What it blocks.** Any population run at all: the replay refuses a missing key rather than defaulting (`intent_replay/config.py:144-146`). The choice changes whether a take-profit tranche fires at all, and therefore the realized R of every multi-tranche episode; it changes nothing else, because R's denominator and the excursions are size- and FX-clean.

### 6.4 Whether the geometry type is versioned independently of `TradeIntent`

**The question.** Does a shared geometry type get its own version field, or does it keep reading the one `TradeIntent` constant?

Today the contract is explicit that the two existing fields are one dial, not two: "`meta.schema_version` is the document's version; `spec.schema_version` is the same constant duplicated in a second class, not an independent dial. **Exactly one gate reads it: the door**" (`apps/alphalens-broker-contract/README.md:253-256`; `schema.py:262-266`).

| Answer | What it costs |
|---|---|
| Share `TradeIntent`'s constant | Any additive geometry change bumps the version of every armed document. The door refuses a **stated** mismatch (`schema_version_unsupported`, `commands/broker.py:1594-1600`), and an absent key means current (`README.md:257-260`), so hand-written templates survive while every journal line that states `"3"` stops being re-armable. |
| Version the geometry independently | A third dial beside the two that already exist — and the brief's own `brief_trade_setup.schema_version` is already a fourth, pinned to `{"1.0.0", "1.1.0"}` by `validate_trade_setup` (`paper/sizing.py:49-53`) and stamped from `thematic/trade_setup/model.py:13`. Four dials on one geometry is what the contract's "not two dials" sentence exists to prevent. |
| Retire the brief's own version when the geometry type lands | Collapses four dials to one. But `validate_trade_setup`'s version check is today the only gate that refuses an unreviewed brief shape, and it does so "loudly" by design (`paper/sizing.py:46-53`); removing it before the typed boundary is in place removes the brief side's only check. |

**What it blocks.** Whether the geometry type can ship before this is settled, or must ship with it.

### 6.5 Which MIC a candidate geometry is replayed on

**The question.** A replay derives its calendar, its fee card and its session grid from `instrument.mic`; a candidate geometry carries only a ticker, and the panel has 287 distinct ones.

| Answer | What it costs |
|---|---|
| A constant `XNYS` for every candidate | This is effectively what the lenses do today (`market/calendar.py:80`; the population monitor never passes `exchange=`). It is also the exact error #1714 records as undetectable by inspection: "the calendar asked for XNYS on three XNAS picks (invisible by inspection — the two venues share session times, so a test needs a venue whose hours differ, e.g. XWAR)" (`CLAUDE.md:182`). |
| Resolve per ticker from a committed table | New data to author and keep fresh for 287 tickers. No such resolver exists in `alphalens_pipeline` today — there is no `primary_exchange` field anywhere outside the broker layer, and `saxo_exchanges.py` offers only `ensure_supported_venue(mic)` (`:83`), which validates a MIC it is given. |
| Resolve per ticker at the broker | Exact, and it makes the harness need a deployment — which the replay's own design forbids ("The engine learns nothing about `~/.alphalens`", `CLAUDE.md:182`). |

**What it blocks.** The fee card (selected by venue), the session grid, and the window's right-edge check.

### 6.6 Where `order_ttl_days` lives after the split

**The question.** `order_ttl_days` is on `TradeSpec` today and its unit is "sessions of `instrument.mic`, not calendar days" (`schema.py:248-251`). An instrument-free geometry type cannot name that venue.

| Answer | What it costs |
|---|---|
| Keep it on `TradeSpec`, beside `size` | One more field on the account-bound side that is not account-bound. |
| Put it on the geometry; the composition supplies the venue | The field's stated unit refers to something the type it sits on cannot see. |
| Drop it from both; let the run config state the deadline | The engine's `entry_deadline` is already the stated form of it (`intent_replay/config.py:39-43`), so nothing is lost on the measurement side — but the brief's own `order_ttl_days: 7` then has no home in the geometry type, and the arming door loses the field's `0`-means-default behaviour. |

**What it blocks.** Whether `brief_trade_setup.order_ttl_days` maps onto the geometry type at all.

### 6.7 Whether the derived-level variants survive the migration

**The question.** One of the nine variants computes its levels from a *first* walk's fills, and the replay engine walks a document once.

`replay_ladder_atr_bracket` runs walk-1 to derive the fill set and the anchor, builds the bracket from `trade_setup["atr"]`, then replays a **modified** setup in walk-2 (`feedback/ladder_replay.py:842-871`). Under the `"realised"` anchor the bracket depends on which rungs touched, so it cannot be written as one document. Its `"planned"` twin can: the planned branch calls `planned_blended_entry` over all intended tiers and ignores the fill set (`ladder_replay.py:659-680`), so walk-1 serves only as a no-fill gate — which the replay's own `no_fill` outcome already is. `fill_anchored_0p5atr` and the three `grid_*` variants are also one-document shapes: they change `entry_tiers` or collapse the TP ladder (`breakeven_lenses.py:11, 21-23`), both of which a document states.

| Answer | What it costs |
|---|---|
| Drop `atr_bracket_1p5` (realised anchor) | Loses a pre-registered lens the multiplicity budget has already been charged for — R4: "every registered lens — including retired ones — counts toward the walk-forward's multiplicity budget" (`docs/adr/0013-trade-side-layer-architecture.md:110-112`). |
| Keep it as a two-pass harness in the laboratory that emits a second document | A translation layer, which is the thing the proposal is meant to remove. |
| Re-express it as declared levels per candidate | The ATR bracket stops being a policy and becomes one computed stop and one computed take-profit per episode, which no reader can re-derive from the document alone. |

**What it blocks.** Whether "replace the lenses with `intent-replay`" means all nine variants or eight.

---

## 7. What this memo does not settle

26 facts were looked for and not established. Each is marked **GAP** where it arises; they are collected here so a reader can see the shape of what is unknown without reading every section.

- **(from section 1)** Whether anything OUTSIDE this repository validates `brief_trade_setup`. Every consumer I found is in-tree (pipeline, Django, apps/web). Settled by listing the readers of `~/.alphalens/thematic_briefs/` on the VPS, which I was instructed not to reach.
- **(from section 1)** Whether a `brief_trade_setup` cell has ever actually been malformed in production. I proved nothing would catch it, not that it happened. Settled by running the §1.4 invariant checks (the ones at tests/thematic/test_pre_open_setup.py:46-96) over all published brief parquets rather than over the frozen 94-setup artefact.
- **(from section 1)** Whether `EntryTierSpec.tag`'s stated mirror of "the brief entry tier's free-text label (e.g. 'T1')" was ever true. Today the brief has TWO fields where the contract has one (`tag` = a reason phrase, `label` = the ordinal `E<n>`), and the door fills `T<n>`. Settled by the git history of schema.py:166-171 against model.py:19-21 — I did not run `git log -L` because the worktree is read-only and shared.
- **(from section 1)** Whether `brief_trade_setup` carries a `status` other than `OK`/`NO_STRUCTURE` in any published parquet. model.py declares only those two (model.py:15-16) and the TS union only those two (types.ts:308), but no gate enforces it at write. Settled by a value count over the brief parquets.
- **(from section 2)** Whether the live brief parquets actually carry `builder_config_version`. `model.py:116-122` emits it unconditionally, the SPA interface at `types.ts:306-316` does not declare it, and the real payload quoted in the task does not show it (the quote is abridged with `...`, so this may be elision, not absence). Settles with: `python -c "import pandas,json; print(sorted(json.loads(pandas.read_parquet('<newest brief>.parquet')['brief_trade_setup'].iloc[0])))"` on the VPS, which I was told not to reach.
- **(from section 2)** How many lines of the live `picks.jsonl` (sim and live) are schema 3 versus older. This decides whether a `size` split needs a legacy allowance immediately or can wait. Settles with the existing census, one journal per run: `cd apps/alphalens-research && ALPHALENS_PICKS_JSONL=<picks.jsonl> ../../.venv/bin/python -m unittest tests.trade_intent.test_legacy_register.RetirementCensusOnARealJournal -v`. Needs the VPS journal.
- **(from section 2)** Whether a schema-3 journal line would be SKIPPED or MIS-DECODED after the split. I argued from `codec.py:192-209` (unmodelled keys are dropped with a warning) plus `PickSize` having no default, but I did not run it: I am read-only in this worktree and the task forbids a writing test. Settles with a throwaway script that monkeypatches `TradeSpec` without `size` and calls `intent_from_jsonable` on an example pick.
- **(from section 2)** Whether `/edge` (the Django `edge` app or the SPA) renders `grid_realized_r_json` / `breakeven_realized_r_json` to a user, or only stores them. I found the producer (`population_ladder_monitor.py:1052,1055`) and the research consumers, but no SPA or Django read. Settles with `grep -rn "realized_r" apps/alphalens-django/edge apps/web/src`.
- **(from section 2)** Whether `apps/web/src/lib/api-types.gen.ts` is regenerated by a committed command and gated in CI, or hand-maintained. It types the field as `unknown`, so it survives either way, but the answer decides whether an SPA-side rename is one edit or two. Settles by reading `apps/web/package.json` scripts and the Django parity workflow.
- **(from section 3)** How many journal lines are in the current v3 shape, per environment. The register's own counts are dated (2026-09-11, 2026-09-24) and must not be reused. Settled by running the census on each journal, one at a time: `cd apps/alphalens-research && ALPHALENS_PICKS_JSONL=<picks.jsonl> ../../.venv/bin/python -m unittest tests.trade_intent.test_legacy_register.RetirementCensusOnARealJournal -v`. I could not run it: this task is read-only and the journals live on the VPS (there is no LIVE broker state on the Mac).
- **(from section 3)** Whether a v3 line without a sibling `size` actually raises TradeIntentDecodeError rather than constructing a wrong object. I derived it from `contract_field` without a default making the field required plus codec.py:300-301, but did not execute it. Settled by one throwaway test that decodes a v3 document against the post-split dataclasses.
- **(from section 3)** Whether the `spec` wire key itself survives the split, or the geometry type gets its own key. Gate 2 (`supplied_derived_paths`, codec.py:176-182), the README jq recipe (`.spec.tp_tranches[]?.r_multiple`) and `replay_from_pick`'s `authored_document` all name `spec` by path, so a rename of `spec` costs more than a move of `size`. Settled by the memo stating which of the two the proposal means.
- **(from section 3)** Whether the `intent_invalid` reason vocabulary (including `size_notional_not_positive` and `size_currency_invalid`) is pinned to a README table by a test the way the top-level codes and the replay's reasons are. I confirmed the pins for top-level broker codes (tests/brokers/test_broker_failure_codes_published.py) and for the replay's reasons (tests/intent_replay/test_cli_codes_published.py) but did not find the equivalent for intent_invalid reasons. Settled by grepping the README reason table against INTENT_INVALID_REASONS.
- **(from section 3)** Whether any consumer outside this repository is pinned to the v3 artefact. The compatibility promise is stated as a promise about what the project EMITS (json_schema.py:203-212), and CLAUDE.md says the project has zero external users, so the cost of the bump may be entirely internal. Settled by the owner stating whether the keeper repo split (Stage 1) already has a second reader of these artefacts.
- **(from section 4)** The size of the basis change is unmeasured. No episode has been run on both engines. The 740 episodes carrying the full what-if comparison pair are the natural overlap panel; running them under one declared policy on both engines, over the bars already on disk, would settle it with no vendor spend.
- **(from section 4)** The refusal census over the brief population does not exist. Nothing validates brief_trade_setup today (no JSON schema, no codec fixed point, no validate_intent), so the count of brief setups the arming door would refuse — and under which INTENT_INVALID_REASONS reason — is unknown. Running the engine's door gates alone over the 146 brief parquets, with no walk, would settle it cheaply.
- **(from section 4)** The population bar cache carries no basis column. `BarFetch` never forwards `adjusted` (bar_window.py:44), so the basis is whatever the Polygon client defaulted to at write time, and nothing in the file records it. Settled either by stamping a basis/provenance column in `_write_cached_bars` (population_ladder_monitor.py:536-546) or by refusing to state a basis for a store read.
- **(from section 4)** `entry_trail_bps` for a population run is undetermined. A brief candidate has no trades record and therefore no `watch_open` `d_bps` to read, and `null` replays a ladder production does not use (README.md:23). The study must declare one distance for the whole panel; which one is a decision, not a fact, and belongs in the registration.
- **(from section 4)** The nine what-if columns have no engine equivalent and the memo does not say what replaces the /edge lens selector. The engine replays the one policy a document declares, so a nine-lens comparison becomes nine runs per episode. Whether that is affordable over 916 episodes is unmeasured; an engine run's wall time per episode on cached bars would settle it.
- **(from section 4)** Whether the close-bar inclusion in the old engine materially moved the stored population numbers is unmeasured. CLAUDE.md records the measurement on 40 real pick files and on CRSR; the same comparison has not been run over the 1652 population bar parquets, which is the set the series actually came from.
- **(from section 4)** ADR 0013 R3's exact boundary for an engine migration is unresolved. R3 permits recomputing the lens layer and the 2026-09-12 rebuild moved frozen terminal rows anyway (breakeven_lenses.py:52-55). Whether replacing the headline engine is an R3 exception or an ADR amendment is a decision the memo should name rather than assume.
- **(from section 5)** The owner's standard position of 8 000 PLN is not recorded anywhere in the repository. The committed templates state 1500.0 EUR (`examples/manual-pick/pullback-two-tiers.json:10`) and the only "8 000" under `docs/` is Config 2 of a rejected frame table. Settled by: the owner stating it, and committing it beside the templates or in the contract README's "Writing a manual pick" section.
- **(from section 5)** I could not establish the LIVE account's `ALPHALENS_BROKER_MAX_PICK_NOTIONAL` current value. The repository pins only the boot-assert ceiling of 15 000 (`live_rails.py:112`). Settled by: reading the LIVE unit's EnvironmentFile on the VPS, which this task forbids.
- **(from section 5)** Whether a new population harness would adopt the right-edge window refusal is unsettled, and the two sources read differently. `intent_replay/config.py:42-43` says a run with no deadline "states one past its last bar"; the refusals that enforce a right edge (`vendor_served_short`, `deadline_not_yet_servable`) live in `scripts/replay_from_pick.py:1703-1704`, not in the engine, whose only window check is `check_window_covers` on `walk_start` (`bars.py:266-296`). Settled by: a design decision in the harness, which is a separate question from the geometry split.
- **(from section 5)** I did not measure how many of the 916 terminal episodes are partially filled, which is the exact set on which the lens and replay tranche-allocation rules diverge. Settled by: counting `filled_fraction < 1.0` over `~/.alphalens/population_ladders/`, which needs VPS access.
- **(from section 5)** I did not establish whether a MIC is recoverable for the 287 distinct panel tickers from any store already on disk. Settled by: checking whether `~/.alphalens/grouped_daily_history/` or the Form-4 store carries a primary-exchange field; there is no such field in `alphalens_pipeline` source.

---

## 8. Objections raised while writing this

28 objections to the proposal came up while the sections were being written against the code. They are recorded rather than resolved: a design memo that lists only the arguments for its own proposal is an advocacy document, and this repository reviews memos adversarially before any code is written.

- **(from section 1)** The proposal's framing "six of the seven fields describe the chart" is loose in one place. `schema_version` (schema.py:262-266) describes the WIRE, not the chart; the geometry fields are five (`entry_tiers`, `disaster_stop`, `tp_tranches`, `order_ttl_days`, `side`). The six-versus-one split holds as "not account-bound versus account-bound", which is the claim that matters, and I wrote it that way rather than repeating the chart phrasing.
- **(from section 1)** An account-free geometry type will not be self-contained. `order_ttl_days` counts "sessions of instrument.mic" (schema.py:248-250), and `instrument` is a field of TradeIntent, not of TradeSpec (schema.py:445). Splitting `size` out leaves a type that still cannot be interpreted without the venue it is paired with. Later sections should say whether the geometry type carries the MIC, or whether the TTL moves out with the size.
- **(from section 1)** The brief and the contract disagree on the tier-label vocabulary, and the split will have to pick one. The brief stamps `E<n>` (model.py:19-21) while the arming door fills `T<n>` (schema.py:170); the execution side separately keeps a third copy of the `E<n>` helper with a hand-maintained agreement note (labels.py:10-14). "The brief emits that geometry" means one of these three changes, and changing the door's fill changes what gets journaled.
- **(from section 1)** Two conventions differ in a way that matters for a shared type, not just for a rename. The contract allows `tranche_pct` to sum to under 100 as a deliberate runner (schema.py:191-194); the brief always equal-weights to exactly 100 (builder.py:208-212). And the brief's `disaster_stop` is nullable (model.py:75) where the contract's is a required positive float (validate.py:279-285). A brief emitting the contract type must either refuse to emit a NO_STRUCTURE row at all or carry an optional geometry.
- **(from section 1)** `validate_trade_setup` (paper/sizing.py:30-77) should not be described in later sections as "the brief's validation". It reads `suggested_size_pct`, which is the field the split removes, so it cannot survive the change unmodified; and it never looks at `tp_tranches`, `atr`, `asof_close` or `order_ttl_days`. It is a plannability predicate for the population monitor, run after the row is on disk.
- **(from section 1)** The shape gate is weaker than the memo's gate table suggests. The published input schema sets no `additionalProperties` (0 grep hits in both the generator and the published JSON), so an extra key passes the schema and is only caught two gates later by the fixed point (`key_discarded`, broker.py:1717-1724). Any section that says "the schema refuses what the brief carries" would be wrong; the fixed point refuses it.
- **(from section 2)** The premise "the brief already speaks the engine's language" is false at the field-name level. `entry_tiers[].limit` versus `limit_price` and `tp_tranches[].target` versus `price` are two renames, and six brief fields (`status`, `asof_close`, `atr`, `atr_distance`, `builder_config_version`, `suggested_size_pct`) have no `TradeSpec` counterpart while `entry_mode` has no brief counterpart. The memo should say which side renames, because the answer decides whether the SPA card and the nine what-if variants move.
- **(from section 2)** Removing `TradeSpec.size` is not a free refactor: `tests/trade_intent/test_json_schema.py:757` runs `breaking_changes(committed, live)` against the committed artefact and classifies a removed field and a removed `$def` as breaking, with a failure message that says to bump `SCHEMA_VERSION`. The epic should budget for schema "4": new artefact filenames, a README edit gated by `:783`, and `_assert_version_is_spoken` (`broker.py:1596-1600`) starting to refuse every document that states "3".
- **(from section 2)** The journal drain is deliberately version-blind (`json_schema.py:206-214`), which is usually the relief and here is the risk. `_filtered` drops an unmodelled key with only a WARNING (`codec.py:192-209`), and `PickSize` has no default, so a schema-3 line that loses its `size` path would raise and be skipped by `iter_picks`. Journals are append-only. The split needs a `size_pct_v2`-shaped legacy allowance, its `# LEGACY(...)` marker and its census predicate IN THE SAME COMMIT, or every already-placed pick silently leaves the drain's view.
- **(from section 2)** `broker trades` and `replay_from_pick` read the plan BY PATH off the raw embedded document, not through the codec (`trades.py:814,838`, `replay_from_pick.py:218`). After a split the trades record carries two document shapes, and `SIZE_SHAPES` (`trades.py:243`) has no vocabulary for the new one. This is the same class of hazard #1714 was written to remove, so it deserves naming rather than being left to the implementation PR.
- **(from section 2)** The epic's count of four consumers understates the work by a large factor: 43 production sites read or write `brief_trade_setup`, 26 production modules name `TradeSpec` or `TradeIntent`, 14 of them spell a literal `spec.size` path, and 30 test modules move with `size`. The honest framing is that the generated JSON Schema is one edit, and everything downstream of it is not.
- **(from section 2)** Three research scripts find the setup column by sniffing (`next((c for c in brief.columns if "trade_setup" in c), None)` at `exit_policy_analysis.py:110`, `exit_policy_planning_sd.py:122`, `exit_policy_missingness.py:57`). A column rename leaves them green and reading nothing, or reading the wrong column if a second `*trade_setup*` column ever coexists during a migration. They need explicit column names before any rename, not after.
- **(from section 2)** The shared contract leaf already contains a brief-shaped reader: `exit_geometry/levels.py:16,40,44` takes a `trade_setup` mapping and reads `asof_close`. That is a pre-existing boundary violation of the "the adapter reports, never the contract decides" rule the project states for `broker_contract`, and a geometry split either fixes it or inherits it.
- **(from section 3)** "The brief already speaks the engine's language" is not true at the key level today, and the fixed-point gate is what makes that matter. The brief's own keys are `limit` and `target` (trade_setup/model.py:33, 53) where the contract has `limit_price` and `price` (schema.py:159, 190). The brief additionally emits `status`, `asof_close`, `atr`, `suggested_size_pct`, `builder_config_version`, per-tier `atr_distance` and per-item `label` (model.py:105-126) — of its top-level keys only `disaster_stop`, `order_ttl_days`, `entry_tiers`, `tp_tranches` and the inner `alloc_pct`/`tranche_pct`/`r_multiple`/`tag` match the contract by name. Because gate 9 refuses any key the decoder would drop (codec.py:121-129), a brief payload cannot be handed to the door unchanged. Splitting `size` out removes the SIZE translation; it does not remove the translation layer. The memo should say which of the remaining key renames the split also performs, or state plainly that a projection step survives it.
- **(from section 3)** Option (a) of the journal question drags a second published contract into the change. `_plan_size_shape` (trades.py:813-817) answers from `LEGACY_ALLOWANCES["size_pct_v2"].still_needed`, and `SIZE_SHAPES` is `("by_amount", "by_percent")` (trades.py:243) published as an enum in the trades schema (trades_schema.py:361). A v3 line that is skipped rather than migrated is neither shape, so option (a) needs a third enum value or a new null reason in `alphalens.broker.trades/v1`. Option (b) avoids this entirely.
- **(from section 3)** There is no wire-neutral first step, which the proposal's framing ("leaving an account-free geometry type") may assume. `_node` turns a nested dataclass into a `$ref` (json_schema.py:157-159), so introducing a `TradeGeometry` dataclass that `TradeSpec` composes changes the artefact and reads as breaking under the classifier (every moved leaf is "field removed or renamed"). A geometry type that is wire-neutral has to be structural — a Protocol over the existing flat fields — which is not a wire type and so does not give the brief something to serialize.
- **(from section 3)** The new `size` field must NOT be given a default. A default would make a v3 journal line decode and size the pick at the default rather than refuse it. That is a worse failure than the one `size_pct_v2` exists to prevent, and nothing in the current gates would catch it: the schema gate is at the door only, and the drain is ungated by design (README.md:261-266).
- **(from section 3)** The bump commit is large by force of the pins (§3.5 step 2), and this repository's own rule is that a plan is one ticket, not one epic. If the commit has to be split for review, the only safe cut is between the contract half (schema, artefacts, codec, legacy entry, validate, templates, README) and the replay half (classification rows plus the spec tables) — and even that is only safe if `test_classification.py:374-375` is allowed to be red between the two, which it should not be on a shared branch. The honest answer is that it is one commit; the memo should say so rather than imply a staged landing.
- **(from section 4)** The proposal's phrasing 'the new series will be LOWER' is weaker than it reads, and I have not written it as a settled fact. The cost gate — the most intuitive lowering term — is roughly two orders of magnitude from binding on the quoted payload (TP1 sits 2404 bps above the planned blend against ~50 bps of declared edge plus commission). The terms that do push down are the entry-trail fill price and the close-bar drop, and the repository's own measurement says a worse exit price is not a worse answer (-0.644 R across 91 cut winners against +1.175 R across 43 saved losers, net inside its interval). So no scalar correction bridges the two series, and a memo that implies one would be wrong.
- **(from section 4)** The lens registry is at its declared cap (MAX_REGISTERED_LENSES = 6), and retired lenses still charge the walk-forward multiplicity budget. Any framing of the migration as 'replace nine lenses with one engine' should say that the nine lenses' multiplicity cost is already spent and is not refunded by retiring them. Otherwise a reader will treat the engine's first registered policy as a fresh family of one, which is the self-deception the project's own doctrine names.
- **(from section 4)** Treating the +0.3846 R mean for be_0p5r_trail0p6 as a target the engine should reproduce would be a mistake, and the task text already says so. Those numbers were produced by the engine being retired, over a store whose lens history was REPLACED by a from-scratch rebuild (every lens lost 5 to 34 values it used to hold), with no provenance distinguishing a forward-stamped cell from a backfilled one. An engine number that differs from +0.3846 is not evidence the engine is wrong.
- **(from section 4)** Option (b), cutting at a date, is cheaper than it looks only because nothing today reads `ladder_config_version`. The column is written on every row and no Django view or SPA component consumes it, so a cut would produce pooled means spanning two bases with nothing in the UI to indicate the boundary. If the epic reaches for the cheap option, the reader has to be built first, and that cost belongs with option (b) rather than being discovered after the cut.
- **(from section 4)** The engine's fractional-unit sizing is a recorded scope cut, not a fidelity gain. Against the lens it is neutral (the lens is dimensionless), but against the daemon it FLATTERS, measured at 17.50 / 130.00 / 60.81 on three budgets with the entry anchor moving ~30 bps. A memo arguing the engine is 'the correct way to compute a what-if' should state that the whole-share lattice is still absent and is not reported in `divergences`.
- **(from section 5)** The proposal's quoted +0.3846 R for `be_0p5r_trail0p6` comes from the lens that honours NO entry TTL. Its TTL-honouring twin is a separate registry entry, `be_0p5r_trail0p6_ttl7` (`breakeven_lenses.py:291-318`), and the replay enforces an `entry_deadline` as a first-class config key that cannot be null (`config.py:39-43`). So a migrated run will not reproduce +0.3846 R, and that is correct behaviour rather than a regression. The memo should say so before anyone compares the two numbers.
- **(from section 5)** The two engines allocate take-profit tranches differently on every partial fill, and the memo's framing ("no translation layer") could be read as implying the numbers will match. The lens re-bases a tranche to `(tranche_pct/100)/filled_frac` of the FILLED position (`ladder_replay.py:1096-1119`); the replay sells `min(fraction * intended, held)` with `intended` summed over ALL rungs (`walk.py:652, 968`). The replay therefore realises TP gains on a smaller share. This is the strongest single argument for the split, and it should be stated as an expected divergence, not discovered after the first run.
- **(from section 5)** `broker_contract/sizing.py:140-143` justifies a `__post_init__` refusal by claiming a brief's `tranche_pct` is "LLM-authored and range-checked nowhere (`paper.sizing` parses a bare `float(raw.get("tranche_pct", 0.0))`)". Both halves are now wrong: the trade setup is computed deterministically and the LLM composes only narrative (`thematic/argumentation/schema.py:5-11`), and `paper/sizing.py` no longer contains that parse — it went with the brief producer in #1552. The refusal is still correct; its stated reason is stale. If the epic touches that file, the comment should be corrected rather than carried forward as evidence.
- **(from section 5)** The proposal says the deprecated lenses and their engine "can be replaced" once the brief speaks the engine's language. One of the nine variants cannot be replaced by a single document: `atr_bracket_1p5` under the realised anchor derives its bracket from walk-1's fill set (`ladder_replay.py:842-871`). The memo should say eight of nine, or name the two-pass harness as in scope.
- **(from section 5)** ADR 0013 R4 charges the multiplicity budget for every registered lens "including retired ones" (`0013-trade-side-layer-architecture.md:110-112`), and the 2026-09-25 amendment charges the CLAIM rather than the tool (`:119-126`). Replacing the lenses therefore does not free budget, and re-running the same nine policies through a new engine and quoting the result is a fresh set of charged looks unless it is recorded as exploratory under the three stated conditions. The memo should not present the migration as budget-neutral without that sentence.

---

## 9. Adversarial review, 2026-10-12 — the proposal is refused

This repository reviews a design memo adversarially before any code. The review was run on the
argument above, with the 28 objections of section 8 handed over as already known so the reviewer
spent its effort on what the memo had NOT doubted. Its verdict was to abandon the type split. Each
load-bearing claim below was then checked against the code rather than accepted.

### 9.1 The repository already paid for this exact coupling, and recorded the price

`broker_contract/sizing.py:114-127`, the docstring of `TpTranchePlan.tranche_frac`:

> `tranche_frac` is a FRACTION of the position (0-1), deliberately NOT the same unit as the
> brief-shaped `TpTrancheSpec.tranche_pct`, which is a PERCENTAGE (0-100). The two names differ
> because the units do; **sharing one name is what let a live sizer and a fee estimator read the same
> number 100x apart.**

That is a measured, in-repo incident of the failure mode this proposal invites, and the remedy
chosen at the time was to NOT share the name. It happened inside ONE bounded context. This proposal
would share a type across two that the project separates on purpose, and across the boundary where
the brief is LLM-authored and validated nowhere while the contract is behind four gates.

So the translation step the proposal removes is not waste. It is an anti-corruption layer, and the
100x line is what it was built from.

### 9.2 The seam is an artifact of one field, not a principle

The proposal cuts on "account-bound versus chart-bound" and moves exactly one field. But `TradeSpec`
carries two more fields with no chart meaning, and the proposal leaves both:

| field | what it is | default |
|---|---|---|
| `size` | the account's budget | none (and section 3 insists it must stay that way) |
| `side` | `Literal["long"]`, "a barrier against the client-side Sell->Buy footgun" (`schema.py:257-261`) | `"long"` |
| `order_ttl_days` | sessions of `instrument.mic` — a venue fact, and section 1 already records that the spec cannot self-complete it (`schema.py:248-255`) | `DEFAULT_ORDER_TTL_DAYS` |

Two observations follow, and the second is the sharper one. A principled cut is **levels versus
policy**: `entry_tiers`, `disaster_stop` and `tp_tranches` need only the tape, while `size`, `side`
and `order_ttl_days` each need something off the intent — the account, the direction convention, the
venue. And the decode-at-default hazard section 3 raises for a new `size` field applies identically
to the two policy fields that already carry defaults; the cut removes one instance of that hazard
and leaves two standing.

### 9.3 The stated payoff does not need the change — this is the one that decides it

The memo's main justification is that retiring the duplicate what-if engine needs no translation
layer once the brief speaks the engine's language. That is circular, for a reason the memo's own
section 2 establishes.

A population run needs one `TradeIntent` per candidate. Composing one requires `size`, `instrument`
and `meta` whatever shape the brief emits, so a brief-to-intent generator exists in both designs.
And "the brief already speaks the engine's language" is false at the field-name level today —
`limit` against `limit_price`, `target` against `price`, with `atr_distance` and `label`
unrepresented. A completed split therefore does not remove the adapter; it shortens it.

The engine is retired by producing `TradeIntent`s and calling the real backtester. The split neither
enables nor requires that. Its unique contribution is a shorter adapter, which is a line count and
not an architecture.

### 9.4 The cheap alternative, and it is cheap

Validate `brief_trade_setup` in place: a brief-side clone of the arming door's third gate — a JSON
Schema generated from the dataclass the payload already is, validated on write, plus a codec
round-trip that refuses any key the brief's own decoder would drop. That last half is the important
one, because section 1 records that the published input schema sets no `additionalProperties`, so a
typo arrives as an extra key and is caught by the fixed point rather than by the schema.

Checked for feasibility rather than assumed: the payload is already a frozen dataclass with a
`to_dict` (`alphalens_pipeline/thematic/trade_setup/model.py:67-126`), and `contract_field` is
importable outside the contract package — a research test already imports it
(`tests/trade_intent/test_contract_metadata.py:23`). So the machinery exists on both sides.

What it gives up is the "brief speaks the engine's language" property, which section 9.3 shows is
false today and unnecessary for the retirement.

### 9.5 What this memo recommends instead

1. **Validate `brief_trade_setup` in place**, with the gate-3 clone of 9.4. One file plus its test.
2. **Retire the duplicate what-if engine behind a brief-to-intent adapter**, and point
   `intent-replay` at the population. Measure, after that lands, whether anything the split would
   have bought still remains.
3. **Fix the docstring.** `TradeSpec` calls itself UNSIZED while carrying `size`, and that sentence
   is also the `description` of `properties.spec` in the published schema, so a third-party producer
   reads it. One sentence, not a migration.

And if the split is revived despite 9.1 to 9.3, it must move `order_ttl_days` and `side` out with
`size` under the levels/policy cut, or state in writing why a seam that leaves two policy fields on
an account-free type is tolerated.

### 9.6 What the review did not change

The destination stands. Silent typos in an unvalidated brief are a real defect; the duplicate
engine's slippage-free fill model is a worse instrument than the backtester; and the measurements in
sections 1 to 4 — the field mapping, the 43/26/14/30 site counts, the basis change, the seven open
questions — are reusable by the three changes above. The error was the coupling mechanism, not the
goal.

**GAP:** a second review, by an agent reading this memo against the repository rather than reasoning
on its argument, was still running when this section was written. If it raises a finding that
changes 9.1 to 9.5, that belongs in a further dated section rather than an edit to this one.
