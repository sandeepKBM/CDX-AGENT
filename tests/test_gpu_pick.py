from cdx_agent import gpu_pick


SINFO = """\
rlab1 gpu:a4000:8 gpu:a4000:2 idle
rlab2 gpu:a4000:8 gpu:a4000:7 idle
rlab3 gpu:a4000:8 gpu:a4000:0 drain
ilab1 gpu:4500_ada:4 gpu:4500_ada:1 idle
"""

SQUEUE = """\
1|tower_alpha|rlab1|gpu:a4000:2|RUNNING
2|other|rlab2|gpu:a4000:3|PENDING
3|done|rlab1|gpu:a4000:8|COMPLETED
"""


def test_parse_gres_and_sinfo_skips_nothing_until_selection():
    assert gpu_pick.parse_gres("gpu:a4000:8(S:0-1)") == {"a4000": 8}
    nodes = gpu_pick.parse_sinfo(SINFO)
    assert nodes[0].free["a4000"] == 6
    assert nodes[2].state == "drain"


def test_parse_squeue_counts_active_gpus_and_clients():
    stats = gpu_pick.parse_squeue(SQUEUE, client_jobs="tower_", slots_per_gpu=2)
    assert stats.gpu_count == 5
    assert stats.clients == {"rlab1": 4}


def test_choice_prefers_most_free_then_client_limit_and_type_filter():
    choice = gpu_pick.choose_node(SINFO, SQUEUE, ["a4000"], n=1, cap=12,
                                  per_node_clients=6)
    assert choice is not None
    assert (choice.node, choice.gpu_type, choice.free, choice.clients) == ("rlab1", "a4000", 6, 4)
    ada = gpu_pick.choose_node(SINFO, "", ["4500_ada"], n=1)
    assert ada is not None and ada.node == "ilab1"


def test_cap_client_limit_and_drain_are_explained():
    cap = gpu_pick.evaluate(SINFO, SQUEUE, ["a4000"], n=1, cap=5)
    assert cap.choice is None
    assert any("GPU cap" in reason for _, reason in cap.reasons)
    limited = gpu_pick.evaluate(SINFO, SQUEUE, ["a4000"], n=1, cap=12,
                                per_node_clients=4)
    assert limited.choice is not None and limited.choice.node == "rlab2"
    assert any(node == "rlab3" and "skipped" in reason for node, reason in limited.reasons)


def test_main_uses_ssh_runner_and_prints_sbatch_options(monkeypatch, capsys):
    calls = []

    def fake(command):
        calls.append(list(command))
        if "sinfo" in command[-1]:
            return (0, "rlab1 gpu:a4000:4 gpu:a4000:0 idle\n", "")
        return (0, "1|other|rlab1|gpu:a4000:1|RUNNING\n", "")

    monkeypatch.setattr(gpu_pick, "run_command", fake)
    assert gpu_pick.main(["--gpu-type", "a4000", "--user", "ss5772"]) == 0
    out = capsys.readouterr().out
    assert 'node=rlab1 type=a4000 free=4 clients=0 sbatch_opts="--nodelist=rlab1 --gres=gpu:a4000:1"' in out
    assert all(call[:5] == ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"] for call in calls)
