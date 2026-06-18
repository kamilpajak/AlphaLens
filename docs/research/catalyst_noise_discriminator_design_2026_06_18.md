# Catalyst noise discriminator — source-based entity-less gate

**Status:** LOCKED (shipped) — refines PR #630.
**Date:** 2026-06-18
**Scope:** `catalyst_resolver.find_trigger_event` eligibility for entity-less events.

## Problem

PR #630 (`_filter_entityless_events`) dropped **every** catalyst event whose LLM-extracted
`primary_entities` is empty. It killed the original bug (a Chinese state-media "build a tech
power" piece, `primary_entities=[]`, became the catalyst for BAH/PSN/AVAV under theme
`national_strategy`) but "entity-less" is a poor proxy for "noise".

## Data reframe (June 2026 production)

Of ~859 substantive entity-less events that reach the gate:
- **~97% are reputable English journalism** — RSS: MarketWatch (191), TechCrunch (74), The Verge
  (72), Wired (64), FT (51), Ars Technica (78), Seeking Alpha (49); GDELT-EN: Yahoo Finance, Forbes,
  CNBC, PR Newswire. These are legit macro/regulatory/industry pieces that simply name no single
  company.
- **The actual propaganda is tiny + structurally identifiable**: ~10-25 events, ~100% GDELT, almost
  all Chinese-language, on a small set of CCP domains (voc.com.cn, 163.com, sina.com.cn, …),
  `sourcecountry == "China"`.

Measured collateral damage from #630: theme `social_media_regulation` lost its catalyst on
2026-06-16/17, dropping SNAP (+MITK) over a real **English TechCrunch** article ("countries moving to
ban social media for children"). #630 paid ~97% false-drop to remove a ~3% bad set.

`language` / `sourcecountry` / `domain` live in the news `extra` JSON and are populated **only for
GDELT** (RSS/Polygon/EDGAR carry none).

## Options weighed (research workflow + Perplexity)

- **(A) default-allow + propaganda blocklist** — flip to allow entity-less, subtract a small
  state-media domain/country set. Keeps ~96-98% legit, 0 propaganda. **Chosen.**
- **(B) default-drop + trusted-source allowlist** — worst legit recall (353-603/859) + open-ended
  upkeep; literally the architecture that caused #630. **Rejected.**
- **(C) LLM relevance/quality label** — unscored, model-drift risk, solves at cost what a free
  deterministic rule solves at zero. **Deferred** (possible future shadow-mode `p_catalyst`).

### Language-arm rejected (user decision)

An early variant of (A) dropped entity-less when `language != English`. Rejected: that drops legit
foreign-language EU journalism (German/French/Polish/Norwegian/Taiwan). The discriminator is the
**SOURCE (state media), not the language** — a German Handelsblatt regulatory piece is legit; a
Chinese voc.com.cn propaganda piece is not. (Consistent with the earlier rejection of language-filtering
entity-RICH foreign news, which is high-signal — Chinese NVDA/TSM, Korean Intel/AMD.)

## Final rule

- **Entity-rich** events (`len(_entity_set(row)) >= 1`): always eligible — never touched.
- **Entity-less** events: eligible **by default**, dropped only when the GDELT `domain` is in
  `state_media_domain_blocklist` **OR** the GDELT `sourcecountry` is in `state_media_countries`
  ({China, Russia, Iran, North Korea, Belarus}). **No language arm.** RSS/Polygon/EDGAR entity-less
  always pass (no domain/country fields).
- Domain match is host-exact or registrable-suffix (never substring: `rt.com` ≠ `report.com`).
- Config in `config/catalyst_noise_filters.yaml` (`state_media_domain_blocklist` seeded from US State
  Dept foreign-mission PRC designations + EU/US-sanctioned Russian outlets + observed CCP domains;
  `state_media_countries`). GDELT spelling verified live (full English names, bare-host domains).

## Residual risks / follow-ups

- **Country backstop spelling** — China/Russia verified live; Iran/NK/Belarus are fail-open
  plausible-spelling backstops (a mismatch silently keeps the event; domain blocklist covers the
  majors). Verify if one recurs.
- **Observability** — drop is count-logged with a domain-vs-country split; a structured Prometheus
  counter is a deferred follow-up (shared with #630).
- **SEO-spam English domains** (dailypolitical, yourdemocracy, themarketsdaily) pass (English, US,
  non-propaganda, display-only — no portfolio harm). Left to the existing URL blocklist if one recurs.
- **Commercial Chinese portals** (163.com/sina/ifeng/tmtpost) are blocklisted as observed noise, not
  strict state media; legit Chinese business news from them is usually entity-rich → bypasses anyway.
