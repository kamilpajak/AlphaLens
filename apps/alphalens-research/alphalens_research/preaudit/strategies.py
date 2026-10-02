"""Short strategy name -> audit script path. The lab's registry, in the lab.

One source of truth for which script runs an audit: adding a strategy means
editing this file, and the pre-audit smoke gate and the ``alphalens audit``
command both read it from here.

It used to live in ``alphalens_cli.commands.audit`` as a private ``_SCRIPTS``,
which the pre-audit runner reached into. That made the lab depend on the
command-line adapter — the one inbound edge into a composition root that
imports from everywhere and is imported by nothing. The registry's contents are
paths to research scripts, so this is where they belong; the CLI now reads it
lazily, the way it already reads the rest of the lab.

The path is resolved inside this app rather than from the workspace root: the
scripts sit two directories up, so the registry no longer has to know how deep
the repository nests it.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["AUDIT_SCRIPTS", "RESEARCH_SCRIPTS_DIR"]

#: ``apps/alphalens-research/scripts`` — this app's own script directory.
#:
#: Assumes an EDITABLE install, which is how the lab is always installed: the
#: scripts are not package data, so a wheel landing in ``site-packages`` would
#: make this point at a directory that does not exist. ``Path`` does not
#: validate, so that failure surfaces when a script is opened, not here. The
#: previous derivation (workspace root, then back down through ``apps/``) had
#: the same assumption and one more level of indirection.
#:
#: Nothing silently depends on it being right: ``tests/test_audit_cli.py``
#: asserts every entry resolves to an existing file, which is what caught a
#: deliberate off-by-one in both directions during review.
RESEARCH_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"

AUDIT_SCRIPTS: dict[str, Path] = {
    "tri_factor": RESEARCH_SCRIPTS_DIR / "experiment_tri_factor_edgar.py",
    "momentum_lowvol": RESEARCH_SCRIPTS_DIR / "experiment_momentum_lowvol_combo.py",
    "constrained_momentum": RESEARCH_SCRIPTS_DIR / "experiment_constrained_momentum.py",
    "constrained_contrarian": RESEARCH_SCRIPTS_DIR / "experiment_constrained_contrarian.py",
    "quality_momentum": RESEARCH_SCRIPTS_DIR / "experiment_quality_momentum_combo.py",
    "longshort_mom_lowvol": RESEARCH_SCRIPTS_DIR / "experiment_longshort_mom_lowvol.py",
    "regime_overlay": RESEARCH_SCRIPTS_DIR / "experiment_regime_overlay.py",
    "vol_target_overlay": RESEARCH_SCRIPTS_DIR / "experiment_vol_target_overlay.py",
    "v7_options_implied": RESEARCH_SCRIPTS_DIR / "experiment_v7_options_implied.py",
    "v8_literature_direct": RESEARCH_SCRIPTS_DIR / "experiment_v8_literature_direct.py",
    "v9_sign_constrained": RESEARCH_SCRIPTS_DIR / "experiment_v9_sign_constrained.py",
    "v9_cross_sectional_residual": RESEARCH_SCRIPTS_DIR
    / "experiment_v9_cross_sectional_residual.py",
    "insider_form4_opportunistic": RESEARCH_SCRIPTS_DIR
    / "experiment_insider_form4_opportunistic.py",
    "insider_pc_compound": RESEARCH_SCRIPTS_DIR / "experiment_insider_pc_compound.py",
    "ev_fcff_yield": RESEARCH_SCRIPTS_DIR / "experiment_ev_fcff_yield.py",
    "pead_pss_v2_2026_05_13": RESEARCH_SCRIPTS_DIR / "experiment_pead_pss_v2.py",
    "idiosyncratic_momentum_2026_05_14_v1": RESEARCH_SCRIPTS_DIR
    / "experiment_idiosyncratic_momentum.py",
}
