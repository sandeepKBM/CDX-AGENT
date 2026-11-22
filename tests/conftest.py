import pathlib

import pytest

from cdx_agent import graph as graph_mod


@pytest.fixture(autouse=True)
def _isolate_home_root(monkeypatch, tmp_path_factory):
    """Prevent every home-directory guard (graph.py's module-level
    HOME_ROOT, and config.py/session.py/workspace_mirror.py's `Path.home()`
    calls) from tripping on pytest's own tmp_path, which can legitimately be
    a subdirectory of the real $HOME on some systems (e.g. a
    scratchpad-configured TMPDIR under $HOME). Tests that specifically
    exercise home-guard behavior patch these themselves within the test body,
    which safely overrides this default for that test only.
    """
    sentinel = tmp_path_factory.mktemp("unrelated-home-sentinel")
    monkeypatch.setattr(graph_mod, "HOME_ROOT", sentinel)
    monkeypatch.setattr(pathlib.Path, "home", staticmethod(lambda: sentinel))


@pytest.fixture(autouse=True)
def _no_live_state(monkeypatch, tmp_path_factory):
    """Never let a test start trash maintenance, write the live audit log, or read/append the live values ledger."""
    root = tmp_path_factory.mktemp("cdx-state")
    monkeypatch.setenv("CDX_TRASH_AUTO", "0")
    monkeypatch.setenv("CDX_AUDIT_DIR", str(root / "audit"))
    monkeypatch.setenv("CDX_TRASH_INDEX", str(root / "trash" / "index.jsonl"))
    monkeypatch.setenv("CDX_VALUES_FILE", str(root / "values.jsonl"))
