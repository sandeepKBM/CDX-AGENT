"""CDX-AGENT package."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("cdx-agent")
except PackageNotFoundError:  # pragma: no cover - local source tree fallback.
    __version__ = "0.0.0"

from .cli import main

__all__ = ["main", "__version__"]
