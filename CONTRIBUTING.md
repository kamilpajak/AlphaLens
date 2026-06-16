# Contributing to AlphaLens

Thanks for your interest. AlphaLens is a personal research and decision-support
project. Contributions — bug reports, fixes, docs, and well-scoped features — are
welcome, but please read the two short sections below first: they cover **how your
contribution is licensed** and the **sign-off** every commit needs.

## License of contributions

The project is released under the [PolyForm Noncommercial License 1.0.0](LICENSE)
(source-available, noncommercial use only).

By submitting a contribution (a pull request, patch, or any other change), you agree
that:

1. Your contribution is licensed under the project's license, the **PolyForm
   Noncommercial License 1.0.0** (inbound = outbound); and
2. You grant the project's copyright holder, **Kamil Pająk**, a perpetual, worldwide,
   non-exclusive, royalty-free, irrevocable license to use, reproduce, modify,
   distribute, and **relicense** your contribution under any terms, **including
   commercial or proprietary terms**.

Point 2 lets the project offer separate commercial licenses in the future without
having to track down and re-ask every past contributor. You keep the copyright to your
own work; you are only granting these licenses on top of it.

If you cannot agree to these terms, please do not submit a contribution.

## Developer Certificate of Origin (DCO)

Every commit must be signed off to certify the
[Developer Certificate of Origin](https://developercertificate.org/). This is a
lightweight, sign-off-based alternative to a separate signed CLA: you assert that you
have the right to submit the code under the terms above.

Add a `Signed-off-by` line to each commit by committing with the `-s` flag:

```bash
git commit -s -m "fix(scope): short description"
```

This appends a line using your real name and email from your git config:

```
Signed-off-by: Jane Developer <jane@example.com>
```

Use your real name (no anonymous or pseudonymous contributions) and an email you can
be reached at. If you forgot to sign off the most recent commit, fix it with
`git commit --amend -s`; for a series of commits, use
`git rebase --signoff <base>`.

### The DCO text you are certifying

```
Developer Certificate of Origin
Version 1.1

Copyright (C) 2004, 2006 The Linux Foundation and its contributors.

Everyone is permitted to copy and distribute verbatim copies of this
license document, but changing it is not allowed.


Developer's Certificate of Origin 1.1

By making a contribution to this project, I certify that:

(a) The contribution was created in whole or in part by me and I
    have the right to submit it under the open source license
    indicated in the file; or

(b) The contribution is based upon previous work that, to the best
    of my knowledge, is covered under an appropriate open source
    license and I have the right under that license to submit that
    work with modifications, whether created in whole or in part
    by me, under the same open source license (unless I am
    permitted to submit under a different license), as indicated
    in the file; or

(c) The contribution was provided directly to me by some other
    person who certified (a), (b) or (c) and I have not modified
    it.

(d) I understand and agree that this project and the contribution
    are public and that a record of the contribution (including all
    personal information I submit with it, including my sign-off) is
    maintained indefinitely and may be redistributed consistent with
    this project or the open source license(s) involved.
```

## Practical workflow

- **Open an issue first** for anything non-trivial, so we can agree on the approach
  before you invest time.
- **Commits** follow [Conventional Commits](https://www.conventionalcommits.org/)
  (`feat(scope):`, `fix(scope):`, `docs(scope):`, `refactor(scope):`, …).
- **Tests** use `unittest` (not pytest). Run the research-lab suite before opening a PR:
  ```bash
  uv run python -m unittest discover \
      -s apps/alphalens-research/tests \
      -t apps/alphalens-research -v
  ```
  or `just test` for the full Python + Django + web sweep, and `just lint` for linting.
- **Source language is English** (comments, docstrings, identifiers) — enforced by
  `apps/alphalens-research/tests/test_no_polish_chars.py`.
- Architecture, layer statuses, and conventions live in [`CLAUDE.md`](CLAUDE.md); read
  it before touching shared surfaces.
