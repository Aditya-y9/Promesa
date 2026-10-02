"""CanvasCritic — critiques Promesa Decision Canvases using LAYA.

A Promesa canvas (from promesa_canvas.py) packages a ticket with AC summary,
fences, edge cases, and 3 strategy angles.  This critic grades the canvas
itself — is it ready for Promesa's plan generator?  Are the fences clear?
Are the strategy angles actually divergent?

The LAYA decision engine scores each property then a bilingual report explains
the findings.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .engine import CriticUnavailable, DecisionOutcome, LayaCritic

log = logging.getLogger(__name__)

# ── Canvas parsing ─────────────────────────────────────────────────────────


def _parse_canvas(md_text: str) -> dict:
    """Parse a Promesa Decision Canvas markdown into sections."""
    result = {
        "key": "",
        "summary": "",
        "ac_list": [],
        "do_list": [],
        "do_not_list": [],
        "edges": "",
        "strategy_angles": [],
        "has_permissions_section": False,
        "permissions": [],
    }

    # Key
    m = re.search(r"# Promesa Decision Canvas — (\w+-\d+)", md_text)
    if m:
        result["key"] = m.group(1)

    # Summary
    m = re.search(r"\*\*Goal:\*\* (.+)", md_text)
    if m:
        result["summary"] = m.group(1).strip()

    # AC list
    acs = re.findall(r"^- \*\*\[AC-\d+\]\*\* (.+)$", md_text, re.MULTILINE)
    result["ac_list"] = acs

    # DO / DO-NOT
    do_list = re.findall(r"^- \*\*DO:\*\* (.+)$", md_text, re.MULTILINE)
    do_not_list = re.findall(r"^- \*\*DO-NOT:\*\* (.+)$", md_text, re.MULTILINE)
    result["do_list"] = do_list
    result["do_not_list"] = do_not_list

    # Edge text
    m = re.search(r"(?i)##\s*Edge[^\n]*\n- Cover:\s*(.+)$", md_text, re.MULTILINE)
    if m:
        result["edges"] = m.group(1).strip()

    # Strategy angles
    angles = re.findall(r"^- \*\*([A-C])\s*/[^*]+\*\* (.+)$", md_text, re.MULTILINE)
    result["strategy_angles"] = [f"{letter}: {desc.strip()}" for letter, desc in angles]

    # Permissions
    perms = re.findall(r"^- (.+?)(?=\n|$)", md_text[md_text.find("## Permissions"):], re.MULTILINE)
    if perms:
        result["has_permissions_section"] = True
        result["permissions"] = [p.strip("- ") for p in perms if p.strip()]

    return result


# ── Scoring ────────────────────────────────────────────────────────────────


def _score_fences_clarity(parsed: dict) -> float:
    """Score clarity of DO / DO-NOT fences."""
    do_not = parsed.get("do_not_list", [])
    if not do_not:
        return 0.3
    # Good fences: specific and actionable
    vague = {"none", "tbd", "n/a", "keep contained", "none flagged"}
    good = sum(1 for d in do_not if not any(v in d.lower() for v in vague))
    return min(1.0, 0.3 + good * 0.25)


def _score_edge_coverage(parsed: dict) -> float:
    """Score edge case coverage in the canvas."""
    edges = parsed.get("edges", "")
    if not edges:
        return 0.1
    if "none explicitly" in edges.lower():
        return 0.3
    # Count distinct edge types
    edge_keywords = {"error", "empty", "expired", "timeout", "boundary",
                     "invalid", "unauthori", "rate limit", "429", "401",
                     "403", "concurrent", "duplicate", "null", "bounce"}
    hits = sum(1 for kw in edge_keywords if kw in edges.lower())
    return min(1.0, 0.2 + hits * 0.12)


def _score_angle_divergence(parsed: dict) -> float:
    """Score how divergent the 3 strategy angles are."""
    angles = parsed.get("strategy_angles", [])
    if len(angles) < 3:
        return 0.2
    # Check that angles actually describe different approaches
    a_text = " ".join(angles).lower()
    # Distinct keywords per angle
    a_keywords = {"minimal", "safe", "smallest", "fewest", "change"}
    b_keywords = {"verify", "test", "guardrail", "regression", "contract"}
    c_keywords = {"tdd", "clean", "refactor", "maintainability", "test-driven"}
    has_a = any(k in a_text for k in a_keywords)
    has_b = any(k in b_keywords for k in b_keywords)
    has_c = any(k in c_keywords for k in c_keywords)
    divergence = sum([has_a, has_b, has_c])
    return min(1.0, 0.2 + divergence * 0.27)


def _score_decision_readiness(parsed: dict) -> float:
    """Score whether the canvas enables a human to make a decision."""
    has_perms = parsed.get("has_permissions_section", False)
    has_summary = bool(parsed.get("summary"))
    has_acs = len(parsed.get("ac_list", [])) > 0
    has_matrix = False  # would need to check for comparison matrix

    score = 0.0
    if has_summary:
        score += 0.2
    if has_acs:
        score += 0.3
    if has_perms:
        score += 0.15
    return min(1.0, score + 0.2)  # base


CANVAS_DIMS = {
    "fences_clarity": {
        "label": "Fences clarity",
        "weight": 20,
        "scorer": _score_fences_clarity,
    },
    "edge_coverage": {
        "label": "Edge case coverage",
        "weight": 20,
        "scorer": _score_edge_coverage,
    },
    "angle_divergence": {
        "label": "Strategy angle divergence",
        "weight": 25,
        "scorer": _score_angle_divergence,
    },
    "decision_readiness": {
        "label": "Decision readiness",
        "weight": 15,
        "scorer": _score_decision_readiness,
    },
}


def _findings(dim: str, score: float, is_lowest: bool) -> str:
    low = score < 0.55
    mid = 0.55 <= score < 0.85

    canvas_findings = {
        "fences_clarity": {
            "low": "Fences are missing or vague. Add specific DO-NOT constraints so Promesa does not over-scope. / Pagar tidak jelas. Tambahkan batasan DO-NOT yang spesifik.",
            "mid": "Fences exist but could be more specific. / Pagar sudah ada tapi bisa lebih spesifik.",
            "high": "Clear, actionable fences that bound scope effectively. / Pagar jelas dan efektif membatasi lingkup.",
        },
        "edge_coverage": {
            "low": "No edge cases listed. Include error paths, empty states, boundaries, and auth failures. / Tidak ada kasus tepi. Sertakan jalur error, keadaan kosong, batas, kegagalan auth.",
            "mid": "Some edges covered but gaps remain (see report). / Beberapa kasus tepi sudah ada tapi masih ada celah.",
            "high": "Edge cases are well-covered — the agent can plan for them from the start. / Kasus tepi tercakup dengan baik.",
        },
        "angle_divergence": {
            "low": "Strategy angles are not sufficiently divergent. Each should propose a meaningfully different approach. / Sudut strategi tidak cukup berbeda. Setiap sudut harus menawarkan pendekatan yang berbeda.",
            "mid": "Angles show some divergence but could push further apart. / Sudut sudah mulai berbeda tapi bisa lebih divergen.",
            "high": "3 clearly divergent strategies covering minimal-safe, verify-first, and clean/TDD paths. / 3 strategi yang jelas berbeda.",
        },
        "decision_readiness": {
            "low": "Canvas is missing critical sections (goal, ACs, permissions). / Canvas kehilangan bagian penting (tujuan, AC, izin).",
            "mid": "Canvas has the key sections but permissions/decision matrix could be clearer. / Bagian utama sudah ada tapi matriks keputusan bisa lebih jelas.",
            "high": "Canvas is decision-ready: goal, ACs, fences, edges, angles, and permissions are all present. / Canvas siap untuk keputusan: tujuan, AC, pagar, kasus tepi, sudut, izin semua ada.",
        },
    }

    bucket = "low" if low else ("mid" if mid else "high")
    base = canvas_findings.get(dim, {}).get(bucket, "")
    if is_lowest and score < 0.6:
        base += " ⚠️ PRIORITY — lowest dimension."
    return base


# ── Report generation ──────────────────────────────────────────────────────

CANVAS_REPORT_HEADER = """# LAYA Critique — Promesa Canvas Quality Report

