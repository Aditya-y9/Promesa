"""build_train_set.py — generate synthetic plan-critique training items.

Each item is a ``{REPO_CONTEXT, PLAN}`` pair with a **planted** label.  The generator
is a Claude Haiku prompt that asks for a plan-review pair around one of the four
repo archetypes (performance, security, resilience, batch) and optionally plants a
specific architectural defect.

Design (matching the jev-demos methodology):
- 1 in 3 items is **clean** (verdict = pass, criterion = none).
- The rest have one **planted defect** from ``plan_critique_task.CRITERIA``.

Usage:
    # Generate 100 items (takes ~$0.50 in Haiku API calls)
    python build_train_set.py 100

    # Generate 2000 items for a full training run
    python build_train_set.py 2000

Environment:
    ANTHROPIC_API_KEY must be set in .env (see .env.example).
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import sys
from pathlib import Path
from typing import Any

from anthropic import APIStatusError, AsyncAnthropic

# Path setup so we can import the task definition
HERE = Path(__file__).parent.resolve()
sys.path.insert(0, str(HERE.parent))  # LAYA/

from finetune.plan_critique_task import (
    ARCHETYPES,
    ARCHETYPE_CONTEXT,
    CRITERIA,
    DEFECT_ARCHETYPES,
)

DATA = HERE / "data"
GENERATED = DATA / "train_generated.jsonl"
JUDGED = DATA / "train_judged.jsonl"

GENERATOR_MODEL = "claude-haiku-4-5-20251022"
GOLD_MODEL = "claude-sonnet-5-20251022"

TOPICS = [
    "microservice API refactoring", "data pipeline migration", "authentication gateway rewrite",
    "notification service consolidation", "search index rebuild", "event ingestion overhaul",
    "reporting dashboard microservice", "payment processing service", "user profile service",
    "caching layer redesign", "job scheduler revamp", "rate limiter implementation",
]

# ── Generator prompts ──────────────────────────────────────────────────────────

CLEAN_PROMPT = """You are a senior architect writing a <REPO_CONTEXT> and a candidate <PLAN> for it.

ARCHETYPE: {archetype} — {archetype_name}
REPO_CONTEXT BASELINE: {context_text}

Write a brief, realistic REPO_CONTEXT (3-5 sentences) about the service, then a PLAN (2-4 steps)
that is architecturally sound and follows the context profile correctly. The plan should use
the correct patterns (streaming, vault secrets, circuit breakers, chunking, etc.) as the context demands.

Return strict JSON:
{{
    "repo_context": "...",
    "plan": "..."
}}"""

GEN_PROMPT = """You are a senior architect writing a <REPO_CONTEXT> and a candidate <PLAN> for it.

ARCHETYPE: {archetype} — {archetype_name}
REPO_CONTEXT BASELINE: {context_text}

PLANTED DEFECT: {defect} ({description})

Write a brief, realistic REPO_CONTEXT (3-5 sentences) about the service, then a PLAN (2-4 steps)
that is architecturally *wrong* in exactly this defect. The plan should seem confident and otherwise
correct. The defect must be *subtle* — the kind a competent architect actually produces under pressure.

Do NOT name the defect in the output. Just produce a flawed plan that a knowledgeable critic
would recognise as having this defect.

Return strict JSON:
{{
    "repo_context": "...",
    "plan": "..."
}}"""

GOLD_PROMPT = """You are a senior architectural critic applying the plan-critique rubric.

REPO_CONTEXT: {repo_context}
PLAN: {plan}

Decide two things:
1. verdict: "pass" if the plan is architecturally sound and aligned with the repo context; "revise" otherwise.
2. criterion: the single best-fitting label from this list for what is wrong ("none" if nothing is):
{criteria}

