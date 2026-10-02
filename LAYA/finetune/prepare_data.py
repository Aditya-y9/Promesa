"""prepare_data.py — data transformation, run before any training.

    python prepare_data.py --out data/train_all.jsonl --plot plots/mix_train_all.png data/train_judged.jsonl

Three stages:
1. combine every generated source (the first file wins on a duplicate id);
2. drop any row whose plan is a near-copy of a test plan;
3. weight rows back to the generation design (also applied at train time by ``--mix``).

Port of the jev-demos pipeline, adapted to the plan-critique task.
"""
from __future__ import annotations

import argparse
import collections
import copy
import difflib
import json
import re
from pathlib import Path
from typing import Any

from finetune_data import DESIGN_CLEAN_SHARE, TEST_SET, read_jsonl

NEAR_TEST = 0.8  # plan similarity at which a training row counts as a test copy


def normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", text.lower()).strip()


def _test_plans() -> list[str]:
    return [normalise(r["plan"]) for r in read_jsonl(TEST_SET)]


def is_near(plan: str, tests: list[str], cutoff: float = NEAR_TEST) -> bool:
    q = normalise(plan)
    for t in tests:
        sm = difflib.SequenceMatcher(None, q, t)
        if sm.real_quick_ratio() >= cutoff and sm.quick_ratio() >= cutoff and sm.ratio() >= cutoff:
            return True
    return False


def closest_test_plan(plan: str) -> tuple[float, str]:
    q = normalise(plan)
    raw = [r["plan"] for r in read_jsonl(TEST_SET)]
    if not raw:
        return (0.0, "")
    return max((difflib.SequenceMatcher(None, q, normalise(t)).ratio(), t) for t in raw)


def drop_test_copies(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(kept, dropped)."""
    tests = _test_plans()
    if not tests:
        return rows, []
    near = [is_near(r["plan"], tests) for r in rows]
    return [r for r, n in zip(rows, near) if not n], [r for r, n in zip(rows, near) if n]


def weight_to_design(rows: list[dict[str, Any]], clean_share: float = DESIGN_CLEAN_SHARE,
                     balance_types: bool = False) -> None:
    """Weight rows back to the generation design: `clean_share` clean, the rest defective.

    With `balance_types`, every defect type also gets an equal share, as it had at generation.
    """
    n, counts = len(rows), collections.Counter(r["planted"] for r in rows)
    if n == 0:
        return
    defect_types = [t for t in counts if t != "none"]
    for r in rows:
        if r["planted"] == "none":
            r["weight"] = clean_share / (counts["none"] / n)
        elif balance_types:
            r["weight"] = (1 - clean_share) / len(defect_types) / (counts[r["planted"]] / n)
        else:
            r["weight"] = (1 - clean_share) / max(1, (n - counts["none"]) / n)


def weighted(rows: list[dict[str, Any]], mix: str) -> list[dict[str, Any]]:
    """A weighted copy, leaving the input untouched."""
    out = copy.deepcopy(rows)
    weight_to_design(out, balance_types=mix == "balanced")
    return out


def combine(paths: list[str]) -> list[dict[str, Any]]:
    seen: dict[int, dict[str, Any]] = {}
    for path in paths:
        for r in read_jsonl(Path(path)):
            seen.setdefault(r["id"], r)
    return list(seen.values())


def main() -> None:
    import matplotlib
    matplotlib.use("Agg")
    from mix_report import plot, print_table, problems, shares

    ap = argparse.ArgumentParser()
    ap.add_argument("sources", nargs="+")
    ap.add_argument("--out", required=True)
    ap.add_argument("--plot", required=True)
    args = ap.parse_args()
    raw = combine(args.sources)
    kept, dropped = drop_test_copies(raw)
    stages = [("as generated", raw, False), ("after dropping test copies", kept, False),
              ("weighted: clean 1 in 3", weighted(kept, "design"), True),
              ("weighted: types balanced too", weighted(kept, "balanced"), True)]
    print_table(stages)
    for name, rows, is_weighted in stages[1:]:
        print(f"{name:30s} {'; '.join(problems(shares(rows, is_weighted))) or 'on design'}")
    Path(args.plot).parent.mkdir(parents=True, exist_ok=True)
    plot(stages, args.plot, compare=(1, 3))
    Path(args.out).write_text("".join(json.dumps(r) + "\n" for r in kept))
    print(f"dropped {len(dropped)} test copies; wrote {len(kept)} rows to {args.out}; plot {args.plot}")


if __name__ == "__main__":
    main()
