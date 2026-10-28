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
    assert stats.gpu_count == 2 and stats.pending_gpus == 3 and stats.held_gpus == 0     # the cap counts running jobs only
    assert stats.clients == {"rlab1": 4}


def test_choice_prefers_most_free_then_client_limit_and_type_filter():
    choice = gpu_pick.choose_node(SINFO, SQUEUE, ["a4000"], n=1, cap=12,
                                  per_node_clients=6)
    assert choice is not None
    assert (choice.node, choice.gpu_type, choice.free, choice.clients) == ("rlab1", "a4000", 6, 4)
    ada = gpu_pick.choose_node(SINFO, "", ["4500_ada"], n=1)
    assert ada is not None and ada.node == "ilab1"


def test_cap_client_limit_and_drain_are_explained():
    cap = gpu_pick.evaluate(SINFO, SQUEUE, ["a4000"], n=1, cap=2)   # 2 running + 1 > 2 (pending not counted)
    assert cap.choice is None
    assert any("GPU cap" in reason for _, reason in cap.reasons)
    limited = gpu_pick.evaluate(SINFO, SQUEUE, ["a4000"], n=1, cap=12,
                                per_node_clients=4)
    assert limited.choice is not None and limited.choice.node == "rlab2"
    assert any(node == "rlab3" and "skipped" in reason for node, reason in limited.reasons)


SCONTROL = """\
NodeName=rlab1 Arch=x86_64 RealMemory=257000 AllocMem=0 FreeMem=250000 State=IDLE ThreadsPerCore=1 Reason=none
NodeName=rlab2 Arch=x86_64 RealMemory=257000 AllocMem=0 State=IDLE ThreadsPerCore=1
NodeName=rlab3 Arch=x86_64 RealMemory=257000 AllocMem=0 State=IDLE+DRAIN ThreadsPerCore=1 Reason=bad disk [root@x]
NodeName=rlab4 Arch=x86_64 RealMemory=257000 AllocMem=190000 State=MIXED+PLANNED ThreadsPerCore=1
NodeName=ilab1 Arch=x86_64 RealMemory=257000 AllocMem=0 State=IDLE ThreadsPerCore=1
"""


def test_parse_scontrol_mem_and_helpers():
    info = gpu_pick.parse_scontrol(SCONTROL)
    assert info["rlab4"].state == "MIXED+PLANNED" and info["rlab4"].free_mb == 67000
    assert info["rlab3"].state == "IDLE+DRAIN"
    assert gpu_pick.parse_mem_mb("60G") == 61440 and gpu_pick.parse_mem_mb("2048") == 2048 and gpu_pick.parse_mem_mb("1T") == 1048576
    assert gpu_pick.parse_other_user_nodes("ss5772|rlab1\nbob|rlab2\nal|rlab3,rlab4\n", "ss5772") == {"rlab2", "rlab3", "rlab4"}
    df = "Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/sda1 1000000000 999000000 1048576 100% /freespace/local\n"
    assert gpu_pick.parse_df_free_gb(df) == 1.0 and gpu_pick.parse_df_free_gb("df: no such file") is None


def test_memory_and_planned_state_reject_the_node_that_stayed_pending():
    sinfo = "rlab4 gpu:4500_ada:2 gpu:4500_ada:0 mix\nrlab1 gpu:4500_ada:2 gpu:4500_ada:1 mix\n"
    ev = gpu_pick.evaluate(sinfo, "", ["4500_ada"], scontrol_text=SCONTROL, mem_mb=128 * 1024)
    assert ev.choice.node == "rlab1" and dict(ev.reasons)["rlab4"].startswith("state=MIXED+PLANNED")
    # without PLANNED the memory check alone still rejects rlab4 (65G free < 128G); 60G fits there (more free GPUs wins)
    unplanned = SCONTROL.replace("MIXED+PLANNED", "MIXED")
    ev = gpu_pick.evaluate(sinfo, "", ["4500_ada"], scontrol_text=unplanned, mem_mb=128 * 1024)
    assert "free memory 65G < requested 128G" in dict(ev.reasons)["rlab4"]
    assert gpu_pick.evaluate(sinfo, "", ["4500_ada"], scontrol_text=unplanned, mem_mb=60 * 1024).choice.node == "rlab4"
    assert gpu_pick.evaluate(sinfo, "", ["4500_ada"], scontrol_text=SCONTROL, mem_mb=60 * 1024).choice.node == "rlab1"


