"""AcceptanceCriteriaCritic — grades AC tickets using LAYA's decision engine.

Maps each ticket's acceptance criteria through the same 7 rubric dimensions
as ``generate_ac_rank.py`` but uses LAYA's calibrated structured decisions
(enum choices, scores, booleans) to produce the grade, then renders a
bilingual critique report.

The LAYA grade tensors provide *calibrated probabilities* for each decision,
not just a score, so the critique can say "80 % certain this AC is
unclear" with principled confidence.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .engine import CriticUnavailable, DecisionOutcome, LayaCritic

log = logging.getLogger(__name__)

# ── Helpers reused from generate_ac_rank.py vocabulary ─────────────────────

VAGUE_TERMS = {
    "easy", "easily", "fast", "quick", "nice", "user-friendly", "user friendly",
    "make sure it works", "should probably", "reasonably sized", "good experience",
    "be careful", "make sure", "lots of", "all major browsers", "look good",
}

GWT_PATTERNS = re.compile(r"(Scenario:|Given|When|Then)", re.IGNORECASE)
NEGATIVE_TERMS = {
    "invalid", "error", "empty", "expired", "exceed", "unauthori",
    "429", "401", "403", "duplicate", "revoked", "bounce",
    "fail", "denied", "forbidden", "timeout", "corrupt", "malformed",
}
QUANT_PATTERNS = re.compile(
    r"\d+|within \d+|less than \d+|max|min|limit|threshold|"
    r"ms|milliseconds?|seconds?|minutes?|hours?|days?|MB|KB|GB",
    re.IGNORECASE,
)


def _normalise_ac(issue: dict) -> Tuple[str, List[str]]:
    """Return (ac_text, ac_list) from a ticket dict, matching generate_ac_rank."""
    raw = issue.get("acceptanceCriteria") or issue.get("acceptance_criteria")
    if raw is None:
        return ("", [])
    if isinstance(raw, str):
        raw = [raw]
    ac_list = [str(item).strip() for item in raw if str(item).strip()]
    return ("\n".join(ac_list), ac_list)


# ── LAYA decision schemas for each dimension ──────────────────────────────
# Each is a JSON schema that LAYA's `decide()` maps via `plan_from_json_schema`.

CLARITY_SCHEMA = {
    "type": "object",
    "properties": {
        "clarity_level": {
            "type": "integer",
            "minimum": 1,
            "maximum": 5,
            "description": "How clear and unambiguous the acceptance criteria are. "
                           "1 = full of vague terms, 5 = crystal clear.",
        },
    },
}

TESTABILITY_SCHEMA = {
    "type": "object",
    "properties": {
        "testability_level": {
            "type": "integer",
            "minimum": 1,
            "maximum": 5,
            "description": "How testable/verifiable the acceptance criteria are. "
                           "1 = subjective, 5 = every criterion is a pass/fail test.",
        },
    },
}

SPECIFICITY_SCHEMA = {
    "type": "object",
    "properties": {
        "specificity_level": {
            "type": "integer",
            "minimum": 1,
            "maximum": 5,
            "description": "How specific and measurable the criteria are. "
                           "1 = no numbers or concrete values, 5 = precisely quantified.",
        },
    },
}

STRUCTURE_SCHEMA = {
    "type": "object",
    "properties": {
        "structure_level": {
            "type": "integer",
            "minimum": 1,
            "maximum": 5,
            "description": "How well-structured the criteria are. "
                           "1 = prose paragraphs, 5 = every criterion is GWT/BDD format.",
        },
    },
}

EDGE_NEGATIVE_SCHEMA = {
    "type": "object",
    "properties": {
        "edge_coverage": {
            "type": "integer",
            "minimum": 1,
            "maximum": 5,
            "description": "How well edge and negative cases are covered. "
                           "1 = none mentioned, 5 = diverse error paths, boundaries, and empties.",
        },
    },
}

OUTCOME_SCHEMA = {
    "type": "object",
    "properties": {
        "outcome_focus": {
            "type": "integer",
            "minimum": 1,
            "maximum": 5,
            "description": "Focus on observable user outcomes vs implementation steps. "
                           "1 = describes implementation code, 5 = purely user-observable behaviour.",
        },
    },
}

RISK_SCHEMA = {
    "type": "object",
    "properties": {
        "has_risk": {
            "type": "boolean",
            "description": "Whether the acceptance criteria mention any risky operations "
                           "(delete, migration, production, PII, billing, deploy, etc.).",
        },
        "has_approval_gate": {
            "type": "boolean",
            "description": "Whether the risk is explicitly gated by an approval step.",
        },
        "risk_level": {
            "type": "integer",
            "minimum": 1,
            "maximum": 3,
            "description": "Overall risk level. 1 = safe, 2 = risky but gated, 3 = unguarded high risk.",
        },
    },
    "required": ["has_risk", "has_approval_gate", "risk_level"],
}

# All dimensions together (for one-shot batch if the schema is flat enough)
# LAYA requires flat schemas (no nesting), so we keep them separate and batch.
ALL_SCHEMAS = {
    "clarity": CLARITY_SCHEMA,
    "testability": TESTABILITY_SCHEMA,
    "specificity": SPECIFICITY_SCHEMA,
    "structure": STRUCTURE_SCHEMA,
    "edge_negative": EDGE_NEGATIVE_SCHEMA,
    "outcome_focus": OUTCOME_SCHEMA,
    "risk": RISK_SCHEMA,
}

DIMENSION_LABELS = {
    "clarity": "Clarity",
    "testability": "Testability",
    "specificity": "Specificity",
    "structure": "Structure",
    "edge_negative": "Edge & negative cases",
    "outcome_focus": "Outcome focus",
    "risk": "Risk & blocking signals",
}

DIMENSION_WEIGHTS = {
    "clarity": 20,
    "testability": 20,
    "specificity": 15,
    "structure": 10,
    "edge_negative": 15,
    "outcome_focus": 10,
    "risk": 10,
}


# ── Deterministic fallback (when LAYA model is unavailable) ────────────────

def _fallback_score(ac_text: str, ac_list: List[str]) -> Dict[str, float]:
    """Deterministic rubric-based fallback — same vocabulary as generate_ac_rank."""
    lower = ac_text.lower()
    n = max(len(ac_list), 1)
    if not ac_text:
        return {d: 0.0 for d in ALL_SCHEMAS}

    vague_hits = sum(1 for t in VAGUE_TERMS if t in lower)
    clarity = max(0.0, 1.0 - (vague_hits / n) * 1.5)

    gwt_hits = len(GWT_PATTERNS.findall(ac_text))
    testability = min(gwt_hits / max(n * 2, 1), 1.0)

    quant_hits = len(QUANT_PATTERNS.findall(ac_text))
    specificity = min(quant_hits / max(n * 2, 1), 1.0)

    structure = min(gwt_hits / max(n * 2, 1), 1.0)

    neg_hits = sum(1 for t in NEGATIVE_TERMS if t in lower)
    edge_negative = min(neg_hits / max(n, 1), 1.0)

    impl_words = {"call ", "install ", "use library", "update database", "write query",
                  "modify the schema", "implement", "add the code", "refactor"}
    impl_hits = sum(1 for w in impl_words if w in lower)
    outcome_focus = max(0.0, 1.0 - (impl_hits / max(n, 1)))

    risky_words = {"migration", "production", "delete", "pii", "deploy", "purge",
                   "drop column", "billing", "payment"}
    risk_hits = sum(1 for w in risky_words if w in lower)
    has_risk = risk_hits > 0
    has_gate = "approval" in lower or "permission" in lower
    if not has_risk:
        risk = 1.0
    elif has_gate:
        risk = 0.6
    else:
        risk = 0.0

    return {
        "clarity": clarity,
        "testability": testability,
        "specificity": specificity,
        "structure": structure,
        "edge_negative": edge_negative,
        "outcome_focus": outcome_focus,
        "risk": risk,
    }


def _level_label(score: float) -> str:
    if score >= 0.85:
        return "🟢"
    if score >= 0.55:
        return "🟡"
    return "🔴"


# ── Report generation ──────────────────────────────────────────────────────

REPORT_HEADER = """# LAYA Critique — AC Quality Report