Return strict JSON: {{"verdict": "...", "criterion": "..."}}"""


def _listing() -> str:
    """Each defect with its description, for the gold prompt."""
    return "\n".join(f"  {k}: {v}" for k, v in CRITERIA.items())


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _text_of(msg: Any) -> str:
    """The text blocks only. Sonnet-like models may put a ThinkingBlock first."""
    blocks = []
    for b in msg.content:
        if hasattr(b, "text"):
            blocks.append(b.text)
    return "\n".join(blocks)


async def json_call(
    client: AsyncAnthropic, model: str, prompt: str, tries: int = 6
) -> tuple[dict[str, Any] | None, dict[str, int]]:
    """One call that must return a JSON object, plus what it cost in tokens."""
    for attempt in range(tries):
        try:
            msg = await client.messages.create(
                model=model, max_tokens=2000, messages=[{"role": "user", "content": prompt}]
            )
            usage = {"in": msg.usage.input_tokens, "out": msg.usage.output_tokens}
            text = _text_of(msg)
            start, end = text.find("{"), text.rfind("}")
            parsed = json.loads(text[start: end + 1]) if start >= 0 else None
            return parsed, usage
        except APIStatusError as err:
            if err.status_code not in (429, 500, 502, 503, 529) or attempt == tries - 1:
                raise
            await asyncio.sleep(2**attempt)


async def generate(client: AsyncAnthropic, target: int, concurrency: int = 10) -> None:
    """Top the generated corpus up to `target` items."""
    existing = read_jsonl(GENERATED)
    existing_ids = {r["id"] for r in existing}
    needed = target - len(existing_ids)
    if needed <= 0:
        print(f"Already have {len(existing_ids)} items; target is {target}, nothing to do.")
        return
    print(f"Need {needed} new items (have {len(existing_ids)}, target {target})")

    sem = asyncio.Semaphore(concurrency)
    lock = asyncio.Lock()
    defects = [k for k in CRITERIA if k != "none"]
    done = 0

    async def one(index: int) -> None:
        nonlocal done
        topic = random.choice(TOPICS)
        archetype = random.choice(list(ARCHETYPES))
        planted = "none" if index % 3 == 0 else random.choice(defects)

        if planted == "none":
            prompt = CLEAN_PROMPT.format(
                archetype=archetype, archetype_name=ARCHETYPES[archetype],
                context_text=ARCHETYPE_CONTEXT[archetype],
            )
        else:
            prompt = GEN_PROMPT.format(
                archetype=archetype, archetype_name=ARCHETYPES[archetype],
                context_text=ARCHETYPE_CONTEXT[archetype],
                defect=planted, description=CRITERIA[planted],
            )
        async with sem:
            pair, usage = await json_call(client, GENERATOR_MODEL, prompt)
        if pair and "repo_context" in pair and "plan" in pair:
            async with lock:
                append_jsonl(GENERATED, {
                    "id": index, "topic": topic, "archetype": archetype,
                    "planted": planted, **pair,
                })
                done += 1
                if done % 25 == 0:
                    print(f"  generated {done}/{needed} ...")

    tasks = [one(i) for i in range(needed)]
    print(f"Generating {needed} items (concurrency {concurrency}) ...")
    await asyncio.gather(*tasks)
    print(f"Done: {done} items written to {GENERATED}")


async def gold_label(client: AsyncAnthropic, concurrency: int = 4) -> None:
    """Grade every generated item that has no gold label yet."""
    existing = {r["id"] for r in read_jsonl(JUDGED)}
    rows = [r for r in read_jsonl(GENERATED) if r["id"] not in existing]
    if not rows:
        print("All items have gold labels already.")
        return
    print(f"Gold-labelling {len(rows)} items ...")

    sem = asyncio.Semaphore(concurrency)
    lock = asyncio.Lock()
    listing = _listing()
    failures = 0

    async def one(item: dict[str, Any]) -> None:
        nonlocal failures
        prompt = GOLD_PROMPT.format(
            repo_context=item["repo_context"], plan=item["plan"], criteria=listing,
        )
        async with sem:
            got, usage = await json_call(client, GOLD_MODEL, prompt)
        verdict, criterion = (got or {}).get("verdict"), (got or {}).get("criterion")
        if verdict not in {"pass", "revise"} or criterion not in CRITERIA:
            failures += 1
            return
        async with lock:
            append_jsonl(JUDGED, {
                **item, "gold_verdict": verdict, "gold_criterion": criterion,
                "gold_tokens_in": usage.get("in", 0), "gold_tokens_out": usage.get("out", 0),
            })

    tasks = [one(r) for r in rows]
    await asyncio.gather(*tasks)
    print(f"Labelled {len(rows) - failures} items ({failures} failures)")


def report() -> None:
    rows = read_jsonl(JUDGED)
    if not rows:
        print("No judged items yet.")
        return
    recovered = sum(1 for r in rows if r["planted"] == r["gold_criterion"])
    agreed = sum(1 for r in rows if (r["planted"] == "none") == (r["gold_verdict"] == "pass"))
    rework = sum(1 for r in rows if r["gold_verdict"] == "revise")
    print(f"\nitems: {len(rows)}   gold says revise: {rework} ({rework/len(rows):.1%})")
    print(f"gold recovered the planted criterion: {recovered}/{len(rows)} ({recovered/len(rows):.1%})")
    print(f"gold verdict matched planted:         {agreed}/{len(rows)} ({agreed/len(rows):.1%})")
    types = {r["gold_criterion"] for r in rows}
    print(f"distinct gold criteria present: {len(types)} of {len(CRITERIA)}")


async def main(target: int) -> None:
    client = AsyncAnthropic()
    await generate(client, target)
    await gold_label(client)
    report()


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1])))