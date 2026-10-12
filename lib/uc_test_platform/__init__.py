"""uc-test-platform: verification runs and end-to-end scenarios on clean
test instances (uc-automa 0031).

It reaches each instance, cuts its network, replays a tool's README as
written, and writes the run's record into the repository it verified.
"""

from pathlib import Path


def _version() -> str:
    try:
        from importlib.metadata import PackageNotFoundError, version
    except ImportError:  # pragma: no cover, Python 3.8 has importlib.metadata
        return "unknown"
    try:
        return version("uc-test-platform")
    except PackageNotFoundError:
        # Running from a checkout that is not installed: VERSION is the
        # single source, as pyproject.toml reads it too.
        version_file = Path(__file__).resolve().parents[2] / "VERSION"
        if version_file.is_file():
            return version_file.read_text(encoding="utf-8").strip()
        return "unknown"


__version__ = _version()
