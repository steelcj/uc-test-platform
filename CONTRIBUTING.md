# Contributing

Changes arrive as commits on a branch, reviewed by Christopher Steel before they reach `main`.

## Before committing

- Run `.venv/bin/python -m unittest discover -s tests`, as the README's Develop section sets it up; every test passes.
- A change to behaviour comes with a test that fails without it.
- A file's licence follows its category, as [`REUSE.toml`](REUSE.toml) records: code MIT, documentation CC BY 4.0. Contributions are accepted under those licences, inbound equals outbound (uc-automa 0025).

## Documents

Documents carry Dublin Core and `sat:` front matter, a Changelog table and a License section. Before 1.0.0 a document's filename carries its version with dots, as in `-v0.1.0`; a bump renames the file with `git mv` and updates its links in the same commit.

## License

This document, *Contributing*, by **Christopher Steel**, with AI assistance from **Claude Opus 5.5 (Anthropic)**, is licensed under the [Creative Commons Attribution 4.0 International License](https://creativecommons.org/licenses/by/4.0/).
