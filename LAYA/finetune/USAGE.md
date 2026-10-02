# Usage Guide — Getting the Intended Accuracy from the Fine-Tuned Plan Critic

The model is a **System‑1 decision model**, not a chat model: it reads a `REPO_CONTEXT` +
`PLAN` and returns a structured decision (`verdict`: pass/revise + `criterion`: one defect or
`none`). It is **fast (~40 ms)** and deterministic for the same input. Following these tips keeps
accuracy near the measured **0.92 verdict / 0.95 REVISE recall / 13-of-18 defects**.

---

## 1. Feed it the right inputs (the #1 accuracy factor)

The model was trained on this exact shape. Match it.

```
REPO_CONTEXT: 2–4 sentences describing the service AND its operational profile / NFRs.
              Mention the archetype demands: latency/security/resilience/batch keywords.
PLAN:          2–4 concise, numbered steps describing what the plan does.
```

- ✅ **Good** — mirrors training:
  ```
  REPO_CONTEXT: The API is latency-sensitive and must stream, paginate, and pool connections.
  PLAN: 1. Load the full result set into memory. 2. Call the downstream API synchronously per row.
  ```
- ❌ **Weak** — one vague sentence, or a 12-step essay → the pattern signal dilutes.

**Do it in code:**
```python
import laya
from plan_critique_task import laya_questions

agent = laya.load("runs/plan102")           # or run102 / plan_critic
state = {"REPO_CONTEXT": repo_context, "PLAN": plan}
ans = agent.predict(state, laya_questions())["answers"]
print(ans["verdict"]["choice"], "|", ans["criterion"]["choice"])
```

---

## 2. Give each plan a **REPO_CONTEXT**, not just a title
The critic decides *relative to the context*. A plan that's fine for a batch job is a defect for a
low‑latency API. Always pass the repo's operational profile — that's what makes *revise* vs *pass*
correct. Missing context is the main way users get false PASS.

---

## 3. Use the `criterion` to write the "Required Directive"
When `verdict = revise`, the `criterion` names **one** defect (e.g. `blocking_io`,
`hardcoded_secret`, `missing_circuit_breaker`). Map it straight to a directive:

| criterion | directive to give the planner |
|---|---|
| `blocking_io` / `naive_in_memory_collection` | → stream, batch, paginate, non‑blocking I/O |
| `hardcoded_secret` / `raw_query` | → vault‑resolve secrets, parameterize inputs |
| `missing_circuit_breaker` / `unshielded_remote_call` | → add timeout, breaker, bulkhead, fallback |
| `missing_idempotency` | → add idempotency key / dedup |
| `no_chunking` / `unbounded_transaction` | → bounded chunks + memory caps |

`criterion == none` ⇒ the plan is clean ⇒ trust the PASS unless you have independent concerns.

---

## 4. Guard against its remaining blind spots
The balanced model recognises **13/18** defect classes. It is **weaker** on the classes it had
few samples of. Sanity‑check these manually when they matter most:
`blocking_io`, `missing_fallback`, `missing_idempotency`, `open_trust_boundary`, `ignores_do_not`.
- If a *fallback / retry / trust‑boundary / DO‑NOT* concern is load‑bearing, don't rely on the
  criterion alone — read the plan.

---

## 5. Threshold / confidence
The model returns a **confidence** per answer. `ans["verdict"]["confidence"]`.
- High confidence + `revise` → treat as reliable (its REVISE F1 is 0.94).
- Low confidence → surface to a human; don't auto‑block on a shaky verdict.

---

## 6. Parse variations
Laya lets you score **Plan A / B / C** against the same `REPO_CONTEXT`. Keep the state keyed to
each plan and compare:
```python
for plan_id, plan in {"A": plan_a, "B": plan_b, "C": plan_c}.items():
    ans = agent.predict({"REPO_CONTEXT": ctx, "PLAN": plan}, laya_questions())["answers"]
    print(plan_id, ans["verdict"]["choice"], ans["criterion"]["choice"], ans["verdict"]["confidence"])
```

---

## 7. Don't regenerate data unless you must
`data/` already gives the measured accuracy. Only regenerate if you want fresh/domain‑specific
coverage (see `RUNBOOK.md`). If you do, keep **1‑clean‑in‑3 + `--mix balanced`**.

---

## 8. Expected numbers to validate on your machine
Run `python evaluate_critique.py runs/plan102` and confirm you're near:

| | Expected |
|---|---|
| Verdict accuracy | ≥ 0.92 |
| REVISE recall | ≥ 0.94 |
| REVISE precision | ≥ 0.93 |
| Defect classes recognised | ≥ 13/18 |

If you're well below, the usual cause is **input shape** (§1) or a **thin REPO_CONTEXT** (§2) — not the model.
