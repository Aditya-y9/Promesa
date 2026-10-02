# Runbook — Fine-tune LAYA as a Plan Critic (quick)

Concise steps to reproduce on another machine (GPU recommended, e.g. NVIDIA ≥8 GB).

## 0. Prereqs
- Python 3.10+, a **GPU** (RTX 5060/4090/etc.), ~10 GB disk free.
- Install deps:
  ```bash
  pip install laya torch --index-url https://download.pytorch.org/whl/cu128
  pip install transformers safetensors huggingface_hub matplotlib tqdm anthropic
  ```
  (Latest CUDA torch for RTX 50-series: `https://download.pytorch.org/whl/cu128`.)
- Optional (to regenerate data): [Ollama](https://ollama.com) + a local model:
  ```bash
  ollama pull qwen3:8b
  ```

## 1. (Skip — data already in repo) Generate data
```bash
# data/test_judged.jsonl  (89 held-out)
# data/train_generated.jsonl -> data/train_all.jsonl (160 balanced rows, already prepared)
python generate_data_balanced.py --train 162 --test 89 --model qwen3:8b
python prepare_data.py data/train_generated.jsonl --out data/train_all.jsonl --plot plots/mix.png
```

## 2. Train (from the included `data/train_all.jsonl`)
```bash
cd LAYA/finetune
python train.py --train data/train_all.jsonl --mix balanced --out runs/plan_critic --device cuda --epochs 5
```
- Output checkpoint → `runs/plan_critic/` (`model.safetensors`, ~842 MB).
- ~30–40 min on an RTX 5060. Live `tqdm` progress shown.

## 3. Evaluate
```bash
# plain metrics (accuracy)
python evaluate.py runs/plan_critic
# critique metrics (REVISE precision/recall + per-defect coverage)
python evaluate_critique.py runs/plan_critic
```
Expected (balanced run): **VERDICT ≥ 0.92, REVISE recall ≥ 0.94, 13+/18 defect classes**.
Baseline = `base` (zero-shot): `python evaluate.py base`.

## 4. Use the critic
Point `LAYA/laya_critique.py` at the trained checkpoint, or load directly:
```python
import laya
agent = laya.load("runs/plan_critic")
ans = agent.predict({"REPO_CONTEXT": ctx, "PLAN": plan}, questions)["answers"]
```
- `verdict` → `pass` | `revise`; `criterion` → one of 18 defects or `none`.

## Files
- `plan_critique_task.py` — task def (verdict + 19-way criterion). **Do not change** once data exists.
- `generate_data_balanced.py` — balanced data generation (local, no API key).
- `train.py` — RLCD training (+ `--device`). `save()` writes `model.safetensors`.
- `prepare_data.py` / `mix_report.py` — dedup + class-mix check.
- `evaluate.py` / `evaluate_critique.py` — plain + critique metrics.
- `data/` — **already included; reuse as-is** (do not regenerate unless you want fresh data).
- `REPORT.md` — results on our run.

> **Gotchas**
> - `laya.load` requires `model.safetensors` (train saves it; a `model.pt` won't load).
> - Keep 1-clean-in-3 + `--mix balanced` — the class balance is what made it work.
