# 🧠 LAYA Critique Agent

A **critique agent** built on the [`laya`](https://pypi.org/project/laya/) System 1
decision engine.  It grades the acceptance criteria of Jira-style tickets and the quality
of Promesa Decision Canvases in this repo, producing **bilingual critique reports**
(English / Bahasa Indonesia).

> **What is LAYA?** LAYA is a fast, *non-autoregressive* (BERT-based) "System 1"
> decision engine that produces **calibrated structured decisions** — enum choices,
> integer scores, and booleans — from a JSON schema.  Unlike generative LLMs that
> produce free text, LAYA returns a decision **plus a principled confidence estimate**
> for every field.  That makes it an excellent fit for *critique*: it decides *how
> good* an AC is (1–5 on each rubric dimension) faster and more repeatably than a
> chat model, and it can be gated on confidence so low-confidence judgments are flagged
> rather than silently trusted.

---

## 📁 Folder layout

```
LAYA/
├── critique/
│   ├── __init__.py            # package exports
│   ├── engine.py              # LayaCritic — wraps the laya decision engine
│   ├── ac_critique.py         # AcceptanceCriteriaCritic — grades AC tickets
│   └── canvas_critique.py     # CanvasCritic — grades Promesa canvases
├── laya_critique.py           # CLI runner (deterministic, reproducible)
└── README.md                  # this file
```

Reports are written to `LAYA/reports/`.

---

## ✨ What it critiques

### 1. Acceptance criteria tickets (`tickets` command)

Grades each ticket in `deepwiki-agent/sample-ac-tickets.json` across the **same 7
rubric dimensions** used by `generate_ac_rank.py` / `promesa_ac_readiness.py`:

| Dimension | Weight | What LAYA decides |
|-----------|--------|-------------------|
| Clarity | 20 | 1–5 — how clear/unambiguous the AC is |
| Testability | 20 | 1–5 — how verifiable/pass-fail each criterion is |
| Specificity | 15 | 1–5 — how quantified/measurable |
| Structure | 10 | 1–5 — BDD/Gherkin structure |
| Edge & negative cases | 15 | 1–5 — error paths, boundaries, empties |
| Outcome focus | 10 | 1–5 — user-observable behaviour vs implementation |
| Risk & blocking signals | 10 | 1–3 + gates — risky/delete/migration etc. |

Each ticket gets an **overall score (0–100)** and a **decision gate**:

- 🟢 **PROCESS** — agent-ready, safe for autonomous execution
- ⚡ **REFINE** — needs improvement before autonomy
- 🛑 **BLOCK** — requires significant rewriting

### 2. Promesa Decision Canvases (`canvases` command)

Grades each `promesa-canvas-*.md` for decision-readiness:

| Dimension | What's assessed |
|-----------|----------------|
| Fences clarity | Are DO / DO-NOT constraints specific and actionable? |
| Edge case coverage | Are error paths, boundaries, empties listed? |
| Strategy angle divergence | Are the A/B/C plans meaningfully different? |
| Decision readiness | Goal, ACs, permissions present so a human can pick? |

---

## 🚀 Usage

### Requirements

```bash
pip install laya
```

> The first online run downloads the LAYA checkpoint (`convaiinnovations/laya`,
> ~421M params, ModernBERT) — a one-time download cached by `huggingface_hub`.
> The script **falls back gracefully** to a deterministic rubric if the model cannot
> load (e.g. no network), so it never hard-fails.

### Commands

```bash
# Score all tickets
python LAYA/laya_critique.py tickets

# Score specific tickets
python LAYA/laya_critique.py tickets --keys PROJ-104 PROJ-109

# Critique all Promesa canvases
python LAYA/laya_critique.py canvases

# Critique one canvas
python LAYA/laya_critique.py canvases --file promesa-canvases/promesa-canvas-PROJ-104.md

# Everything, plus JSON summary
python LAYA/laya_critique.py all --json

# Offline mode (skip download, use deterministic rubric)
python LAYA/laya_critique.py tickets --offline
```

### Outputs

Every run writes to `LAYA/reports/`:

- `laya-critique-<KEY>.md` — one bilingual report per ticket
- `laya-canvas-critique-<KEY>.md` — one bilingual report per canvas
- `laya-critique-all-tickets.md` / `laya-critique-all-canvases.md` — composite
- `laya-critique-tickets.json` — machine-readable summary (with `--json`)

---

## ✍️ Why "critique" agent?

The `deepwiki-agent` extension already *plans* with Gemini and enforces guardrails.
This LAYA agent is the **independent critic** in the loop:

1. It **independently judges ticket quality** before the planning agent consumes it
   (a "gate" that stops bad ACs from wasting a planning run).
2. It **checks the Promesa canvas** is decision-ready before plans A/B/C are generated.
3. It attaches **calibrated confidence** to every grade — low-confidence judgments are
   flagged, so the human knows when to look closer.
4. Reports are **bilingual** (English / Bahasa Indonesia) for the team.

This matches the repo's philosophy: **deterministic, never-assuming, verifiable** — but
adds a fast System 1 *second opinion* on quality.

---

## 🔍 Notes

- **Determinism:** given the same input, the same schema yields the same decision. The
  underlying LAYA checkpoint is a frozen, temperature-calibrated model — no sampling noise.
- **Confidence gating:** the engine tracks `answer_confidence` (max probability) per
  field; a future gate can refuse to auto-process any AC where confidence is low.
- **No secrets:** this agent makes no network calls beyond the one-time model download and
  never touches credentials.

---

## 🔗 Relation to the rest of the repo

| Tool | Role |
|------|------|
| `generate_ac_rank.py` | deterministic 7-dim AC **scorer** (baseline) |
| `promesa_ac_readiness.py` | Promesa readiness **gate** |
| `promesa_canvas.py` | builds the **decision canvas** |
| `deepwiki-agent/` | the executing **coding agent** (plans + guardrails) |
| **`LAYA/` (this)** | the independent **critique agent** / second opinion |
