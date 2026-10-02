"""LAYA critique engine package.

Uses the `laya` System 1 decision engine (fast, non-autoregressive, calibrated
structured decisions) as the critic that grades acceptance criteria and Promesa
canvases, then renders human-readable critique reports.

Matching the rest of this repo (generate_ac_rank.py, promesa_ac_readiness.py,
promesa_canvas.py) every report is deterministic for the same input. The LAYA
schema-driven decisions provide the *grade*; the bilingual critique renderer explains
the *why*.
"""
from .engine import LayaCritic, DecisionOutcome, CriticUnavailable
from .ac_critique import AcceptanceCriteriaCritic
from .canvas_critique import CanvasCritic

__all__ = [
    "LayaCritic",
    "DecisionOutcome",
    "CriticUnavailable",
    "AcceptanceCriteriaCritic",
    "CanvasCritic",
]
