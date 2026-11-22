import pytest

mujoco = pytest.importorskip("mujoco")

from cdx_agent import model_diff as md  # noqa: E402

BASE = """<mujoco><option timestep="{ts}"/>
<worldbody><body name="link" pos="0 0 1">
  <joint name="hinge" type="hinge" {rng}/>
  <geom name="g" type="box" size=".1 .1 .1" mass="1" friction="{fr} 0.005 0.0001"/>
</body></worldbody>
<actuator><motor name="m" joint="hinge" {force}/></actuator></mujoco>"""


def _xml(tmp_path, name, ts="0.001", rng='range="-1 1"', fr="1", force='forcerange="-10 10" forcelimited="true"'):
    p = tmp_path / name
    p.write_text(BASE.format(ts=ts, rng=rng, fr=fr, force=force))
    return str(p)


def test_diff_reports_compiled_changes(tmp_path, capsys):
    a = _xml(tmp_path, "a.xml")
    b = _xml(tmp_path, "b.xml", ts="0.005", fr="0.8", force='forcerange="-120 120" forcelimited="true"')
    assert md.main([a, b]) == 0
    out = capsys.readouterr().out
    assert "timestep" in out and "0.001 -> 0.005" in out
    assert "forcerange" in out and "[-10, 10] -> [-120, 120]" in out
    assert "friction" in out and "3 compiled difference(s)" in out


def test_same_model_has_no_differences(tmp_path, capsys):
    a = _xml(tmp_path, "a.xml")
    assert md.main([a, a]) == 0
    assert "0 compiled difference(s)" in capsys.readouterr().out


def test_lint_flags_unlimited_actuator_and_joint(tmp_path, capsys):
    bad = _xml(tmp_path, "bad.xml", rng="", force="")
    assert md.main(["--lint", bad]) == 1
    out = capsys.readouterr().out
    assert "no force limit" in out and "hinge/slide without a range" in out
    assert md.main(["--lint", _xml(tmp_path, "ok.xml")]) == 0
