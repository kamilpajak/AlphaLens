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
* One INVERTED signal, on its own gauge (#1193):
  - ``missing_grant``: the ADR 0017 account-bound grant is host-only by
    construction, so every comparison above is structurally BLIND to it —
    wiping it off the host makes the two sides agree perfectly and the check
    reports ``converged``. That happened twice (2026-08-25, 2026-08-28), the
    second time undetected. Only a positive assertion — these names must be
    PRESENT, in the host config AND in what systemd loaded — can see it.
    Asserted for the units that DECLARE a requirement in :data:`UNITS`, on
    PRESENCE not location, so the host can move the grant from the base unit
    into an untracked drop-in without a window in which this alerts.
    It carries its OWN gauge and alert deliberately: the drift alert's remedy
    is "reinstall the tracked files", which is exactly what causes this.
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

# The account-bound LIVE grant (ADR 0017). One pair, two roles:
#   * host-only — it must never sit in a public-repo unit file, so its
#     presence on the host is not drift. Applied to BOTH units: neither var
#     means anything to the SIM daemon, so the allowlist cannot hide a
#     SIM-side change.
#   * REQUIRED on the LIVE unit (#1193) — without it the daemon cannot
#     construct a real-money client at its next start.
# Deliberately ONE constant behind both, so the "never compare these" list and
# the "these must exist" list cannot drift apart.
LIVE_GRANT_VARS = frozenset({"ALPHALENS_SAXO_LIVE_STANDING", "SAXO_LIVE_ACCOUNT_KEY"})
HOST_ONLY_VARS = LIVE_GRANT_VARS

# (unit name, base unit filename, host-only vars this unit REQUIRES). The
# drop-in directory is <base>.d in both the repo and the host layout. The
# requirement is a third TUPLE FIELD rather than a lookup table keyed by unit
# name: a parallel table can silently lose an entry on a rename, and a check
# that can rot to always-green is the one failure this module exists to
# prevent.
UNITS: tuple[tuple[str, str, frozenset[str]], ...] = (
    ("alphalens-broker-manager", "alphalens-broker-manager.service", frozenset()),
    ("alphalens-broker-manager-live", "alphalens-broker-manager-live.service", LIVE_GRANT_VARS),
    # The shared price reader (#1172) holds the ONE elevated Saxo session both
    # daemons read. A hand edit here (a different socket path, the session gate
    # flipped) silently changes what every price decision on this host sees, so
    # it belongs under the same detect-never-auto-apply watch as the daemons.
    ("alphalens-saxo-price-reader", "alphalens-saxo-price-reader.service", frozenset()),
    # The LIVE balance reader (#1203). It places nothing, but it carries the
    # nine rails because `assert_live_rails` gates EVERY live client, and one of
    # them (EXIT_POLICY) is resolved against the live policy registry at boot —
    # a rename there would kill the balance read silently, before the gauge is
    # written. It REQUIRES the grant for the same reason the daemon does: no
    # grant, no client, no reading, at its next fire.
    ("alphalens-broker-capital-reader", "alphalens-broker-capital-reader.service", LIVE_GRANT_VARS),
)

METRICS_BASENAME = "alphalens_domain_systemd-drift-check.prom"

# The shared credentials file every broker unit sources. systemd applies
# `EnvironmentFile=` AFTER in-unit `Environment=` lines, so one arming line
# here overrides every reader's explicit ALLOW_ORDERS=0 at its next start —
# which is why the broker-rail ban on this file (#1209) is watched hourly.
ENV_FILE_PATH = Path("/etc/alphalens/env")
# Synthetic key on the drift gauge for env-file findings — NOT a systemd unit.
# Riding alphalens_systemd_drift_findings means the existing
# AlphalensSystemdUnitDrift alert pages with zero new rules.
ENV_FILE_UNIT_LABEL = "etc-alphalens-env"
# Trailing underscore on purpose: ALPHALENS_BROKER_* are the nine rails plus
# ALLOW_ORDERS; ALPHALENS_BROKERAGE_-style names are not rails.
BANNED_ENV_NAME_PREFIX = "ALPHALENS_BROKER_"


@dataclass(frozen=True)
class Finding:
    unit: str
    kind: str  # untracked_file | missing_file | content_drift | env_drift | unreadable_file | banned_env_var
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
            values[key.strip()] = expand_specifiers(value.strip())
    return values


# systemd specifiers this comparison understands. `%h` is the invoking user's
# home; `%%` is a literal percent. Both sides of the comparison must mean the
# same string, and `systemctl show -p Environment` reports the EXPANDED value,
# so the repo side has to expand too — #1172 put `%h` into an Environment=
# value for the first time and the raw comparison reported permanent drift on
# a converged host.
#
# Deliberately a SHORT list rather than a general expander: every other
# specifier is refused (below) instead of guessed, matching this module's
# refuse-rather-than-misread contract. `%h` resolves from the running user, so
# the check is only meaningful run AS the unit's user — already true, since it
# reads that user's ~/.config/systemd/user.
_SUPPORTED_SPECIFIERS = {"h": lambda: str(Path.home())}


def expand_specifiers(value: str) -> str:
    """Expand the systemd specifiers this module supports; leave the rest.

    An unsupported specifier is NOT expanded here — it is reported by
    :func:`unreadable_reason`, so it surfaces as "cannot measure" rather than
    as a wrong comparison."""
    out: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char != "%" or index + 1 >= len(value):
            out.append(char)
            index += 1
            continue
        specifier = value[index + 1]
        if specifier == "%":
            out.append("%")
        elif specifier in _SUPPORTED_SPECIFIERS:
            out.append(_SUPPORTED_SPECIFIERS[specifier]())
        else:
            out.append(char + specifier)  # left for unreadable_reason to refuse
        index += 2
    return "".join(out)


def _unsupported_specifier(assignments: str) -> str | None:
    index = 0
    while index < len(assignments) - 1:
        if assignments[index] == "%":
            specifier = assignments[index + 1]
            if specifier != "%" and specifier not in _SUPPORTED_SPECIFIERS:
                return f"%{specifier}"
            index += 2
            continue
        index += 1
    return None


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
    unsupported = _unsupported_specifier(assignments)
    if unsupported is not None:
        return f"{unsupported} specifier is not expanded — comparing it as text would be wrong"
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


def is_host_only_grant_dropin(text: str, host_only: frozenset[str] | set[str]) -> bool:
    """True when this drop-in does NOTHING but assign host-only vars.

    The tolerance for the one untracked file the host is expected to carry
    (#1193): the operator-local grant drop-in, which a unit-file ``cp``
    cannot reach and which therefore survives every future deploy. Same rule
    :func:`strip_host_only_environment_lines` already applies to the base
    unit, extended to one more place.

    A CONTENT contract, not a basename allowlist — a name-based exception
    would let any future file called ``99-live-grant.conf`` govern the daemon
    unseen, which is the ``zz-oco-disable.conf`` lesson (#1136) this module
    exists to keep. Judging by the assigned VARIABLES alone is the trap that
    makes the content check necessary: grant-only ``Environment=`` lines PLUS
    an ``ExecStart=`` override would sail through it. So every non-blank,
    non-comment line has to be accounted for, and an unreadable payload is
    refused rather than waved past.
    """
    assigned = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";")) or stripped == "[Service]":
            continue
        if not stripped.startswith("Environment="):
            return False
        if unreadable_reason(stripped[len("Environment=") :]) is not None:
            return False
        names = set(environment_assignments(stripped))
        if not names or not names <= set(host_only):
            return False
        assigned = True
    return assigned


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
        # The one expected untracked file: the operator-local grant drop-in
        # (#1193). Tolerated by CONTENT — anything it governs beyond the
        # host-only vars still flags.
        if is_host_only_grant_dropin(host_files[name], host_only_vars):
            continue
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
        # BOTH sides, not just the host. A repo blob carrying a form this
        # parser refuses (an unsupported specifier, say) would otherwise be
        # composed and compared as literal text — a confidently wrong answer,
        # strictly worse than a loud finding. The host-side check alone only
        # covers it while the two texts happen to agree.
        for side, text in (("host", host_text), ("repo", repo_text)):
            reason = file_unreadable_reason(text)
            if reason is not None:
                findings.append(Finding(unit, "unreadable_file", name, f"{side}: {reason}"))

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


