from pathlib import Path

from cdx_agent import graph


def _touch(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _isolated_repo(tmp_path, monkeypatch) -> Path:
    monkeypatch.setattr(graph, "HOME_ROOT", tmp_path / "unrelated-home")
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo


def test_document_frequencies_counts_nodes_containing_each_token():
    nodes = [
        graph.FileNode(path="train_a.py", kind="python", imports=[], classes=[], functions=[], tags=[], path_refs=[]),
        graph.FileNode(path="train_b.py", kind="python", imports=[], classes=[], functions=[], tags=[], path_refs=[]),
        graph.FileNode(path="quantile.py", kind="python", imports=[], classes=[], functions=[], tags=[], path_refs=[]),
    ]
    freqs = graph._document_frequencies(nodes, ["train", "quantile"])
    assert freqs["train"] == 2
    assert freqs["quantile"] == 1


def test_rare_token_outranks_common_token_via_idf_weighting(tmp_path, monkeypatch):
    # E7: a task token that shows up in nearly every file (weak signal, e.g.
    # "train" in a training-heavy repo) should score lower per-hit than one
    # that shows up in only a couple files (strong, specific signal) -- the
    # flat +2-per-hit scoring this replaces couldn't tell these apart.
    repo = _isolated_repo(tmp_path, monkeypatch)
    for i in range(8):
        _touch(repo / f"train_variant_{i}.py", "# train\n")
    _touch(repo / "quantile_regression.py", "# quantile\n")

    graph.build_graph(str(repo), task="quantile train")
    context_pack = (repo / ".codex_graph" / "context_pack.md").read_text()

    # Scope to the "Task-relevant files" section specifically -- the
    # unrelated "Likely entrypoints" section (ENTRYPOINT_RE-based, not
    # task-relevance-scored) lists train_variant_*.py first structurally,
    # which would make a whole-document text-position check meaningless.
    section = context_pack.split("## Task-relevant files")[1].split("## Possible call chain")[0]
    quantile_idx = section.find("quantile_regression.py")
    common_idx = section.find("train_variant_0.py")
    assert quantile_idx != -1
    assert common_idx != -1
    assert quantile_idx < common_idx


def test_score_node_for_task_falls_back_to_flat_weighting_without_doc_freqs(tmp_path):
    node = graph.FileNode(
        path="train_policy.py", kind="python", imports=[], classes=[], functions=[], tags=[], path_refs=[]
    )
    # backward-compat signature: no doc_freqs/total_nodes supplied
    score = graph._score_node_for_task(node, ["train", "policy"])
    assert score > 0


def test_score_node_for_task_idf_weighted_score_exceeds_flat_for_rare_token(tmp_path):
    node = graph.FileNode(
        path="quantile.py", kind="python", imports=[], classes=[], functions=[], tags=[], path_refs=[]
    )
    flat_score = graph._score_node_for_task(node, ["quantile"])
    idf_score = graph._score_node_for_task(node, ["quantile"], doc_freqs={"quantile": 1}, total_nodes=100)
    assert idf_score > flat_score


def test_command_relevant_outputs_valid_json_sorted_by_score(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace

    repo = _isolated_repo(tmp_path, monkeypatch)
    _touch(repo / "quantile_regression.py", "# quantile\n")
    _touch(repo / "unrelated.py", "x = 1\n")

    graph.command_relevant(SimpleNamespace(repo=str(repo), task="quantile", max_files=20000, force_home_scan=False))
    import json as json_mod

    out = json_mod.loads(capsys.readouterr().out)
    assert out
    assert out[0]["path"] == "quantile_regression.py"
    scores = [item["score"] for item in out]
    assert scores == sorted(scores, reverse=True)
