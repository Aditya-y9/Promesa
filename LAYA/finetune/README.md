# 🎯 Fine-tuning LAYA as a Plan Critic

This folder fine-tunes **LAYAG** (Convai, 421M parameters, Apache-2.0) to be a
**plan critic** for the exact job in your `plan-critique-prompt.md`:

> **Verdict** — does a candidate implementation plan (Plans A/B/C) align with the repo's
> operational profile and non-functional requirements?
> **Criterion** — *which* architectural defect makes it fail that test?

The pipeline is a faithful port of the [mani-aiml/jev-demos `laya-finetune/`](https://github.com/mani-aiml/jev-demos)
methodology — the **same RLCD training objective, the same data checks (test copies, class mix,
planted labels), and the same temperature calibration** — but with **entirely new synthetic data**:
each item is a `{REPO_CONTEXT, PLAN}` pair instead of a Q&A pair, and the defect set is the
architectural anti-patterns your prompt tells the critic to reject.

> **Not the repo's data.** Nothing from `jev-demos` is reused as training content. We only reuse the
> *pipeline code and methodology*; the task definition, archetypes, defect rubric, generator prompts, and
> class-mix target are all original and match your plan-critique rubric.

---

## The job

Each item under judgement:

```
REPO_CONTEXT: The service is performance-intensive and latency-sensitive. It must stream and
  paginate; no unbounded in-memory buffering, no blocking calls, no connection-per-request.
PLAN:
  1. Load the full result set into a HashSet and filter in memory.
  2. For each record, call the downstream HTTP API synchronously in a loop.
  3. Retry each failed call up to 15 times with a 1-second sleep.
```

**planted**: `blocking_io` (and verdict `revise`).

Two LAYA questions:

| question | type | options | purpose |
|---|---|---|---|
| `verdict` | 2-way choice | `pass` / `revise` | the high-volume gate call |
| `criterion` | N-way choice | 17 architectural defects + `none` | the prescriptive `REQUIRED DIRECTIVES` lookup |

**Defect rubric** (mirrors the prompt's four archetypes):

| archetype | anti-patterns to REJECT |
|---|---|
| **Performance / Low-Latency** | in-memory collection, blocking I/O, heavy allocation, unbounded retries, missing pagination, missing connection pool |
| **Security / Compliance** | hardcoded secret, raw query, open trust boundary, missing audit logging, overly risky |
| **Resilience / Distributed** | unshielded remote call, missing circuit breaker, missing idempotency, missing fallback, unbounded retries |
| **Batch / High-Throughput** | unbounded transaction, no chunking, missing pagination, no memory cap, ignores DO-NOT |

---

## Pipeline

```bash
cd LAYA/finetune
python3 -m venv .venv && (source .venv/bin/activate || .venv\Scripts\activate)
pip install -r requirements.txt
cp .env.example .env          # put ANTHROPIC_API_KEY in it

# 1. data: generate + gold-label synthetic plan-critique items (paid Claude calls)
python build_train_set.py 2000
python prepare_data.py data/train_judged.jsonl \
    --out data/train_all.jsonl --plot plots/mix_train_all.png

# 2. train: re-checks the mix first (runs/<name>/mix.png), refuses a skewed one
python train.py --train data/train_all.jsonl --mix balanced --out runs/plan101 --device cuda

# 3. score on the held-out items
python evaluate.py base
python evaluate.py runs/plan101
```

| File | Does |
|---|---|
| `plan_critique_task.py` | the task definition: verdict + criterion, archetypes, defect rubric |
| `build_train_set.py` | generates synthetic plan items with **planted** labels via Claude Haiku, gold-labels via Sonnet |
| `finetune_data.py` | rows → LAYA training sequences via LAYA's own `build_sequence` |
| `prepare_data.py` | combine, drop test copies, weight to the generation design |
| `mix_report.py` | class-mix table, plot, pre-flight check |
| `train.py`, `calibrate.py` | port of Convai's fine-tuning (RLCD loss) + temperature fit |
| `evaluate.py` | scoring on the held-out items |

### The three data checks (learned the hard way in jev-demos — kept here)

1. **No test copies** — drop any training plan that is a near-copy of a test plan.
2. **Check the mix after every change** — clean vs defective, and every defect type equal.
   The target is the generation design (1 clean in 3, one place per defect type), never the
   test labels.
3. **Planted labels are the answer key** — a paid judge is a *quality check*, not a requirement.

---

## Training the exact checkpoint

`build_train_set.py` currently points the generator at **Claude Haiku** and the gold labeler at
**Claude Sonnet**. You can:
- raise the `target` to whatever corpus size you can afford;
- edit `build_train_set.py` to draw from **your own** crisis plans instead of the synthetic prompts
  (keep a `repo_context` + `plan` + `planted` per row).

The fine-tuned model is **not** distributed — train your own (`runs/*/`, ~840 MB each).

> **Environment note.** Your current `experiments/.venv` is **CPU-only torch** and the LAYA English
> checkpoint is ~421M parameters, so a real run belongs on a GPU box (`--device cuda`, or an Apple
> Silicon machine with MPS like the reference project). This code auto-detects the device; on CPU it will
> run but far too slowly to train a full checkpoint.

---

## Relation to the rest of `LAYA/`

| Folder | Role |
|---|---|
| `../critique/` | the **already-built** LAYA plan/AC critic that scores tickets (`laya_critique.py`) |
| `finetune/` (this) | the **training pipeline** that produces a LAYA checkpoint specialised for the plan-critique job |

Once trained, point a future version of the critic at `runs/plan101` so the gate uses the
fine-tuned model instead of the stock checkpoint.
