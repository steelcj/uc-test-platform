# uc-test-platform

The test platform for Vishpala's tools: it runs verification runs and end-to-end scenarios on clean test instances, and writes each run's record into the repository it verified, as [uc-automa 0031](https://github.com/steelcj/uc-automa/blob/main/en-ca/docs/decisions/0031--a-test-platform-repository-v0.1.0.md) decides.

## What it will hold

- **The drivers**, one per way of reaching an instance: over SSH for Windows, with `incus exec` for Linux, and later macOS.
- **The network cut**, with its self-test and the IPv6 rule.
- **The verification run** of [uc-automa 0027](https://github.com/steelcj/uc-automa/blob/main/en-ca/docs/decisions/0027--verification-without-an-end-to-end-scenario-v0.1.0.md): the README's install blocks replayed as written against local mirrors, the oldest Python, the test signing key with its two bad cases, and the record.
- **The recipes for the clean instances**, the Windows snapshot and the Linux container's, as scripts that rebuild them.

A tool's own job and an OSAT manager's scenario steps stay in that tool's repository; the platform runs them.

## Use

`--repository` names the checkout under test; the Windows driver also takes uc-portage and uc-automa from checkouts beside it:

```sh
uc-test-platform linux --repository <checkout> --instance <container> --snapshot <clean-snapshot>
uc-test-platform windows --repository <checkout> --instance <vm> --snapshot <clean-snapshot> --ssh <host>
uc-test-platform cut selftest <container> --snapshot <clean-snapshot>
```

`linux` runs the repository's `tests/e2e_scenario.py` in an Incus container; `windows` runs the unit tests of uc-portage, uc-automa and the repository, the network cut's self-test, the scenario and an offline install from wheels, in a virtual machine over SSH. Each restores its instance to the clean snapshot before and after, and writes its record into the repository under test, under `tests/e2e/<platform>/`. `cut` runs the network cut's own commands.

## Status

Not released. The Linux and Windows drivers and the network cut moved here from osat-manager-restic on 2026-10-11, unchanged apart from taking the repository under test as `--repository`; the verification run of uc-automa 0027 is still to be built. The verification runs for uc-portage 0.2.0 and uc-automa 0.2.0 use this repository at a recorded commit, and its first release follows uc-automa 0.2.0, whose wheel step it calls.

## Develop

From a checkout:

```sh
python -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m unittest discover -s tests
```

On Windows, in Command Prompt:

```bat
python -m venv .venv
.venv\Scripts\python -m pip install -e .
.venv\Scripts\python -m unittest discover -s tests
```

`python` is the command manage-python installs, on Linux, macOS and Windows alike. With another Python, use its own command instead, such as `python3`. On Debian and Ubuntu the system Python's `venv` module needs the `python3-venv` package first: `sudo apt install python3-venv`.

## License

Code is licensed MIT, and documentation CC BY 4.0, as [`REUSE.toml`](REUSE.toml) records (uc-automa 0025).

This document, *uc-test-platform*, by **Christopher Steel**, with AI assistance from **Claude Opus 5.5 (Anthropic)**, is licensed under the [Creative Commons Attribution 4.0 International License](https://creativecommons.org/licenses/by/4.0/).