> Generated by the LAYA System 1 decision engine
> Date: {date}
> Canvas key: {key}

"""


def _gate_label(overall: float) -> str:
    if overall >= 75:
        return "PROCESS ✅"
    if overall >= 40:
        return "REVISE ⚡"
    return "REJECT 🛑"


# ── Critic class ───────────────────────────────────────────────────────────


class CanvasCritic:
    """Grades Promesa Decision Canvases for quality and decision-readiness.

    Usage
    -----
        critic = CanvasCritic()
        with open("promesa-canvas-PROJ-104.md") as f:
            report = critic.critique_canvas_md(f.read())
        print(report["report_markdown"])
    """

    def __init__(self, laya_critic: Optional[LayaCritic] = None):
        self._laya = laya_critic or LayaCritic(prefer_offline=False)

    def critique_canvas_md(self, md_text: str, source: str = "") -> dict:
        """Parse and critique a Promesa canvas from its markdown text."""
        parsed = _parse_canvas(md_text)
        key = parsed["key"] or source or "unknown"

        # Score dimensions
        dim_scores = {}
        for dim_name, dim_def in CANVAS_DIMS.items():
            dim_scores[dim_name] = dim_def["scorer"](parsed)

        # Overall
        overall = sum(
            dim_scores[d] * CANVAS_DIMS[d]["weight"]
            for d in dim_scores
        )
        overall = round(overall, 1)

        gate = _gate_label(overall)

        # Lowest dimension
        sorted_dims = sorted(dim_scores.items(), key=lambda x: x[1])
        lowest_dim = sorted_dims[0][0] if sorted_dims else "fences_clarity"

        dim_findings = {}
        for dim, score in dim_scores.items():
            is_lowest = dim == lowest_dim
            dim_findings[dim] = {
                "score": round(score, 3),
                "finding": _findings(dim, score, is_lowest),
                "icon": "🟢" if score >= 0.85 else ("🟡" if score >= 0.55 else "🔴"),
            }

        # Render
        lines = [
            CANVAS_REPORT_HEADER.format(
                date=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                key=key,
            ),
            f"**Gate:** {gate}  (overall score: {overall:.0f} / 100)",
            f"\n### Summary\n",
            f"**Summary:** {parsed['summary'] or '—'}",
            f"**AC count:** {len(parsed['ac_list'])}",
            f"**Strategy angles:** {len(parsed['strategy_angles'])}",
            f"**DO-NOT fences:** {len(parsed['do_not_list'])}",
            f"**Edges listed:** {parsed['edges'] or '—'}",
            "",
            "| Dimension              | Score         | Finding                                     |",
            "|------------------------|---------------|---------------------------------------------|",
        ]

        for dim in ["fences_clarity", "edge_coverage", "angle_divergence", "decision_readiness"]:
            fd = dim_findings.get(dim, {})
            lines.append(
                f"| {CANVAS_DIMS[dim]['label']:<22} | {fd.get('score', 0.0):>6.2f}  {fd.get('icon', '⬜')} | {fd.get('finding', '')} |"
            )

        # Low-scoring dimensions
        low_dims = [(d, s) for d, s in dim_scores.items() if s < 0.55]
        if low_dims:
            lines.append("\n## Improvement priorities\n")
            for dim, _ in sorted(low_dims, key=lambda x: x[1]):
                fd = dim_findings.get(dim, {})
                lines.append(f"- **{CANVAS_DIMS[dim]['label']}:** {fd.get('finding', '')}")

        lines.append("\n---\n_Engine: deterministic rubric_")
        report_md = "\n".join(lines)

        return {
            "key": key,
            "summary": parsed["summary"],
            "overall": overall,
            "gate": gate,
            "dimensions": dim_findings,
            "ac_count": len(parsed["ac_list"]),
            "report_markdown": report_md,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    def critique_canvas_file(self, path: Path) -> dict:
        """Read a canvas file and critique it."""
        md_text = path.read_text(encoding="utf-8")
        return self.critique_canvas_md(md_text, source=path.name)

    def critique_batch(self, paths: List[Path]) -> List[dict]:
        """Critique multiple canvas files."""
        return [self.critique_canvas_file(p) for p in paths]