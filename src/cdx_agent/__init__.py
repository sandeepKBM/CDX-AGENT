"""CDX-AGENT package.

Imports stay lazy: hooks run under the system python (no PyYAML/tomli) and import single modules such as
cdx_agent.delete_policy, so importing the package must not pull in cli/graph.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("cdx-agent")
except PackageNotFoundError:  # pragma: no cover - local source tree fallback.
    __version__ = "0.0.0"


def main(argv=None):
    from .cli import main as _main

    return _main(argv)


__all__ = ["main", "__version__"]