def test_state_words_planned_reserved_maint_down_are_blocked():
    for state in ("MIXED+PLANNED", "IDLE+DRAIN", "DOWN*", "RESERVED", "MAINT", "plnd", "drng"):
        assert gpu_pick._state_blocked(state)
    assert not gpu_pick._state_blocked("MIXED") and not gpu_pick._state_blocked("idle")


def test_isaac_client_limit_alone_vs_shared_node_and_default_pattern():
    sinfo = "rlab1 gpu:a4000:8 gpu:a4000:2 mix\nrlab2 gpu:a4000:8 gpu:a4000:2 mix\n"
    squeue = "1|isaac_eval|rlab1|gpu:a4000:1|RUNNING\n2|isaac_eval|rlab2|gpu:a4000:1|RUNNING\n3|train|rlab2|gpu:a4000:1|RUNNING\n"
    # alone: 2 running clients + 2 new = 4 <= 4 -> fits.  Shared with another user: limit 2 -> rejected.
    ev = gpu_pick.evaluate(sinfo, squeue, ["a4000"], other_user_nodes={"rlab2"})
    assert ev.choice.node == "rlab1" and ev.choice.clients == 2
    assert "client limit 2+2>2" in dict(ev.reasons)["rlab2"] and "other users" in dict(ev.reasons)["rlab2"]
    full = gpu_pick.evaluate(sinfo, squeue + "4|tower_x|rlab1|gpu:a4000:1|RUNNING\n", ["a4000"])
    assert full.choice.node == "rlab2" and "client limit 4+2>4" in dict(full.reasons)["rlab1"]


def test_disk_check_rejects_full_disk_and_warns_on_unknown():
    sinfo = "ilab1 gpu:a4000:2 gpu:a4000:0 idle\nilab2 gpu:a4000:2 gpu:a4000:0 idle\nilab3 gpu:a4000:2 gpu:a4000:1 idle\n"
    free = {"ilab1": 0.0, "ilab2": 3.0, "ilab3": None}
    ev = gpu_pick.evaluate(sinfo, "", ["a4000"], prefixes=["ilab"], min_disk_gb=50, disk_probe=free.get)
    assert ev.choice.node == "ilab3"                       # unknown disk is a warning, not a rejection
    assert "/freespace/local free 0G < 50G" in dict(ev.reasons)["ilab1"] and "free 3G" in dict(ev.reasons)["ilab2"]
    assert any("ilab3" in w and "unknown" in w for w in ev.warnings)
    # the probe runs only for nodes that passed the other checks
    probed = []
    gpu_pick.evaluate(sinfo, "", ["a4000"], prefixes=["ilab"], n=2, min_disk_gb=50,
                      disk_probe=lambda n: probed.append(n) or 100.0)
    assert probed == ["ilab1", "ilab2"]


