# Roadmap

Forward plan only; completed items move to [CHANGELOG.md](CHANGELOG.md).

## Before uc-portage 0.2.0's verification runs

- Move the Windows driver, `tests/e2e/windows/run_over_ssh.py`, and the network cut, `tests/incus_acl.py`, from osat-manager-restic, with their tests.
- A Linux driver for the container `linux`, reached with `incus exec`, as the ordinary user `osattest`.
- The verification run of uc-automa 0027: wheels built by uc-automa's wheel step from a recorded uc-automa commit; the README's `Install`, `Develop`, `Get the release key` and `Install offline` blocks replayed as written, one block per shell per section, with mirrors and `insteadOf` mappings in the test account; the oldest Python for the unit tests; the two bad signature cases; the record, with placeholders and a `Result:` line, written into the verified repository.
- Recipes that rebuild the Windows snapshot and the Linux container's snapshot, both `osat-ready-tools`.

## First release

- After uc-automa 0.2.0, released with `uc-automa release` like the other tools, depending on `uc-automa>=0.2.0,<0.3.0`.

## Later

- macOS, once Apple hardware is available.
- osat-manager-restic's scenario run by the platform, its steps kept in osat-manager-restic.
