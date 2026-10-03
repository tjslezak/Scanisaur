"""Scanisaur: schema-aware pre-flight checks for AI-agent SQL."""

from importlib.metadata import PackageNotFoundError, version

from scanisaur.errors import ScanisaurError

try:
    __version__ = version("scanisaur")
except PackageNotFoundError:  # running from a source tree that isn't installed
    __version__ = "0.0.0"

__all__ = ["ScanisaurError", "__version__"]
