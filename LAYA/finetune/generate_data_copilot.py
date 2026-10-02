"""generate_data_copilot.py — build the plan-critique training corpus using GitHub Copilot.

No API key is needed. It calls the official Copilot CLI via `copilot -p` (through
`use_copilot.copilot_cli`).  To keep the number of slow CLI invocations low, each call
asks Copilot to return a **batch** of N plan-review items at once.

Each item is a `{repo_context, plan}` pair with a **planted** label:
- 1 in 3 is clean (verdict pass, criterion none);
- the rest have one planted architectural defect.

Usage (from the repo root):
    python LAYA/finetune/generate_data_copilot.py --train 120 --test 40 --batch 6
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
FINETUNE_DIR = HERE.parent
EXPERIMENTS = HERE.parent.parent.parent
sys.path.insert(0, str(FINETUNE_DIR))
sys.path.insert(0, str(EXPERIMENTS))

# UTF-8 output even when piped (Windows cp1252 default)
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from plan_critique_task import ARCHETYPES, ARCHETYPE_CONTEXT, CRITERIA  # noqa: E402
from use_copilot import CopilotError, copilot_cli  # noqa: E402

DATA = HERE.parent / "data"

TOPICS = [
    "microservice API refactoring", "data pipeline migration", "authentication gateway rewrite",
    "notification service consolidation", "search index rebuild", "event ingestion overhaul",
    "reporting dashboard microservice", "payment processing service", "user profile service",
    "caching layer redesign", "job scheduler revamp", "rate limiter implementation",
]

DEFECTS = [k for k in CRITERIA if k != "none"]

BATCH_PROMPT = """You are generating training data for an architectural plan critic.

Produce EXACTLY {n} distinct items.  Each item has a repo_context and a plan, and a hidden
"planted" defect label.  {n_clean} of them must be CLEAN plans (planted: "none" — the plan is
architecturally sound and follows the repo profile).  The rest each have EXACTLY ONE planted defect
from this list (do NOT mention the defect in the plan text): {defects}.

Architecture archetypes you may draw from:
{archetype_lines}

Rules:
- The repo_context is 2-4 sentences describing a service and its operational profile / NFRs.
- A plan is 2-4 concise numbered steps.
- Clean plans use the correct patterns the context demands (streaming, vault secrets, circuit
  breakers, chunking, pagination, etc.).
- Defective plans are architecturally WRONG in exactly one planted defect, subtle and otherwise confident.
- Vary the archetype across items. Vary the topic.

Return ONLY a JSON array, one object per item, EXACTLY like:
[
  {{"repo_context": "...", "plan": "1. ...\\n2. ...", "planted": "blocking_io"}},
  ...
]

The "planted" field is the defect label from the list above, or "none". Use the exact defect keys.
Do NOT add anything outside the JSON array."""


def _ask_batch(n: int, n_clean: int, tries: int = 3) -> list[dict]:
    archetype_lines = "\n".join(f"- {a}: {d}" for a, d in ARCHETYPE_CONTEXT.items())
    prompt = BATCH_PROMPT.format(
        n=n, n_clean=n_clean, defects=", ".join(DEFECTS),
        archetype_lines=archetype_lines,
    )
    for attempt in range(tries):
        print(f"  [call {attempt + 1}/{tries}] asking Copilot for a batch of {n} items ({n_clean} clean) ...", flush=True)
        try:
            res = copilot_cli(prompt, timeout=300)
            print(f"  [call {attempt + 1}/{tries}] copilot rc={res.returncode} ({len(res.stdout)} chars)", flush=True)
            if not res.ok:
                print(f"  [call {attempt + 1}/{tries}] stderr: {res.stderr.strip()[:300]}", flush=True)
                continue
            items = _parse_array(res.stdout)
            print(f"  [call {attempt + 1}/{tries}] parsed {len(items)} items", flush=True)
            if len(items) >= 1:
                return items
        except CopilotError as e:
            print(f"  [call {attempt + 1}/{tries}] copilot error: {e}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"  [call {attempt + 1}/{tries}] unexpected: {e}", flush=True)
    return []


def _parse_array(text: str) -> list[dict]:
    """Parse a JSON array (or ad-hoc [{...},{...}] lines) out of Copilot's reply."""
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end < 0:
        return []
    body = text[start: end + 1]
    try:
        data = json.loads(body)
        return [d for d in data if isinstance(d, dict) and "repo_context" in d and "plan" in d]
    except json.JSONDecodeError:
        pass
    blocks = re.findall(r"\{([^{}]*)\}", body)
    out = []
    for b in blocks:
        item = _parse_block(b)
        if item:
            out.append(item)
    return out


