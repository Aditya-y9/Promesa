"""generate_data_balanced.py — generate a CLASS-BALANCED plan-critique corpus via Ollama.

The earlier generator let the model pick the "planted" label, so common defects
(blocking_io, hardcoded_secret, unbounded_transaction) were over-produced while rare ones
(missing_fallback, heavy_allocation, missing_connection_pool, ...) got 1-3 samples.  As a
*critique agent* it MUST recognise every defect type, so under-represented classes are a real gap.

Fix: this generates **one item per explicitly-requested defect**, cycling through all defect
types round-robin so each appears ~equally (plus 1-in-3 clean).  Each call names the exact
defect to plant, so the class mix is balanced by construction.

Usage (local, no API key):
    python generate_data_balanced.py --train 360 --test 120 --model qwen3:8b
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve()
FINETUNE_DIR = HERE.parent
sys.path.insert(0, str(FINETUNE_DIR))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from plan_critique_task import ARCHETYPES, ARCHETYPE_CONTEXT, CRITERIA  # noqa: E402

DATA = HERE.parent / "data"

DEFECTS = [k for k in CRITERIA if k != "none"]

SINGLE_PROMPT = """You are generating training data for an architectural plan critic.

Write ONE item: a repo_context and a plan.

ARCHETYPES you may draw from (pick one):
{archetype_lines}

PLANTED DEFECT: {defect} ({description})

Rules:
- repo_context: 2-4 sentences describing a service and its operational profile / NFRs.
- plan: 2-4 concise numbered steps.
- If the defect is "none": the plan is architecturally CORRECT and follows the context profile
  (use streaming, vault secrets, circuit breakers, chunking, pagination, etc. as appropriate).
- Otherwise the plan is architecturally WRONG in EXACTLY this defect, and no other; it must seem
  confident and subtle. Do NOT name the defect in the text.

Return ONLY a JSON object (no markdown fences):
{{"repo_context": "...", "plan": "..."}}"""


def _ask_one(client, model: str, defect: str, num_ctx: int = 4096, tries: int = 3) -> dict | None:
    archetype_lines = "\n".join(f"- {a}: {d}" for a, d in ARCHETYPE_CONTEXT.items())
    prompt = SINGLE_PROMPT.format(
        archetype_lines=archetype_lines, defect=defect, description=CRITERIA[defect],
    )
    for attempt in range(tries):
        try:
            resp = client.chat(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                options={"temperature": 0.7, "num_predict": 1024},
            )
            text = resp["message"]["content"]
            item = _parse(text)
            if item:
                return item
        except Exception as e:  # noqa: BLE001
            print(f"    (err on '{defect}': {e})", file=sys.stderr, flush=True)
    return None


def _parse(text: str) -> dict | None:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        return None
    body = text[start: end + 1]
    try:
        d = json.loads(body)
        if isinstance(d, dict) and isinstance(d.get("repo_context"), str) and isinstance(d.get("plan"), str):
            return {"repo_context": d["repo_context"], "plan": d["plan"]}
    except json.JSONDecodeError:
        pass
    result = {}
    for m in re.finditer(r'(\w+)\s*:\s*"((?:[^"\\]|\\.)*)"|(\w+)\s*:\s*(\S[^,\n}]*)', body):
        if m.group(1):
            result[m.group(1)] = m.group(2).replace("\\n", "\n")
        elif m.group(3):
            result[m.group(3)] = m.group(4).strip().strip('"').replace("\\n", "\n")
    if isinstance(result.get("repo_context"), str) and isinstance(result.get("plan"), str):
        return result
    return None


TOPICS = [
    "microservice API refactoring", "data pipeline migration", "authentication gateway rewrite",
    "notification service consolidation", "search index rebuild", "event ingestion overhaul",
    "reporting dashboard microservice", "payment processing service", "user profile service",
    "caching layer redesign", "job scheduler revamp", "rate limiter implementation",
]


def generate(target: int, out: Path, base_id: int, seed: int, client, model: str) -> int:
    rng = random.Random(seed)
    existing = 0
    if out.exists():
        existing = sum(1 for line in out.read_text().splitlines() if line.strip())
    remaining = target - existing
    if remaining <= 0:
        print(f"[gen] already have {existing} >= {target} in {out}", flush=True)
        return 0

    # Plan the label sequence so it is balanced by construction:
    # 1 clean in 3, and every defect type equally among the defective 2/3.
    labels = []
    # clean block
    n_clean = round(target / 3)
    labels += ["none"] * n_clean
    # defective: cycle defects evenly to fill the rest
    labels += [DEFECTS[i % len(DEFECTS)] for i in range(target - n_clean)]
    labels = labels[:target]
    rng.shuffle(labels)

    wrote, failed = 0, 0
    with open(out, "a", encoding="utf-8") as f:
        next_id = base_id + existing
        for defect in labels:
            print(f"[gen] ({existing + wrote}/{target}) planting '{defect}' ...", flush=True)
            t0 = time.time()
            item = _ask_one(client, model, defect)
            if not item:
                failed += 1
                print(f"    failed ({time.time() - t0:.0f}s)", flush=True)
                continue
            f.write(json.dumps({
                "id": next_id + wrote, "topic": rng.choice(TOPICS),
                "archetype": rng.choice(list(ARCHETYPES)),
                "planted": defect if defect in CRITERIA else ("none" if rng.random() < 0.34 else "blocking_io"),
                "repo_context": item["repo_context"].strip(),
                "plan": item["plan"].strip(),
            }, ensure_ascii=False) + "\n")
            wrote += 1
            f.flush()
            if wrote >= remaining:
                break
    print(f"[gen] FINISHED {wrote} written to {out} ({failed} failed)", flush=True)
    return wrote


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--train", type=int, default=360)
    ap.add_argument("--test", type=int, default=120)
    ap.add_argument("--out", type=Path, default=DATA)
    ap.add_argument("--model", default="qwen3:8b")
    ap.add_argument("--seed", type=int, default=20260924)
    args = ap.parse_args()

    global model, client
    model = args.model
    import ollama
    client = ollama.Client(host="http://localhost:11434")
    try:
        client.show(model)
        print(f"Using Ollama model: {model}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"Could not reach model '{model}': {e}", file=sys.stderr, flush=True)
        sys.exit(1)

    DATA.mkdir(parents=True, exist_ok=True)
    print("Generating test set ...")
    generate(args.test, DATA / "test_judged.jsonl", base_id=0, seed=args.seed, client=client, model=model)
    print("Generating train set ...")
    generate(args.train, DATA / "train_generated.jsonl", base_id=args.test, seed=args.seed + 1, client=client, model=model)
    print("Done.")


if __name__ == "__main__":
    main()
