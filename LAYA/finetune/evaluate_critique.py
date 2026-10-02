"""evaluate_critique.py — critique-focused evaluation of a LAYA plan-critic checkpoint.

The plain evaluate.py reports micro accuracy (agreement).  For a *critique agent* what
actually matters is:

- **Verdict (REVISE) recall**  — did we catch the bad plans? (false negatives are the worst)
- **Verdict (REVISE) precision** — are our "revise" flags right? (false positives erode trust)
- **Per-defect accuracy**    — across ALL defect types (a critic that cannot see rare defects is useless)

Usage:
    python evaluate_critique.py runs/plan101
    python evaluate_critique.py base
"""
from __future__ import annotations

import collections
import json
import sys
import warnings
from pathlib import Path
from typing import Any

from finetune_data import MODEL_ID, SUBFOLDER, TEST_SET, labels, questions, read_jsonl
from plan_critique_task import state_for

RUNS = Path(__file__).parent / "runs"


def load(model: str):
    import laya
    if model == "base":
        from huggingface_hub import snapshot_download
        from laya.agent import _fix_tokenizer_config
        root = snapshot_download(MODEL_ID, allow_patterns=[f"{SUBFOLDER}/*"])
        path = str(Path(root) / SUBFOLDER)
        _fix_tokenizer_config(path)
        return laya.load(path)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return laya.load(str(Path(model).resolve()))


def ask(agent, items, name, question):
    agent.predict(state_for(items[0]["repo_context"], items[0]["plan"]), {name: question})
    rows = []
    for it in items:
        ans = agent.predict(state_for(it["repo_context"], it["plan"]), {name: question})["answers"][name]
        rows.append({"id": it["id"], "pick": ans["choice"].replace(" ", "_")})
    return rows


def crit_metrics(rows, items, name):
    """Precision/recall/F1 per class (or per verdict class if not criterion)."""
    ref = {it["id"]: labels(it, "planted")[name] for it in items}
    classes = sorted(set(ref.values()))
    out = {}
    conf = collections.defaultdict(collections.Counter)
    for r in rows:
        conf[ref[r["id"]]][r["pick"]] += 1
    for cls in classes:
        tp = conf[cls][cls]
        fp = sum(conf[c][cls] for c in classes if c != cls)
        fn = sum(conf[cls][c] for c in classes if c != cls)
        prec = tp / (tp + fp) if (tp + fp) else 0.0
        rec = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
        out[cls] = {"prec": round(prec, 3), "rec": round(rec, 3), "f1": round(f1, 3)}
    return out, conf


def main() -> None:
    model = sys.argv[1] if len(sys.argv) > 1 else "runs/plan101"
    items = read_jsonl(TEST_SET)
    agent = load(model)
    qs = questions("short")
    tag = "base" if model == "base" else Path(model).name

    for name, question in qs.items():
        rows = ask(agent, items, name, question)
        per_class, conf = crit_metrics(rows, items, name)
        correct = sum(1 for r in rows if labels(next(it for it in items if it["id"] == r["id"]), "planted")[name].replace(" ", "_") == r["pick"])
        acc = correct / len(rows)

        print(f"\n=== {name} ({tag})  micro-acc {acc:.3f} ===")
        if name == "verdict":
            for cls in ("pass", "revise"):
                m = per_class.get(cls, {"prec": 0, "rec": 0, "f1": 0})
                print(f"  {cls:6s}  prec {m['prec']}  recall {m['rec']}  f1 {m['f1']}")
        else:
            for cls in sorted(per_class, key=lambda k: per_class[k]["f1"]):
                m = per_class[cls]
                print(f"  {cls:26s}  prec {m['prec']}  recall {m['rec']}  f1 {m['f1']}")
            # Report how many of the defect classes the critic can actually discriminate at all
            n_discrim = sum(1 for cls in per_class if cls != "none" and per_class[cls]["rec"] > 0)
            print(f"  -> recognises {n_discrim}/{len(per_class) - 1} defect classes (recall>0)")

    RUNS.mkdir(exist_ok=True)
    # Save per-class summary
    summary = {}
    for name, question in qs.items():
        rows = ask(agent, items, name, question)
        per_class, _ = crit_metrics(rows, items, name)
        summary[name] = per_class
    (RUNS / f"critique_{tag}.json").write_text(json.dumps(summary, indent=1, default=str))
    print(f"\nwrote runs/critique_{tag}.json")


if __name__ == "__main__":
    main()
