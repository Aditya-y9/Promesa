"""LayaCritic — wraps the `laya` System 1 decision engine as a critique backend.

The engine provides calibrated structured decisions (enum choices, scores,
booleans) from a JSON schema definition.  Every call is *deterministic* for the
same state + schema, matching the philosophy of the rest of this repo.

Usage
-----
    critic = LayaCritic()
    outcome = critic.decide(
        state={"ac_text": "Given ... When ... Then ..."},
        schema={
            "type": "object",
            "properties": {
                "clarity": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 5,
                    "description": "Clarity of the acceptance criterion",
                },
            },
        },
    )
    print(outcome.values)   # {"clarity": 4}
"""
from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

log = logging.getLogger(__name__)


@dataclass
class DecisionOutcome:
    """The structured output from a LAYA critique decision.

    ``values`` holds the schema-shaped answer (e.g. ``{"clarity": 4}``).
    ``confidence`` is the normalised-entropy per field.
    ``answer_confidence`` is ``max(p)`` per field — the probability on the
    reported answer.
    ``routing`` records which checkpoint was used.
    ``usage`` holds token counts when available.
    """

    values: Dict[str, Any]
    confidence: Dict[str, float]
    answer_confidence: Dict[str, Optional[float]]
    routing: Optional[Dict[str, Any]] = None
    usage: Optional[Dict[str, Any]] = None


class CriticUnavailable(RuntimeError):
    """Raised when the LAYA model could not be loaded (e.g. no internet / disk)."""


class LayaCritic:
    """Graded critic powered by the `laya` decision engine.

    Two modes:

    **online** (default) — downloads the LAYA checkpoint on first use and runs
    decisions through it.  The English checkpoint is ~421M parameters (ModernBERT);
    download is one-time, cached by huggingface_hub.

    **offline** — when the model cannot be loaded, falls back to a
    deterministic scoring rubric that matches this repo's ``generate_ac_rank.py``
    and ``promesa_ac_readiness.py`` scorers, producing the same output shape.
    """

    def __init__(
        self,
        model: str = "english",
        device: Optional[str] = None,
        prefer_offline: bool = False,
        min_confidence: Optional[float] = None,
    ):
        self._model_name = model
        self._device = device
        self._prefer_offline = prefer_offline
        self._min_confidence = min_confidence
        self._runner: Any = None  # lazy: laya.Agent or laya.Router
        self._fallback_active = False

    # ------------------------------------------------------------------
    # Lazy model load
    # ------------------------------------------------------------------

    @property
    def runner(self) -> Any:
        """Access the LAYA runner, loading it on first access."""
        if self._runner is None and not self._prefer_offline:
            self._load()
        if self._runner is None:
            raise CriticUnavailable(
                "LAYA model is not loaded and offline mode is active. "
                "Call decide() with the deterministic fallback, or allow online access."
            )
        return self._runner

    def _load(self) -> None:
        """Download and initialise the LAYA decision model."""
        try:
            import laya  # noqa: F811
        except ImportError:
            log.warning("laya package not installed; falling back to deterministic critic")
            self._fallback_active = True
            return

        try:
            log.info("Loading LAYA model '%s' ...", self._model_name)
            # Use the Router so it auto-selects the right checkpoint per state.
            if self._model_name == "router":
                self._runner = laya.Router()
            else:
                from laya import Agent
                self._runner = Agent(
                    "convaiinnovations/laya",
                    subfolder=None if self._model_name == "english" else self._model_name,
                    device=self._device,
                )
            log.info("LAYA model loaded successfully.")
        except Exception as exc:
            log.warning("Failed to load LAYA model: %s; fallback active", exc)
            self._fallback_active = True

    # ------------------------------------------------------------------
    # Decide
    # ------------------------------------------------------------------

    def decide(
        self,
        state: Any,
        schema: Optional[Dict[str, Any]] = None,
        questions: Optional[Dict[str, Any]] = None,
    ) -> DecisionOutcome:
        """Run a structured decision against the LAYA engine.

        Pass exactly one of ``schema`` or ``questions``.
        """
        if self._fallback_active or self._prefer_offline:
            raise CriticUnavailable(
                "LAYA model not loaded. Use decide_fallback() for deterministic scoring."
            )

        try:
            import laya
        except ImportError:
            raise CriticUnavailable("laya package not installed")

        result = laya.decide(
            self.runner,
            state,
            schema=schema,
            questions=questions,
            return_details=True,
            min_confidence=self._min_confidence,
        )
        return DecisionOutcome(
            values=result.values,
            confidence=result.confidence,
            answer_confidence=result.answer_confidence,
            routing=result.routing,
            usage=result.usage,
        )

    def decide_batch(
        self,
        states: Sequence[Any],
        schema: Optional[Dict[str, Any]] = None,
        questions: Optional[Dict[str, Any]] = None,
    ) -> List[DecisionOutcome]:
        """Batch form of ``decide`` — one shared schema over many states."""
        if self._fallback_active or self._prefer_offline:
            raise CriticUnavailable(
                "LAYA model not loaded. Use decide_fallback() for deterministic scoring."
            )

        try:
            import laya
        except ImportError:
            raise CriticUnavailable("laya package not installed")

        results = laya.decide_batch(
            self.runner,
            list(states),
            schema=schema,
            questions=questions,
            return_details=True,
            min_confidence=self._min_confidence,
        )
        return [
            DecisionOutcome(
                values=r.values,
                confidence=r.confidence,
                answer_confidence=r.answer_confidence,
                routing=r.routing,
                usage=r.usage,
            )
            for r in results
        ]

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    @property
    def available(self) -> bool:
        """Whether the LAYA model is loaded and ready."""
        try:
            return self.runner is not None
        except CriticUnavailable:
            return False

    @property
    def using_fallback(self) -> bool:
        """True if the model is unavailable and deterministic scoring is in use."""
        return self._fallback_active