def grant_findings(
    unit: str,
    host_env: dict[str, str],
    live_env: dict[str, str] | None,
    required: frozenset[str],
) -> list[Finding]:
    """The inverted assertion: the required host-only names must be PRESENT.

    Both sides are judged because they answer different questions. The host
    configuration predicts the NEXT start; the loaded environment describes
    the current one. A wipe that has not been ``daemon-reload``ed yet shows
    up only in the first — and that is the dangerous window, because the
    running daemon keeps trading on its exec-time environment and nothing
    looks wrong until it restarts.

    ``live_env=None`` (systemctl rendered a form the narrow parser refuses)
    silences only the systemd half. The file half must still run: it is the
    predictive one, and letting an unreadable property suppress it would
    reintroduce exactly the blindness this function removes.

    An EMPTY value counts as absent. ``Environment=SAXO_LIVE_ACCOUNT_KEY=``
    sets the name to the empty string, so a presence-by-name test would call
    that grant healthy while ``_standing_grant_valid`` (which requires present
    AND non-empty AND equal) refuses at the next start — a false green on the
    exact question this gauge answers. Emptiness is the most of that rule this
    can mirror: equality would mean comparing the values, and the values must
    never be read here.

    Presence, never VALUES: the grant is an opaque account identifier and no
    finding text may carry it.
    """
    findings: list[Finding] = []
    for var in sorted(required):
        absent_from: list[str] = []
        if not host_env.get(var):
            absent_from.append("the host unit configuration")
        if live_env is not None and not live_env.get(var):
            absent_from.append("the systemd-loaded environment")
        if not absent_from:
            continue
        findings.append(
            Finding(
                unit,
                "missing_grant",
                var,
                f"absent or empty in {' and '.join(absent_from)} — without the ADR 0017 grant this "
                "instance refuses to construct a real-money client at its NEXT start",
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


def render_grant_metrics(present_per_unit: dict[str, bool]) -> str:
    """A gauge of its OWN, not a contribution to the drift count (#1193).

    The drift alert tells the operator to reinstall the tracked files, which
    is precisely what wipes the grant — routing this through it would hand
    out the remedy that caused the failure. Only units that DECLARE a
    requirement get a sample: a `1` for the SIM daemon would be meaningless,
    while an explicit `0` for the LIVE one is the point (an absent series must
    mean "broken emitter", never "the grant is fine").
    """
    lines = [
        "# HELP alphalens_systemd_live_grant_present The ADR 0017 account-bound grant is present for this unit (1 = present).",
        "# TYPE alphalens_systemd_live_grant_present gauge",
    ]
    for unit, present in sorted(present_per_unit.items()):
        lines.append(f'alphalens_systemd_live_grant_present{{unit="{unit}"}} {int(present)}')
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
    # Not every unit HAS a tracked drop-in directory — the shared price reader
    # (#1172) has none. Ask the parent listing first rather than letting
    # `git ls-tree` on a non-existent path exit 128: that raised out of here,
    # reported check_failed, and exited 1 — which this script reserves for
    # "could not measure", so it stalled the job's last-success clock and
    # would page through the staleness pair. Probing the parent also keeps a
    # GENUINE git failure (unreadable repo) propagating, which catching an
    # exit code would have blurred. `_host_files` guards the same case with
    # `is_dir()`; this is the repo half of that symmetry.
    tracked = _run(["git", "ls-tree", "--name-only", f"origin/main:{SYSTEMD_DIR}"]).split()
    # The sibling timer (#1207): a scheduling-stanza edit on the host is drift
    # like any other (the #1206 dormancy was exactly that), so the timer rides
    # the same byte compare. It is a FILE here and nothing more — never fed to
    # env composition (`_dropin_texts` keeps `.conf` only).
    timer_name = base_name.removesuffix(".service") + ".timer"
    if timer_name in tracked:
        files[timer_name] = _run(["git", "show", f"origin/main:{SYSTEMD_DIR}/{timer_name}"])
    if f"{base_name}.d" not in tracked:
        return files
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
    # Host half of the sibling-timer coverage (#1207) — see `_repo_files`. A
    # tracked timer missing here reports missing_file (a never-installed timer
    # is exactly the state that precedes a #1206-style silent dormancy).
    timer_path = HOST_UNIT_DIR / (base_name.removesuffix(".service") + ".timer")
    if timer_path.is_file():
        files[timer_path.name] = timer_path.read_text()
    dropin_dir = HOST_UNIT_DIR / f"{base_name}.d"
    if dropin_dir.is_dir():
        for path in sorted(dropin_dir.iterdir()):
            # Every regular file counts, whatever its suffix: an inert
            # `.disabled` note is still untracked host state. README.md is the
            # one tracked non-.conf file and compares like any other.
            if path.is_file():
                files[path.name] = path.read_text()
    return files


def _dropin_texts(files: dict[str, str]) -> list[tuple[str, str]]:
    """The drop-in files systemd would apply, in its lexical order.

    Only ``.conf`` counts: a tracked ``README.md`` is compared as a FILE (so a
    host copy cannot diverge unseen) but is not configuration.
    """
    return sorted((name, text) for name, text in files.items() if name.endswith(".conf"))


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


def env_file_findings(text: str) -> list[Finding]:
    """The broker-rail ban on the shared credentials file (#1209).

    Names only, never values: the file holds secrets, so the scan looks at
    the part of each assignment line BEFORE the first ``=`` and nothing else.
    Banned: every ``ALPHALENS_BROKER_*`` name, plus the ADR 0017 grant pair —
    in the 0600 per-unit drop-in the grant arms ONE unit; here it would grant
    all readers of the file (the LIVE unit header is the authority: "NEVER
    add any ALPHALENS_BROKER_*, ALPHALENS_SAXO_LIVE_STANDING, or
    SAXO_LIVE_ACCOUNT_KEY line to the shared /etc/alphalens/env").

    Blank lines, ``#``/``;`` comments and lines without ``=`` cannot set a
    variable, so they cannot arm anything and are ignored. An ``export ``
    prefix is stripped CONSERVATIVELY: systemd sets nothing for such a line
    (verified empirically on the host, systemd 255 — it neither strips the
    prefix nor accepts the spaced key), but a dotenv-habituated hand edit
    could write one, and a rail-shaped line in the secrets file deserves a
    page even while inert. Same conservatism covers a banned name absorbed
    into a previous line's backslash-continued VALUE (sets nothing; still
    flagged). Key-side continuation was probed too: systemd drops the split
    line entirely, so a name cannot be smuggled across two lines past this
    scan.
    """
    findings: list[Finding] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";")) or "=" not in stripped:
            continue
        name = stripped.split("=", 1)[0].strip().removeprefix("export ").strip()
        if name.startswith(BANNED_ENV_NAME_PREFIX) or name in LIVE_GRANT_VARS:
            findings.append(
                Finding(
                    ENV_FILE_UNIT_LABEL,
                    "banned_env_var",
                    name,
                    "a broker rail or LIVE grant in the shared credentials "
                    "file overrides every unit's Environment= at its next "
                    "start (EnvironmentFile= wins) — the one arming surface "
                    "is the LIVE unit's 10-allow-orders.conf drop-in; remove "
                    "this line",
                )
            )
    return findings


