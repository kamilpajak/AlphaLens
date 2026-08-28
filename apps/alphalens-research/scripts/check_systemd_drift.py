"""Detect host-side systemd drift for the broker-manager units (#1135).

The repository states what the two broker daemons run (base units + tracked
drop-in directories, #1134/#1136), and ``test_deploy_systemd_units.py`` pins
that statement — against itself. Nothing could see the VPS: the #1121 drift
(values typed into the installed unit) ran for ~2 weeks and was found by hand.
This check runs ON the VPS and answers "did it happen again", on its own.

Contract
--------
* **Detect, never auto-apply.** The rules/Grafana syncs converge the host to
  ``origin/main``; doing that here would let a merge change live trading
  behaviour and would fight an operator's deliberate host-side setting. This
  script only reads, compares, logs, and writes a gauge.
* The repo side of the comparison is the fetched ``origin/main`` BLOB
  (``git show``), never the working tree — a stale or locally-edited checkout
  must not be able to vouch for the host.
* Three signals per unit, each a separate finding kind:
  - ``untracked_file`` / ``missing_file``: the drop-in DIRECTORY listing vs
    the repo's. Name-level on purpose — an untracked file whose values happen
    to match today is still a host governed by something unreadable from the
    repo (the ``zz-oco-disable.conf`` lesson, #1136).
  - ``content_drift``: a tracked file whose host bytes differ from the blob.
    Catches comment drift, value edits, and ExecStart tampering alike. For
    the base unit, ``Environment=`` lines assigning ONLY host-only vars are
    stripped before comparing (the ADR 0017 account-bound grant lines live in
    the installed LIVE unit by design).
  - ``env_drift``: the repo-composed environment (base + tracked drop-ins in
    lexical order, systemd's own precedence) vs what systemd actually LOADED
    (``systemctl --user show -p Environment``). Catches a drifted-but-not-
    daemon-reloaded host and env injected through paths the file checks
    cannot see. Host-only vars are excluded, not reported.
* Drift is a MEASUREMENT, not a job failure: the run exits 0 whenever the
  check completed, and the gauge carries the count (0 written explicitly per
  unit — an absent series must mean "broken emitter", never "no drift").
  Exit 1 is reserved for operational failure (fetch, systemctl, unreadable
  REPO blob), which holds the job's last-success clock still and pages via
  the staleness pair.
* Finding text never embeds host-only values — they are opaque account
  identifiers.

The ``Environment=`` parser here is the promoted single home of the one
``test_deploy_systemd_units.py`` grew for #1134; that file imports these
functions rather than keeping a second copy.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SYSTEMD_DIR = "deploy/systemd"
HOST_UNIT_DIR = Path.home() / ".config" / "systemd" / "user"

# The account-bound LIVE grant (ADR 0017) is host-only by design: it must
# never sit in a public-repo unit file, so its presence on the host is not
# drift. Applied to BOTH units — neither var means anything to the SIM
# daemon, so the allowlist cannot hide a SIM-side change.
HOST_ONLY_VARS = frozenset({"ALPHALENS_SAXO_LIVE_STANDING", "SAXO_LIVE_ACCOUNT_KEY"})

# (unit name, base unit filename). The drop-in directory is <base>.d in both
# the repo and the host layout.
UNITS: tuple[tuple[str, str], ...] = (
    ("alphalens-broker-manager", "alphalens-broker-manager.service"),
    ("alphalens-broker-manager-live", "alphalens-broker-manager-live.service"),
    # The shared price reader (#1172) holds the ONE elevated Saxo session both
    # daemons read. A hand edit here (a different socket path, the session gate
    # flipped) silently changes what every price decision on this host sees, so
    # it belongs under the same detect-never-auto-apply watch as the daemons.
    ("alphalens-saxo-price-reader", "alphalens-saxo-price-reader.service"),
)

METRICS_BASENAME = "alphalens_domain_systemd-drift-check.prom"


@dataclass(frozen=True)
class Finding:
    unit: str
    kind: str  # untracked_file | missing_file | content_drift | env_drift | unreadable_file
    subject: str  # filename or variable name
    detail: str


# --------------------------------------------------------------------------
# The narrow Environment= parser (promoted from test_deploy_systemd_units).
# --------------------------------------------------------------------------


def environment_assignments(text: str) -> dict[str, str]:
    """The ``Environment=`` assignments in one unit or drop-in file.

    systemd's ``Environment=`` takes a space-separated LIST of assignments, so
    one line may carry several; this splits on whitespace rather than reading
    the line as a single pair. That is only sound because a value containing a
    space would have to be QUOTED, and :func:`unreadable_reason` refuses
    quoting.

    Deliberately narrow: no quoting, no line continuations, no bare
    ``Environment=`` reset. Anything this parser cannot read honestly must be
    refused via :func:`unreadable_reason` rather than misread quietly.
    """
    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("Environment="):
            continue
        for assignment in stripped[len("Environment=") :].split():
            key, _, value = assignment.partition("=")
            values[key.strip()] = value.strip()
    return values


def unreadable_reason(assignments: str) -> str | None:
    """Why :func:`environment_assignments` cannot honestly read this
    ``Environment=`` payload, or ``None`` when it can.

    The contract is refusal, not best effort: systemd understands quoting,
    line continuations, C-style escapes and a bare ``Environment=`` reset,
    and this parser understands none of them. Anything it would MISREAD has
    to be rejected outright — a quietly wrong composition is the one failure
    this module exists to prevent. Hence the blanket backslash ban: a
    trailing one continues the line and an escaped space hides a value
    boundary, and neither is worth supporting for files that never use them.
    """
    if "\\" in assignments:
        return "backslash: continuation and C-style escapes are not parsed"
    if '"' in assignments or "'" in assignments:
        return "quoted value is not parsed"
    tokens = assignments.split()
    if not tokens:
        return "bare Environment= reset is not parsed"
    for token in tokens:
        if "=" not in token:
            return f"{token!r} is not an assignment"
    return None


def file_unreadable_reason(text: str) -> str | None:
    """First unreadable ``Environment=`` payload in a whole file, if any."""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("Environment="):
            continue
        reason = unreadable_reason(stripped[len("Environment=") :])
        if reason is not None:
            return reason
    return None


def composed_environment(base_text: str, dropins: list[tuple[str, str]]) -> dict[str, str]:
    """Base unit + drop-ins applied in the given (lexical) order — systemd's
    own precedence, later assignment wins."""
    composed = environment_assignments(base_text)
    for _name, text in dropins:
        composed.update(environment_assignments(text))
    return composed


def strip_host_only_environment_lines(text: str, host_only: frozenset[str] | set[str]) -> str:
    """Drop ``Environment=`` lines whose EVERY assignment targets a host-only
    var. A mixed line stays: dropping it would hide a governed assignment
    behind the allowlist."""
    kept: list[str] = []
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("Environment="):
            assigned = environment_assignments(stripped)
            if assigned and set(assigned) <= set(host_only):
                continue
        kept.append(line)
    return "".join(kept)


# --------------------------------------------------------------------------
# The pure comparison.
# --------------------------------------------------------------------------


def drift_findings(
    unit: str,
    repo_files: dict[str, str],
    host_files: dict[str, str],
    repo_env: dict[str, str],
    live_env: dict[str, str] | None,
    host_only_vars: frozenset[str],
) -> list[Finding]:
    """Compare one unit's repo statement against its host reality.

    ``repo_files`` / ``host_files`` map filename -> text for the base unit
    plus every file in the drop-in directory (the base unit's name is the one
    ending in ``.service``). ``repo_env`` / ``live_env`` are the composed and
    the systemd-loaded environments respectively. ``live_env=None`` means the
    loaded environment could not be HONESTLY read (systemctl rendered a form
    the narrow parser refuses, e.g. a quoted value): that is one finding, not
    a license to fabricate per-variable diffs from a shredded parse.

    A host file that is BOTH unreadable and byte-different reports both
    findings deliberately. The grant-line strip is conservative on an
    unreadable line (garbage-parsed keys are never all host-only, so the line
    is kept and the raw bytes compare), and suppressing content_drift there
    would hide a real byte change behind the weirdness; the co-emitted
    unreadable_file finding is what marks the file untrusted.
    """
    findings: list[Finding] = []

    for name in sorted(set(host_files) - set(repo_files)):
        findings.append(Finding(unit, "untracked_file", name, "host file the repo does not track"))
    for name in sorted(set(repo_files) - set(host_files)):
        findings.append(Finding(unit, "missing_file", name, "tracked file absent on the host"))

    for name in sorted(set(repo_files) & set(host_files)):
        repo_text, host_text = repo_files[name], host_files[name]
        if name.endswith(".service"):
            host_text_cmp = strip_host_only_environment_lines(host_text, host_only_vars)
        else:
            host_text_cmp = host_text
        if host_text_cmp != repo_text:
            findings.append(
                Finding(unit, "content_drift", name, "host bytes differ from the origin/main blob")
            )
        reason = file_unreadable_reason(host_text)
        if reason is not None:
            findings.append(Finding(unit, "unreadable_file", name, reason))

    if live_env is None:
        findings.append(
            Finding(
                unit,
                "unreadable_file",
                "systemd:Environment",
                "the loaded Environment property uses a form the parser refuses",
            )
        )
        return findings

    repo_cmp = {k: v for k, v in repo_env.items() if k not in host_only_vars}
    live_cmp = {k: v for k, v in live_env.items() if k not in host_only_vars}
    for var in sorted(set(repo_cmp) | set(live_cmp)):
        repo_value, live_value = repo_cmp.get(var), live_cmp.get(var)
        if repo_value == live_value:
            continue
        findings.append(
            Finding(
                unit,
                "env_drift",
                var,
                f"repo composes {repo_value!r}, systemd loaded {live_value!r}",
            )
        )
    return findings


def render_metrics(findings_per_unit: dict[str, int]) -> str:
    lines = [
        "# HELP alphalens_systemd_drift_findings Divergences between the repo-declared and host systemd state (0 = converged).",
        "# TYPE alphalens_systemd_drift_findings gauge",
    ]
    for unit, count in sorted(findings_per_unit.items()):
        lines.append(f'alphalens_systemd_drift_findings{{unit="{unit}"}} {count}')
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# VPS-facing IO — thin shells around the pure functions above.
# --------------------------------------------------------------------------


def _run(argv: list[str], timeout: int = 120) -> str:
    return subprocess.run(
        argv, cwd=REPO_ROOT, check=True, capture_output=True, text=True, timeout=timeout
    ).stdout


def _repo_files(base_name: str) -> dict[str, str]:
    # Every blob in the tracked drop-in directory, README included — name
    # parity has to cover non-.conf files too, or a host README could neither
    # flag as untracked nor be content-checked.
    files = {base_name: _run(["git", "show", f"origin/main:{SYSTEMD_DIR}/{base_name}"])}
    dropin_dir = f"{SYSTEMD_DIR}/{base_name}.d"
    listing = _run(["git", "ls-tree", "--name-only", f"origin/main:{dropin_dir}"])
    for name in listing.split():
        files[name] = _run(["git", "show", f"origin/main:{dropin_dir}/{name}"])
    return files


def _host_files(base_name: str) -> dict[str, str]:
    files: dict[str, str] = {}
    base_path = HOST_UNIT_DIR / base_name
    if base_path.is_file():
        files[base_name] = base_path.read_text()
    dropin_dir = HOST_UNIT_DIR / f"{base_name}.d"
    if dropin_dir.is_dir():
        for path in sorted(dropin_dir.iterdir()):
            # Every regular file counts, whatever its suffix: an inert
            # `.disabled` note is still untracked host state. README.md is the
            # one tracked non-.conf file and compares like any other.
            if path.is_file():
                files[path.name] = path.read_text()
    return files


def _live_environment(unit: str) -> dict[str, str] | None:
    """The systemd-loaded environment, or ``None`` when systemctl renders a
    form (quoting, escapes) the narrow parser would shred into phantom
    variables — reported upstream as one unreadable finding."""
    out = _run(["systemctl", "--user", "show", unit, "-p", "Environment"])
    payload = out.strip().removeprefix("Environment=")
    if not payload:
        return {}
    if unreadable_reason(payload) is not None:
        return None
    return environment_assignments(f"Environment={payload}")


def _write_metrics(text: str) -> None:
    directory = os.environ.get("ALPHALENS_TEXTFILE_DIR")
    if not directory:
        print("ALPHALENS_TEXTFILE_DIR unset — metrics not written", file=sys.stderr)
        return
    target = Path(directory) / METRICS_BASENAME
    tmp = target.with_suffix(".prom.tmp")
    tmp.write_text(text)
    tmp.replace(target)


def main() -> int:
    try:
        _run(["git", "fetch", "--quiet", "origin", "main"], timeout=180)
    except (subprocess.SubprocessError, OSError) as exc:
        print(f"fetch_failed: {exc}", file=sys.stderr)
        return 1

    counts: dict[str, int] = {}
    all_findings: list[Finding] = []
    for unit, base_name in UNITS:
        try:
            repo_files = _repo_files(base_name)
            host_files = _host_files(base_name)
            dropins = sorted((n, t) for n, t in repo_files.items() if n.endswith(".conf"))
            repo_env = composed_environment(repo_files[base_name], dropins)
            live_env = _live_environment(unit)
        except (subprocess.SubprocessError, OSError) as exc:
            print(f"check_failed unit={unit}: {exc}", file=sys.stderr)
            return 1
        findings = drift_findings(unit, repo_files, host_files, repo_env, live_env, HOST_ONLY_VARS)
        counts[unit] = len(findings)
        all_findings.extend(findings)

    for f in all_findings:
        print(f"DRIFT unit={f.unit} kind={f.kind} subject={f.subject}: {f.detail}")
    if not all_findings:
        print(f"converged: {', '.join(u for u, _ in UNITS)} match origin/main")

    _write_metrics(render_metrics(counts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
