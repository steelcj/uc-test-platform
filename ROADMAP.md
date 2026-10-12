# Roadmap

Forward plan only; completed items move to [CHANGELOG.md](CHANGELOG.md).

## Before uc-portage 0.2.0's verification runs

- The Linux driver for the container `linux`, reached with `incus exec` as the ordinary user `osattest` that its snapshot already holds; the moved driver creates its own user, as osat-manager-restic's scenario needs.
- The verification run of uc-automa 0027: wheels built by uc-automa's wheel step from a recorded uc-automa commit; the README's `Install`, `Develop`, `Get the release key` and `Install offline` blocks replayed as written, one block per shell per section, with mirrors and `insteadOf` mappings in the test account; the oldest Python for the unit tests; the two bad signature cases; the record, with placeholders and a `Result:` line, written into the verified repository.
- Recipes that rebuild the Windows snapshot and the Linux container's snapshot, both `osat-ready-tools`.

## First release

- After uc-automa 0.2.0, released with `uc-automa release` like the other tools, depending on `uc-automa>=0.2.0,<0.3.0`.

## Later

- macOS, once Apple hardware is available.
- osat-manager-restic's scenario run by the platform, its steps kept in osat-manager-restic.
