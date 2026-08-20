"""Hermetic stubs for the two-stage map-themes path.

``orchestrator.map_themes`` makes TWO kinds of LLM call per theme now: one
stage-A proposal and one stage-B assessment per in-bracket candidate. Every
existing map-themes test patched only the proposal, so without this helper they
reach OpenRouter for real — a live network call inside the default
``unittest discover`` run, and a real bill.

Use :func:`stub_assessor` in ``setUp`` (auto-undone via ``addCleanup``) or
:func:`patch_assessor` as a context manager. The default answer is
``not_established``, chosen because it is the assessment outcome that used to
DROP a candidate: a test that silently starts filtering on the channel would go
red here rather than passing on a lucky ``established``.
"""

from __future__ import annotations

from unittest import mock

from alphalens_pipeline.thematic.mapping import channel_assessor


def assessment(
    status: str = channel_assessor.SUPPORT_NOT_ESTABLISHED,
) -> channel_assessor.ChannelAssessment:
    """One deterministic assessment, no LLM involved."""
    scored = status in (
        channel_assessor.SUPPORT_ESTABLISHED,
        channel_assessor.SUPPORT_SUGGESTIVE,
    )
    return channel_assessor.ChannelAssessment(
        support_status=status,
        channel_type="customer_demand" if scored else "none",
        text="the event states x -> demand shifts -> revenue moves" if scored else "",
        evidence="the event states x" if scored else "",
        falsifier="the 10-K names no such customer" if scored else "",
        confidence=0.5 if scored else None,
        votes=channel_assessor._ASSESS_VOTES,
        valid_n=channel_assessor._ASSESS_VOTES if scored else 0,
        support_dispersion=0,
        outcome=channel_assessor.AssessmentOutcome.SUCCESS,
        assessed_at="2026-08-19T00:00:00+00:00",
    )


def patch_assessor(status: str = channel_assessor.SUPPORT_NOT_ESTABLISHED):
    """Patch ``assess_candidates`` to answer one result per input, in order."""

    def _fake(*, candidates, **_kwargs):
        return [assessment(status) for _ in candidates]

    return mock.patch.object(
        channel_assessor, "assess_candidates", side_effect=_fake, autospec=True
    )


def stub_assessor(testcase, status: str = channel_assessor.SUPPORT_NOT_ESTABLISHED):
    """Start :func:`patch_assessor` for the lifetime of one test."""
    patcher = patch_assessor(status)
    patcher.start()
    testcase.addCleanup(patcher.stop)


def theme_proposal(
    *,
    proposed=None,
    candidates=None,
    verdicts=None,
    in_bracket=None,
    keywords=("kw",),
    outcome=None,
    decline_reason="",
):
    """Build a :class:`orchestrator.ThemeProposal` for a stubbed stage A.

    ``candidates`` defaults to ``proposed`` (i.e. everything in bracket), which
    is what most tests want; pass both to model a bracket drop.
    """
    from alphalens_pipeline.thematic.mapping import orchestrator, theme_mapper

    proposed = list(proposed or [])
    candidates = list(candidates if candidates is not None else proposed)
    if in_bracket is None:
        in_bracket = {c["ticker"]: 1_000_000_000.0 for c in candidates}
    if verdicts is None:
        from alphalens_pipeline.thematic.verification import mcap_filter

        verdicts = [
            mcap_filter.McapVerdict(
                ticker=c["ticker"],
                market_cap=in_bracket.get(c["ticker"]),
                verdict=(
                    mcap_filter.IN_BRACKET if c["ticker"] in in_bracket else mcap_filter.TOO_BIG
                ),
            )
            for c in proposed
        ]
    if outcome is None:
        outcome = (
            theme_mapper.MapperOutcome.SUCCESS if proposed else theme_mapper.MapperOutcome.DECLINED
        )
    return orchestrator.ThemeProposal(
        proposed=proposed,
        verdicts=list(verdicts),
        candidates=candidates,
        in_bracket=dict(in_bracket),
        keywords=list(keywords),
        outcome=outcome,
        decline_reason=decline_reason,
    )