def test_main_prints_choice_with_mem_and_one_line_per_rejected_node(monkeypatch, capsys):
    calls = []

    def fake(command):
        calls.append(list(command))
        text = command[-1]
        if "sinfo" in text:
            return (0, "rlab1 gpu:a4000:4 gpu:a4000:0 idle\nrlab1 gpu:a4000:4 gpu:a4000:0 idle\nrlab4 gpu:a4000:4 gpu:a4000:0 mix\n"
                       "westeros gpu:a4000:8 gpu:a4000:0 idle\n", "")
        if "scontrol" in text:
            return (0, SCONTROL, "")
        if "-t RUNNING" in text:
            return (0, "other|rlab4\n", "")
        return (0, "1|other|rlab1|gpu:a4000:1|RUNNING\n", "")

    monkeypatch.setattr(gpu_pick, "run_command", fake)
    monkeypatch.setattr(gpu_pick, "probe_disk", lambda node: 200.0)
    assert gpu_pick.main(["--gpu-type", "a4000", "--user", "ss5772", "--mem", "128G"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == 'node=rlab1 type=a4000 free=4 clients=0 sbatch_opts="--nodelist=rlab1 --gres=gpu:a4000:1 --mem=128G"'
    assert out[1:] == ["rejected node=rlab4 reason=state=MIXED+PLANNED skipped"]       # no duplicates, prefix-filtered hidden
    assert all(call[:5] == ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"] for call in calls)


def test_main_no_choice_returns_2_and_survives_scontrol_failure(monkeypatch, capsys):
    def fake(command):
        if "sinfo" in command[-1]:
            return (0, "rlab1 gpu:a4000:4 gpu:a4000:4 idle\n", "")
        if "scontrol" in command[-1]:
            return (1, "", "boom")
        return (0, "", "")

    monkeypatch.setattr(gpu_pick, "run_command", fake)
    monkeypatch.setattr(gpu_pick, "probe_disk", lambda node: 200.0)
    assert gpu_pick.main(["--gpu-type", "a4000", "--user", "u"]) == 2
    cap = capsys.readouterr()
    assert "rejected node=rlab1 reason=need 1" in cap.out and "scontrol show node failed" in cap.err


def test_squeue_gres_prefix_counts_gpus_and_clients():
    # squeue %b prints "gres/gpu:<type>:<n>"; the "gres/" prefix must not hide the count (it zeroed the cap and client checks).
    from cdx_agent.gpu_pick import parse_squeue
    s = parse_squeue("535104|tower_dv4c_c|rlab4|gres/gpu:a4000:2|RUNNING\n535076|small_lora4b|rlab4|gres/gpu:a4000:1|RUNNING\n"
                     "9|tower_x|rlab3|gres/gpu:1|RUNNING")
    assert s.gpu_count == 4
    assert s.clients == {"rlab4": 4, "rlab3": 2}



def test_cap_counts_running_only_held_and_pending_reported():
    # 2026-10-21: 23 held/pending 1-GPU jobs made every node "user GPU cap 24+3>12" although only 2 GPUs ran.
    squeue = "".join(f"{i}|t5x_{i}||gres/gpu:1|PENDING|JobHeldUser\n" for i in range(17))
    squeue += "".join(f"{i}|s0_{i}||gres/gpu:1|PENDING|Priority\n" for i in range(100, 106))
    squeue += "532911|cap_coder|rlab2|gres/gpu:a100:2|RUNNING|None\n"
    stats = gpu_pick.parse_squeue(squeue)
    assert (stats.gpu_count, stats.pending_gpus, stats.held_gpus) == (2, 6, 17)
    ev = gpu_pick.evaluate("rlab3 gpu:a4000:8 gpu:a4000:1 alloc\n", squeue, ["a4000"], n=1)
    assert ev.choice is not None and ev.choice.node == "rlab3"
    assert not gpu_pick.evaluate("rlab3 gpu:a4000:8 gpu:a4000:1 alloc\n", squeue, ["a4000"], n=11).choice   # 2+11 > 12


SCONTROL_CPU = """\
NodeName=rlab3 CPUAlloc=96 CPUEfctv=96 CPUTot=96 RealMemory=1027551 AllocMem=165888 State=ALLOCATED
NodeName=rlab4 CPUAlloc=50 CPUEfctv=96 CPUTot=96 RealMemory=256000 AllocMem=256000 State=MIXED
NodeName=ilab1 CPUAlloc=60 CPUTot=64 RealMemory=786432 AllocMem=131072 State=MIXED
"""
SINFO_CPU = "rlab3 gpu:a4000:8 gpu:a4000:1 alloc\nrlab4 gpu:a4000:8 gpu:a4000:6 mix\nilab1 gpu:a4500:8 gpu:a4500:0 mix\n"


def test_cpu_check_rejects_nodes_with_all_cpus_allocated():
    info = gpu_pick.parse_scontrol(SCONTROL_CPU)
    assert info["rlab3"].free_cpus == 0 and info["rlab4"].free_cpus == 46 and info["ilab1"].free_cpus == 4  # CPUTot fallback
    ev = gpu_pick.evaluate(SINFO_CPU, "", ["a4000", "a4500"], scontrol_text=SCONTROL_CPU, mem_mb=48 * 1024)
    r = dict(ev.reasons)
    assert "free CPUs 0 < 1 (CPUAlloc 96 of 96)" in r["rlab3"]
    assert "free memory 0G" in r["rlab4"]
    assert ev.choice.node == "ilab1"
    assert gpu_pick.evaluate(SINFO_CPU, "", ["a4500"], scontrol_text=SCONTROL_CPU, cpus=8).choice is None
    assert gpu_pick.evaluate(SINFO_CPU, "", ["a4000"], scontrol_text=SCONTROL_CPU, cpus=0, mem_mb=None).choice.node == "rlab3"


def test_status_rows_say_why_free_gpus_cannot_start():
    rows = {r["node"]: r for r in gpu_pick.status_rows(SINFO_CPU, SCONTROL_CPU, mem_mb=48 * 1024)}
    assert rows["rlab3"]["free"] == 7 and rows["rlab3"]["start"] == "CPUs full (96/96)"
    assert rows["rlab4"]["start"].startswith("mem 0G free")
    assert rows["ilab1"]["start"] == "yes" and rows["ilab1"]["cpu_free"] == 4 and rows["ilab1"]["mem_total_g"] == 768


WS = """0, GPU-a, NVIDIA GeForce RTX 2080 Ti, 1476, 11264, 0
1, GPU-b, NVIDIA GeForce RTX 2080 Ti, 5342, 11264, 4
2, GPU-c, NVIDIA GeForce RTX 2080 Ti, 3, 11264, 0
---
GPU-a,3030503,1472,ss5772
GPU-b,2184688,5334,jhd79
GPU-b,2184689,8,jhd79
"""


def test_parse_workstation_owners_per_gpu():
    g = gpu_pick.parse_workstation(WS)
    assert [x["index"] for x in g] == [0, 1, 2]
    assert g[0]["owners"] == {"ss5772": 1472} and g[1]["owners"] == {"jhd79": 5342} and g[2]["owners"] == {}
    assert g[1]["used_mib"] == 5342 and g[1]["total_mib"] == 11264 and g[1]["util"] == 4


def test_main_status_prints_inventory_cap_line_and_workstations(monkeypatch, capsys):
    def fake(command):
        text = command[-1]
        if "sinfo" in text:
            return (0, SINFO_CPU, "")
        if "scontrol" in text:
            return (0, SCONTROL_CPU, "")
        if "-t RUNNING" in text:
            return (0, "", "")
        if "nvidia-smi" in text:
            return (0, WS, "") if "westeros" in command else (255, "", "ssh: connect to host arrakis: timed out")
        return (0, "1|x||gres/gpu:1|PENDING|JobHeldUser\n2|y|rlab2|gres/gpu:a100:2|RUNNING|None\n", "")

    monkeypatch.setattr(gpu_pick, "run_command", fake)
    monkeypatch.setattr("socket.gethostname", lambda: "elsewhere")
    assert gpu_pick.main(["--status", "--user", "ss5772", "--mem", "48G"]) == 0
    out = capsys.readouterr().out
    assert "rlab3\ta4000:8\t7/8\t0/96\t" in out and "CPUs full (96/96)" in out
    assert "total: 24 GPUs, 17 free, 8 startable (on 1 nodes)" in out
    assert "ss5772: running 2/8 GPUs (cap), pending 0, held 1" in out
    assert "# westeros" in out and "jhd79 5342" in out and "free" in out
    assert "# arrakis: unreachable" in out and "timed out" in out


def test_main_requires_gpu_type_unless_status():
    import pytest
    with pytest.raises(SystemExit):
        gpu_pick.main(["--user", "u"])