def _read_env_file() -> str:
    return ENV_FILE_PATH.read_text()


def main() -> int:
    try:
        _run(["git", "fetch", "--quiet", "origin", "main"], timeout=180)
    except (subprocess.SubprocessError, OSError) as exc:
        print(f"fetch_failed: {exc}", file=sys.stderr)
        return 1

    counts: dict[str, int] = {}
    grant_present: dict[str, bool] = {}
    all_findings: list[Finding] = []
    all_grant_findings: list[Finding] = []
    for unit, base_name, required in UNITS:
        try:
            repo_files = _repo_files(base_name)
            host_files = _host_files(base_name)
            repo_env = composed_environment(repo_files[base_name], _dropin_texts(repo_files))
            # `.get`, not `[...]`: a host with no installed unit at all is a
            # real state (reported as missing_file plus a missing grant), and
            # a KeyError here is outside the caught pair below — it would kill
            # the job with a traceback instead of measuring.
            host_env = composed_environment(
                host_files.get(base_name, ""), _dropin_texts(host_files)
            )
            live_env = _live_environment(unit)
        except (subprocess.SubprocessError, OSError) as exc:
            print(f"check_failed unit={unit}: {exc}", file=sys.stderr)
            return 1
        findings = drift_findings(unit, repo_files, host_files, repo_env, live_env, HOST_ONLY_VARS)
        counts[unit] = len(findings)
        all_findings.extend(findings)
        if required:
            missing = grant_findings(unit, host_env, live_env, required)
            grant_present[unit] = not missing
            all_grant_findings.extend(missing)

    # The env-file ban (#1209). Unreadable = cannot verify the invariant =
    # the reserved exit 1 — in practice near-unreachable, because this very
    # unit sources the file via a no-leading-dash EnvironmentFile= and would
    # fail before ExecStart.
    try:
        env_findings = env_file_findings(_read_env_file())
    except OSError as exc:
        print(f"check_failed env_file: {exc}", file=sys.stderr)
        return 1
    counts[ENV_FILE_UNIT_LABEL] = len(env_findings)
    all_findings.extend(env_findings)

    for f in all_findings:
        print(f"DRIFT unit={f.unit} kind={f.kind} subject={f.subject}: {f.detail}")
    for f in all_grant_findings:
        print(f"GRANT unit={f.unit} kind={f.kind} subject={f.subject}: {f.detail}")
    if not all_findings and not all_grant_findings:
        print(
            f"converged: {', '.join(u for u, _, _ in UNITS)} match origin/main "
            "and /etc/alphalens/env carries no broker rail"
        )

    _write_metrics(render_metrics(counts) + render_grant_metrics(grant_present))
    return 0


if __name__ == "__main__":
    sys.exit(main())