def _parse_block(inner: str) -> dict | None:
    result = {}
    for m in re.finditer(r'(\w+)\s*:\s*"((?:[^"\\]|\\.)*)"|(\w+)\s*:\s*(\S[^,\n}]*)', inner):
        if m.group(1):
            result[m.group(1)] = m.group(2).replace("\\n", "\n")
        elif m.group(3):
            result[m.group(3)] = m.group(4).strip().strip('"').replace("\\n", "\n")
    if "repo_context" in result and "plan" in result and "planted" in result:
        return result
    return None


def generate(target: int, out: Path, base_id: int, seed: int, batch: int) -> int:
    rng = random.Random(seed)
    existing = 0
    if out.exists():
        existing = sum(1 for line in out.read_text().splitlines() if line.strip())
    remaining = target - existing
    if remaining <= 0:
        print(f"[gen] already have {existing} >= {target} in {out}", flush=True)
        return 0

    wrote, failed = 0, 0
    with open(out, "a", encoding="utf-8") as f:
        next_id = base_id + existing
        while wrote < remaining:
            n = min(batch, remaining - wrote)
            n_clean = max(1, n // 3)
            print(f"[gen] need {remaining - wrote} more; requesting batch of {n} -> {out.name}", flush=True)
            items = _ask_batch(n, n_clean)
            if not items:
                failed += 1
                print(f"[gen] batch failed ({failed}); progress {existing + wrote}/{target}", flush=True)
                if failed > 3:
                    print("[gen] too many failed calls; stopping this file", file=sys.stderr, flush=True)
                    break
                continue
            for it in items:
                tree = next_id + wrote
                planted = it.get("planted", "none")
                if planted not in CRITERIA:
                    planted = "none" if rng.random() < 0.34 else rng.choice(DEFECTS)
                f.write(json.dumps({
                    "id": tree, "topic": rng.choice(TOPICS),
                    "archetype": rng.choice(list(ARCHETYPES)),
                    "planted": planted,
                    "repo_context": it["repo_context"].strip(),
                    "plan": it["plan"].strip(),
                }, ensure_ascii=False) + "\n")
                wrote += 1
                if wrote >= remaining:
                    break
            f.flush()
            print(f"[gen] batch wrote {wrote} so far into {out.name}: {existing + wrote}/{target}", flush=True)
    print(f"[gen] FINISHED {wrote} written to {out}", flush=True)
    return wrote


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--train", type=int, default=120)
    ap.add_argument("--test", type=int, default=40)
    ap.add_argument("--batch", type=int, default=6)
    ap.add_argument("--out", type=Path, default=DATA)
    ap.add_argument("--seed", type=int, default=20260924)
    args = ap.parse_args()

    DATA.mkdir(parents=True, exist_ok=True)
    print("Generating test set ...")
    generate(args.test, DATA / "test_judged.jsonl", base_id=0, seed=args.seed, batch=args.batch)
    print("Generating train set ...")
    generate(args.train, DATA / "train_generated.jsonl", base_id=args.test, seed=args.seed + 1, batch=args.batch)
    print("Done.")


if __name__ == "__main__":
    main()
