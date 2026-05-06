# systemd-user units (VPS deployment)

User-scoped service definitions for AlphaLens long-running tasks on Linux VPS
hosts where launchd is unavailable.

## form4-backfill.service

SEC EDGAR Form-4 bulk backfill (`scripts/run_form4_backfill.py`). Wall-time on
a small VPS: ~5-10 days for the full 2006-2026 R3000 universe (~8000 CIKs,
limited by SEC's 10 req/s rate cap). Resume-safe via the JSON manifest at
`~/.alphalens/form4_backfill_manifest.json`, so a crash + restart skips
already-processed CIKs and resumes from where it left off.

### Install

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/form4-backfill.service ~/.config/systemd/user/

# Edit Environment= lines in the unit file to match your VPS paths and contact.
systemctl --user daemon-reload
systemctl --user enable --now form4-backfill.service

# One-time: allow the unit to keep running after logout.
sudo loginctl enable-linger "$USER"
```

### Inspect

```bash
systemctl --user status form4-backfill.service
journalctl --user -u form4-backfill.service -f       # live tail
journalctl --user -u form4-backfill.service --since "1 hour ago"
```

### Stop / restart

```bash
systemctl --user stop form4-backfill.service
systemctl --user restart form4-backfill.service
```

### Why this exists

The earlier deployment ran the script inside `screen` with
`bash -c "... ; exec bash"`. That setup has no auto-recovery — a reboot,
OOM kill, or `pkill` aborts a multi-day run with no restart. systemd's
`Restart=on-failure` + `RestartSec=60` automates recovery while
`StartLimitBurst=5` prevents tight crash loops if the underlying problem
is persistent (bad credentials, exhausted disk, SEC ban).
