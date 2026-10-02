"""finetune_data.py — turn judged (repo_context, plan) corpus rows into LAYA training sequences.

The target is a one-hot on the row's label: the planted label by default (the reference
both checkpoints are scored against), or the Sonnet judge's label. The sequence is built
by LAYA's own `build_sequence`, with the checkpoint's own budgets, so training sees
exactly what inference will see.

This is a direct port of the jev-demos `finetune_data.py` adapted to the
plan-critique task (REPO_CONTEXT/PLAN → verdict/criterion instead of QUESTION/ANSWER).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from huggingface_hub import snapshot_download
from laya.agent import _fix_tokenizer_config
from laya.common import QTYPES, build_sequence, render_options

from plan_critique_task import CRITERIA, laya_questions

MODEL_ID, SUBFOLDER = "convaiinnovations/laya", "typed-decisions"
SHORT_CRITERIA = {k: k.replace("_", " ") for k in CRITERIA}
DESIGN_CLEAN_SHARE = 1 / 3  # build_train_set plants one clean item in three
TEST_SET = Path(__file__).parent / "data" / "test_judged.jsonl"


def base_checkpoint() -> str:
    """Local path of the zero-shot typed-decisions checkpoint."""
    root = snapshot_download(MODEL_ID, allow_patterns=[f"{SUBFOLDER}/*"])
    path = os.path.join(root, SUBFOLDER)
    _fix_tokenizer_config(path)
    return path


def questions(criterion_labels: str) -> dict[str, dict[str, Any]]:
    """Both questions; the criterion with short labels or full descriptions."""
    qs = laya_questions()
    if criterion_labels == "short":
        qs["criterion"] = {**qs["criterion"], "criteria": SHORT_CRITERIA}
    return qs


def labels(row: dict[str, Any], reference: str) -> dict[str, str]:
    """The answer key for one row: what was planted, or what the Sonnet judge said."""
    if reference == "sonnet":
        return {"verdict": row["gold_verdict"], "criterion": row["gold_criterion"]}
    return {"verdict": "pass" if row["planted"] == "none" else "revise", "criterion": row["planted"]}


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]


def to_items(rows: list[dict[str, Any]], qs: dict[str, dict[str, Any]], tok, cfg,
             reference: str = "planted") -> list[dict[str, Any]]:
    """One training sequence per row per question, dropping any whose markers were cut."""
    items = []
    for row in rows:
        state = {"REPO_CONTEXT": row["repo_context"], "PLAN": row["plan"]}
        for name, q in qs.items():
            internal = {"t": q["type"], "ins": q["instructions"], "crit": q["criteria"]}
            keys = list(q["criteria"])
            ids, markers = build_sequence(tok, state, internal, cfg["max_len"], cfg["head_max_len"])
            if len(markers) != len(render_options(internal)):
                continue
            label = keys.index(labels(row, reference)[name])
            target = [1.0 if i == label else 0.0 for i in range(len(keys))]
            items.append({"ids": ids, "markers": markers, "qtype": QTYPES[q["type"]], "target": target,
                          "label": label, "question": name, "weight": row.get("weight", 1.0)})
    return items
