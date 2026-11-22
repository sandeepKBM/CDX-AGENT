import re
from pathlib import Path

from cdx_agent import modes

BASH = Path(__file__).resolve().parents[1] / "tools" / "bin" / "cdx-agent"

DEPRECATED = {"--dg", "--graperoot", "--dg-root", "--graperoot-root", "--dg-workspace", "--dg-workspace-init",
              "--dg-workspace-list", "--dg-workspace-show", "--dg-workspace-clean",
              "--doctor", "--token-doctor", "--usage", "--usage-session", "--usage-month"}


def bash_flags():
    flags = set()
    for line in BASH.read_text().splitlines():
        m = re.match(r"^    ((?:-h|--[a-z0-9-]+)(?:=\*)?(?:\|(?:-h|--[a-z0-9-]+)(?:=\*)?)*)\)$", line)
        if m:
            for part in m.group(1).split("|"):
                flags.add(part.replace("=*", ""))
    return flags


def test_registry_has_every_bash_flag():
    found = bash_flags()
    assert len(found) > 60
    assert found <= set(modes.BY_FLAG), sorted(found - set(modes.BY_FLAG))


def test_deprecated_set():
    assert {m.flag for m in modes.MODES if m.deprecated} == DEPRECATED


def test_dry_run_flags():
    for flag in ("--deploy", "--rm", "--gpu-lock"):
        assert modes.BY_FLAG[flag].supports_dry_run
    for flag in ("--cpu-job", "--value", "--trash", "--inuse", "--hygiene"):
        assert not modes.BY_FLAG[flag].supports_dry_run


def test_nearest_flag_message():
    assert modes.unknown_message("--hygeine") == "unknown option --hygeine; did you mean --hygiene?"
    assert modes.lookup("--drain=5") is modes.BY_FLAG["--drain"]


def test_main_exit_codes(capsys):
    assert modes.main(["--check-flag", "--hygiene"]) == 0
    assert modes.main(["--check-flag", "--hygeine"]) == 2
    assert "did you mean --hygiene" in capsys.readouterr().err
    assert modes.main(["--supports-dry-run", "--deploy"]) == 0
    assert modes.main(["--supports-dry-run", "--cpu-job"]) == 3


def test_help_text_lists_every_active_flag():
    text = modes.help_text()
    for m in modes.MODES:
        if not m.deprecated and m.flag != "-h":
            assert m.flag in text
