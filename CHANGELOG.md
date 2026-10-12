# Changelog

All notable changes are recorded here, in the form of [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). This repository follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- The repository, as uc-automa 0031 decides: the `uc_test_platform` package with its `uc-test-platform --version` command and tests, the `python-tool` layout, and uc-automa 0.1.0's offerings.
- The Linux and Windows end-to-end drivers and the network cut, moved from osat-manager-restic at commit 37bcb06 (uc-automa 0031): `uc-test-platform linux`, `windows` and `cut`. Each takes the repository under test as `--repository` and writes its record there, under `tests/e2e/<platform>/`.
