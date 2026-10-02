# Fine-tuning Report — LAYA as a Plan Critic

**Date:** 2026-10-02 · **Model:** LAYA (Convai, 421M, `typed-decisions` checkpoint)
**Hardware:** NVIDIA RTX 5060 Laptop (8 GB, compute cap 12.0) · **Torch:** 2.11.0+cu128
**Task:** Criticise candidate implementation plans (A/B/C) against repo context
→ **Verdict** (PASS / REVISE) + **Criterion** (19-way architectural defect ID).

---

## 1. Data (no API key — local Ollama)

| Set | Source | Rows | Clean share |
|---|---|---|---|
| Test | `data/test_judged.jsonl` | 89 | 1-in-3 |
| Train | `data/train_all.jsonl` | 160 | 35.6% → reweighted 33.3% |

- Generated with `generate_data_balanced.py` (Ollama **qwen3:8b**, local GPU).
- **Balance fixed by construction:** each defect planted explicitly (round-robin), so all 18
  defect classes are represented (vs. the first attempt where 8 defect types had only 0–3 samples).
- Data checks kept: test-copy dedup, class-mix preflight (`--mix balanced`), planted labels as key.

> **Key lesson:** class imbalance was the #1 blocker for a good critic — the earlier imbalanced
> model had **zero recall on 8 defect classes**.

---

## 2. Training

- Objective: **RLCD** (policy-gradient on a proper-scoring reward + soft cross-entropy), Convai/jev-demos port.
- 160 rows → 288 train / 32 calibration sequences · **5 epochs · 180 steps · ~34 min**.
- Temps fitted (choice/score/noul): `[1.845, 1.0, 1.0]` · Checkpoint → `runs/plan102/` (`model.safetensors`, 842 MB).
- Live `tqdm` progress bar in the terminal.

---

## 3. Results — critique metrics (89 held-out items)

### Verdict (PASS / REVISE) — the decision that matters

| Metric | Imbalanced `plan101` | **Balanced `plan102`** |
|---|---|---|
| Accuracy | 0.775 | **0.921** |
| REVISE precision | 0.842 | **0.933** |
| **REVISE recall** (catches bad plans) | 0.814 | **0.949** |
| REVISE F1 | 0.828 | **0.941** |

### Criterion (19-way architectural defect) — can it *see* every failure mode?

| Metric | Imbalanced `plan101` | **Balanced `plan102`** |
|---|---|---|
| Accuracy | 0.483 | **0.663** |
| Defect classes recognised (recall>0) | 8/18 | **13/18** |

Notable per-defect F1 gains: `no_chunking` 0 → **1.0**, `unbounded_retries` 0.857 → **1.0**,
`naive_in_memory_collection` 0 → **0.857**, `missing_connection_pool` 0 → **0.667**.

---

## 4. Verdict

✅ **The fine-tuned critic is dramatically better for its job.** It now **catches 95% of bad
plans** (REVISE recall 0.949) and recognises 13/18 distinct architectural defects (was 8/18).
Use `runs/plan102/` as the backing model for the `LAYA/laya_critique.py` critic.