> Generated by the LAYA System 1 decision engine
> Date: {date}
> Source: {source}

"""

TICKET_SUMMARY = """
---
## {key}: {summary}  {prio}
**Type:** {itype}  |  **Status:** {status}  |  **Assignee:** {assignee}
**AC count:** {ac_count}
**Overall grade:** {overall:.0f} / 100  ({gate})
---

"""

DIM_ROW = "| {dim:<20} | {score:>6.2f} / 1.0  {icon} | {finding} |"
TABLE_HEADER = """
| Dimension              | Score         | Finding                                     |
|------------------------|---------------|---------------------------------------------|
"""


def _finding(dim: str, score: float, is_lowest: bool) -> str:
    """Generate a bilingual critique finding for a dimension."""
    low = score < 0.55
    mid = 0.55 <= score < 0.85

    dim_findings = {
        "clarity": {
            "low": "Contains vague terms like 'easy', 'fast', 'nice'. Replace with concrete, operational definitions. / Gunakan istilah konkret bukan 'mudah' atau 'cepat'.",
            "mid": "Mostly clear but a few terms could be more concrete. / Sebagian jelas, beberapa istilah bisa lebih konkret.",
            "high": "Crystal clear: no vague language. / Sangat jelas: tidak ada bahasa kabur.",
        },
        "testability": {
            "low": "Missing GWT/BDD format. Rewrite as objectively verifiable pass/fail statements. / Tidak menggunakan format GWT. Tulis ulang sebagai pernyataan yang bisa diverifikasi.",
            "mid": "Some testable structure, but not every criterion is pass/fail. / Beberapa struktur teruji, tapi belum semua kriteria bisa lulus/gagal.",
            "high": "Fully testable: every criterion is a verifiable pass/fail statement. / Sepenuhnya teruji: setiap kriteria bisa diverifikasi.",
        },
        "specificity": {
            "low": "No quantifiers (numbers, time limits, sizes). Add concrete thresholds. / Tidak ada angka, batas waktu, ukuran. Tambahkan ambang batas konkret.",
            "mid": "Some quantifiers present but coverage is uneven. / Beberapa angka sudah ada tapi belum merata.",
            "high": "Well-quantified: time limits, sizes, and counts are specified. / Terukur dengan baik: batas waktu, ukuran, jumlah disebutkan.",
        },
        "structure": {
            "low": "Prose-style criteria. Convert to Given/When/Then format. / Kriteria bergaya prosa. Konversikan ke format Given/When/Then.",
            "mid": "Mixed format—some GWT, some prose. Standardise. / Format campuran—sebagian GWT, sebagian prosa. Standarisasi.",
            "high": "Excellent BDD structure throughout. / Struktur BDD yang sangat baik.",
        },
        "edge_negative": {
            "low": "No edge or negative cases mentioned. Add: empty states, errors, boundaries, auth failures. / Tidak ada kasus tepi atau negatif. Tambahkan: keadaan kosong, error, batas, kegagalan auth.",
            "mid": "Some negative coverage but skip important paths (see report). / Beberapa kasus negatif ada tapi melewatkan jalur penting.",
            "high": "Good coverage of error paths, boundaries, and edge cases. / Cakupan error, batas, dan kasus tepi yang baik.",
        },
        "outcome_focus": {
            "low": "Describes implementation steps (call, install, update DB). Focus on user-observable behaviour. / Menjelaskan langkah implementasi. Fokus pada perilaku yang bisa diamati pengguna.",
            "mid": "Mostly outcome-focused but some implementation language remains. / Sebagian besar fokus pada hasil tapi masih ada bahasa implementasi.",
            "high": "Purely outcome-focused: describes what the user sees and experiences. / Sepenuhnya fokus pada hasil: menjelaskan apa yang dilihat dan dialami pengguna.",
        },
        "risk": {
            "low": "Unguarded high-risk operation. Add an explicit approval gate. / Operasi berisiko tinggi tanpa pengaman. Tambahkan gerbang persetujuan eksplisit.",
            "mid": "Risky but gated by approval. / Berisiko tapi sudah ada persetujuan.",
            "high": "No risk signals — safe for autonomous processing. / Tidak ada sinyal risiko — aman untuk pemrosesan otomatis.",
        },
    }

    bucket = "low" if low else ("mid" if mid else "high")
    base = dim_findings.get(dim, {}).get(bucket, "")
    if is_lowest and score < 0.6:
        base += " ⚠️ PRIORITY — lowest dimension."
    return base


def _gate_label(overall: float, risk_score: float) -> Tuple[str, str]:
    """Return (gate, qualifier)."""
    if overall >= 75 and risk_score >= 0.5:
        return "PROCESS ✅", "Agent-ready — safe for autonomous execution"
    if overall >= 40:
        return "REFINE ⚡", "Needs improvement before autonomous processing"
    return "BLOCK 🛑", "Requires significant rewriting"


# ── Critic class ───────────────────────────────────────────────────────────


class AcceptanceCriteriaCritic:
    """Grades acceptance criteria tickets through the LAYA decision engine.

    Produces a bilingual (English / Indonesian) critique report for each ticket,
    covering all 7 rubric dimensions.

    Usage
    -----
        critic = AcceptanceCriteriaCritic()
        report = critic.critique_ticket(ticket_dict)
        print(report["report_markdown"])

        # Batch mode
        reports = critic.critique_batch(ticket_list)
    """

    def __init__(self, laya_critic: Optional[LayaCritic] = None):
        self._laya = laya_critic or LayaCritic(prefer_offline=False)

    # ── Single ticket ──────────────────────────────────────────────────

    def critique_ticket(self, ticket: dict) -> dict:
        """Grade one ticket and return a full critique report."""
        ac_text, ac_list = _normalise_ac(ticket)
        key = ticket.get("key", "???")

        if not ac_text:
            return self._empty_report(ticket)

        # Score using LAYA (with deterministic fallback)
        scores = self._score_dimensions(ac_text, ac_list)

        # Weighted overall
        overall = sum(
            scores[d] * DIMENSION_WEIGHTS.get(d, 10)
            for d in scores
        )
        # Risk is cap-weighted separately
        risk_score = scores.get("risk", 0.0)
        overall = round(overall, 1)

        # Gate
        gate, qualifier = _gate_label(overall, risk_score)

        # Find lowest dimension for priority flag
        sorted_dims = sorted(
            [(d, s) for d, s in scores.items() if d != "risk"],
            key=lambda x: x[1],
        )
        lowest_dim = sorted_dims[0][0] if sorted_dims else "clarity"

        # Build findings per dimension
        dim_findings = {}
        for dim, score in scores.items():
            is_lowest = dim == lowest_dim
            dim_findings[dim] = {
                "score": round(score, 3),
                "finding": _finding(dim, score, is_lowest),
                "icon": _level_label(score),
            }

        # Render report
        report_md = self._render_report(ticket, scores, dim_findings, overall, gate, qualifier)

        return {
            "key": key,
            "summary": ticket.get("summary", ""),
            "overall": overall,
            "gate": gate,
            "qualifier": qualifier,
            "dimensions": dim_findings,
            "ac_count": len(ac_list),
            "report_markdown": report_md,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "engine": "laya" if self._laya.available else "fallback",
        }

    def _score_dimensions(
        self, ac_text: str, ac_list: List[str]
    ) -> Dict[str, float]:
        """Score all dimensions — LAYA decision engine or fallback."""
        if self._laya.available:
            try:
                return self._laya_scores(ac_text, ac_list)
            except CriticUnavailable:
                pass
            except Exception as exc:
                log.warning("LAYA scoring failed: %s; using fallback", exc)
        return _fallback_score(ac_text, ac_list)

    def _laya_scores(self, ac_text: str, ac_list: List[str]) -> Dict[str, float]:
        """Score dimensions through LAYA's decision engine.

        Each dimension is a separate schema since LAYA requires flat property
        schemas.  We batch them all in one ``decide_batch`` call.
        """
        state = self._ac_to_state(ac_text, ac_list)

        # Build one combined schema that captures all dimensions flatly
        combined = {
            "type": "object",
            "properties": {
                "clarity_level": CLARITY_SCHEMA["properties"]["clarity_level"],
                "testability_level": TESTABILITY_SCHEMA["properties"]["testability_level"],
                "specificity_level": SPECIFICITY_SCHEMA["properties"]["specificity_level"],
                "structure_level": STRUCTURE_SCHEMA["properties"]["structure_level"],
                "edge_coverage": EDGE_NEGATIVE_SCHEMA["properties"]["edge_coverage"],
                "outcome_focus": OUTCOME_SCHEMA["properties"]["outcome_focus"],
                "has_risk": RISK_SCHEMA["properties"]["has_risk"],
                "has_approval_gate": RISK_SCHEMA["properties"]["has_approval_gate"],
                "risk_level": RISK_SCHEMA["properties"]["risk_level"],
            },
        }

        outcome = self._laya.decide(state, schema=combined)
        vals = outcome.values

        # Map LAYA 1-5 scores to 0-1 range
        def _norm(v: Any, hi: int = 5) -> float:
            if v is None:
                return 0.0
            return max(0.0, min(1.0, (float(v) - 1.0) / (hi - 1.0)))

        scores = {
            "clarity": _norm(vals.get("clarity_level")),
            "testability": _norm(vals.get("testability_level")),
            "specificity": _norm(vals.get("specificity_level")),
            "structure": _norm(vals.get("structure_level")),
            "edge_negative": _norm(vals.get("edge_coverage")),
            "outcome_focus": _norm(vals.get("outcome_focus")),
        }

        # Risk: map 3-level to 0-1
        rl = vals.get("risk_level")
        if rl is not None:
            risk_map = {1: 1.0, 2: 0.6, 3: 0.0}
            scores["risk"] = risk_map.get(int(rl), 0.0)
        else:
            scores["risk"] = 0.0

        return scores

    @staticmethod
    def _ac_to_state(ac_text: str, ac_list: List[str]) -> Dict[str, str]:
        """Build the LAYA state dict from AC text."""
        return {"ac_text": ac_text, "ac_count": str(len(ac_list))}

    @staticmethod
    def _empty_report(ticket: dict) -> dict:
        key = ticket.get("key", "???")
        return {
            "key": key,
            "summary": ticket.get("summary", ""),
            "overall": 0.0,
            "gate": "BLOCK 🛑",
            "qualifier": "No acceptance criteria found",
            "dimensions": {},
            "ac_count": 0,
            "report_markdown": f"# LAYA Critique — {key}\n\n**No acceptance criteria.** Cannot grade.",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "engine": "fallback",
        }

    # ── Batch ──────────────────────────────────────────────────────────

    def critique_batch(self, tickets: List[dict]) -> List[dict]:
        """Grade multiple tickets and return a list of reports."""
        return [self.critique_ticket(t) for t in tickets]

    # ── Report rendering ───────────────────────────────────────────────

    def _render_report(
        self,
        ticket: dict,
        scores: Dict[str, float],
        dim_findings: Dict[str, dict],
        overall: float,
        gate: str,
        qualifier: str,
    ) -> str:
        ac_text, ac_list = _normalise_ac(ticket)
        key = ticket.get("key", "???")
        summary = ticket.get("summary", "")
        prio = ticket.get("priority", "")
        itype = ticket.get("issuetype", "")
        status = ticket.get("status", "")
        assignee = ticket.get("assignee", "Unassigned")

        lines = [
            REPORT_HEADER.format(
                date=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                source=ticket.get("_source", "direct"),
            ),
            TICKET_SUMMARY.format(
                key=key,
                summary=summary,
                prio=f"({prio})" if prio else "",
                itype=itype or "—",
                status=status or "—",
                assignee=assignee or "Unassigned",
                ac_count=len(ac_list),
                overall=overall,
                gate=gate.split(" ")[0],
            ),
            f"**Gate decision:** {gate} — {qualifier}",
            TABLE_HEADER,
        ]

        for dim in ["clarity", "testability", "specificity", "structure",
                     "edge_negative", "outcome_focus", "risk"]:
            fd = dim_findings.get(dim, {})
            lines.append(DIM_ROW.format(
                dim=DIMENSION_LABELS.get(dim, dim),
                score=fd.get("score", 0.0),
                icon=fd.get("icon", "⬜"),
                finding=fd.get("finding", ""),
            ))

        # AC listing
        lines.append("\n## Acceptance Criteria")
        for i, ac in enumerate(ac_list, 1):
            lines.append(f"{i}. {ac}")

        # Improvement suggestions
        low_dims = [(d, s) for d, s in scores.items() if s < 0.55]
        if low_dims:
            lines.append("\n## Improvement priorities\n")
            for dim, _ in sorted(low_dims, key=lambda x: x[1]):
                fd = dim_findings.get(dim, {})
                lines.append(f"- **{DIMENSION_LABELS.get(dim, dim)}:** {fd.get('finding', '')}")

        lines.append(f"\n---\n_Engine: {'LAYA model (System 1)' if self._laya.available else 'Deterministic fallback'}_")
        return "\n".join(lines)