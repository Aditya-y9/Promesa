"""evaluate.py — score a LAYA checkpoint on the held-out test items.

Each question is asked on its own, so agreement and latency compare like-for-like
across checkpoints. The answer key is the planted label; agreement with the Sonnet
reference judge is reported alongside it.

    python evaluate.py base                  # the zero-shot typed-decisions checkpoint
    python evaluate.py runs/plan101          # a fine-tuned checkpoint
"""
from __future__ import annotations

import collections
import json
import statistics
import sys
import time
import warnings
from pathlib import Path
from typing import Any

from finetune_data import MODEL_ID, SUBFOLDER, TEST_SET, labels, questions, read_jsonl
from plan_critique_task import state_for

COVERAGE = (0.1, 0.25, 0.5, 0.75, 0.9, 1.0)
RUNS = Path(__file__).parent / "runs"


def _load_laya(model_path: str):
    """Load on MLX if available, else on PyTorch cpu."""
    import laya
    return laya.load(str(Path(model_path).resolve()))


def load(model: str):
    """Load the checkpoint."""
    if model == "base":
        from huggingface_hub import snapshot_download
        from laya.agent import _fix_tokenizer_config
        root = snapshot_download(MODEL_ID, allow_patterns=[f"{SUBFOLDER}/*"])
        path = str(Path(root) / SUBFOLDER)
        _fix_tokenizer_config(path)
        return load(path)
    return _load_laya(model)


def ask(agent, items: list[dict[str, Any]], name: str, question: dict[str, Any]) -> dict[str, Any]:
    """Every item through one question, timed per call after a warm-up call."""
    agent.predict(state_for(items[0]["repo_context"], items[0]["plan"]), {name: question})
    rows, latency = [], []
    for it in items:
        start = time.perf_counter()
        answer = agent.predict(state_for(it["repo_context"], it["plan"]), {name: question})["answers"][name]
        latency.append((time.perf_counter() - start) * 1000)
        rows.append({"id": it["id"], "pick": answer["choice"].replace(" ", "_"),
                     "conf": float(answer["confidence"]), "ms": round(latency[-1], 2)})
    return {"rows": rows, "p50_ms": round(statistics.median(latency), 1)}


def agreement(rows: list[dict[str, Any]], key: dict[int, str]) -> float:
    return sum(r["pick"] == key[r["id"]] for r in rows) / len(rows)


def risk_coverage(rows: list[dict[str, Any]], key: dict[int, str]) -> list[tuple[float, float]]:
    """Agreement on the most confident share of items, for each share in COVERAGE."""
    ranked = sorted(rows, key=lambda r: r["conf"], reverse=True)
    return [(share, round(agreement(ranked[: max(1, int(len(ranked) * share))], key), 4)) for share in COVERAGE]


def score(run: dict[str, Any], items: list[dict[str, Any]], name: str) -> dict[str, Any]:
    planted = {it["id"]: labels(it, "planted")[name] for it in items}
    out = {"agreement": round(agreement(run["rows"], planted), 3),
           "p50_ms": run["p50_ms"],
           "top_picks": collections.Counter(r["pick"] for r in run["rows"]).most_common(3),
           "risk_coverage": risk_coverage(run["rows"], planted),
           "rows": run["rows"]}
    # Sonnet reference labels are optional — skip if not present
    try:
        sonnet = {it["id"]: labels(it, "sonnet")[name] for it in items}
        out["agreement_sonnet"] = round(agreement(run["rows"], sonnet), 3)
    except KeyError:
        out["agreement_sonnet"] = None
    return out


def evaluate(model: str) -> dict[str, Any]:
    """Both questions for one checkpoint, printed and saved to runs/eval_<name>.json."""
    items_path = TEST_SET
    if not items_path.exists():
        print(f"No test set found at {items_path}. Generate one with build_train_set.py.")
        return {}
    items, agent, out = read_jsonl(items_path), load(model), {}
    for name, question in questions("short").items():
        out[name] = score(ask(agent, items, name, question), items, name)
        s = out[name]
        print(f"{name:9s} vs key {s['agreement']:.3f}  vs Sonnet {s['agreement_sonnet']}  "
              f"p50 {s['p50_ms']} ms  top {s['top_picks']}")
    tag = "base" if model == "base" else Path(model).name
    RUNS.mkdir(exist_ok=True)
    (RUNS / f"eval_{tag}.json").write_text(json.dumps(out, indent=1))
    return out


if __name__ == "__main__":
    evaluate(sys.argv[1])