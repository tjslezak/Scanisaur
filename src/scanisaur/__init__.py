"""Scanisaur: schema-aware pre-flight checks for AI-agent SQL."""

from scanisaur.errors import ScanisaurError

__all__ = ["ScanisaurError", "__version__"]


def __getattr__(name: str) -> str:
    # Looked up on first use: importlib.metadata adds tens of ms to `scanisaur hook`.
    if name == "__version__":
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version("scanisaur")
        except PackageNotFoundError:  # running from a source tree that isn't installed
            return "0.0.0"
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
