"""plan_critique_task.py — the plan-critique job LAYA is fine-tuned for.

Implements the ``plan-critique-prompt.md`` role as a LAYA decision task.  Each item
under judgement is a ``{REPO_CONTEXT, PLAN}`` pair: a short description of a repo's
operational profile and non-functional requirements, and one candidate implementation plan.

Two questions (mirroring the prompt's **Verdict** and **Required Directives**):

1. ``verdict`` — 2-way.  PASS = the plan is architecturally sound and context-aligned;
   REVISE = it uses naive patterns that violate the repo profile / NFRs.
2. ``criterion`` — N-way.  Which *single* architectural defect best describes what is wrong
   with the plan (``none`` when nothing is).

The criteria model the naive anti-patterns the prompt tells the critic to reject per archetype.
"""
from __future__ import annotations

from typing import Any, Dict

# ── Verdict ────────────────────────────────────────────────────────────────────
VERDICT_OPTIONS: Dict[str, str] = {
    "pass": "the plan is architecturally sound and strictly aligned with the repo context profile",
    "revise": "the plan uses naive patterns that violate the repo's operational profile or non-functional requirements",
}

# ── Architectural defects (mutually exclusive, flattened for LAYA's token budget) ──
CRITERIA: Dict[str, str] = {
    "none": "no architecture defect worth flagging",
    "naive_in_memory_collection": "keeps all data in an in-memory collection instead of streaming, batching, or paginating",
    "blocking_io": "makes blocking calls where the context demands non-blocking / async I/O",
    "heavy_allocation": "allocates heavily or buffers whole payloads where the context demands bounded memory",
    "unbounded_retries": "retries or loops without a cap, risking unbounded work in a low-latency path",
    "hardcoded_secret": "embeds a credential or secret in code instead of resolving it from a vault",
    "raw_query": "sends raw templated queries instead of parameterized inputs",
    "open_trust_boundary": "accepts untrusted input on an open trust boundary without validation",
    "missing_audit_logging": "fails to log an auditable record of a sensitive action",
    "unshielded_remote_call": "calls a remote service with no timeout, circuit breaker, or bulkhead",
    "missing_circuit_breaker": "no circuit breaker around a fragile downstream dependency",
    "missing_idempotency": "no idempotency key or dedup for an operation that may be retried",
    "missing_fallback": "no dead-letter, fallback, or graceful-degradation path on downstream failure",
    "unbounded_transaction": "does bulk work in one transaction with no bounded size or chunking",
    "missing_pagination": "loads an entire result set instead of paginating or limiting",
    "missing_connection_pool": "opens connections per call instead of pooling them",
    "no_chunking": "processes a large batch as one unit with no chunk size or memory cap",
    "ignores_do_not": "expands scope or performs a change the context explicitly forbids",
    "overly_risky": "touches a risky surface (delete, migration, prod data) without an approval gate",
}

# Defect → the archetypes that make it a "violation" (used by the generator).
# Kept flat and mutually exclusive; each belongs to at least one archetype.
DEFECT_ARCHETYPES: Dict[str, list[str]] = {
    "naive_in_memory_collection": ["performance", "batch", "resilience"],
    "blocking_io":              ["performance"],
    "heavy_allocation":          ["performance", "batch"],
    "unbounded_retries":       ["performance", "resilience"],
    "hardcoded_secret":        ["security"],
    "raw_query":                ["security"],
    "open_trust_boundary":     ["security"],
    "missing_audit_logging":   ["security"],
    "unshielded_remote_call":  ["resilience"],
    "missing_circuit_breaker": ["resilience"],
    "missing_idempotency":     ["resilience", "batch"],
    "missing_fallback":        ["resilience"],
    "unbounded_transaction":   ["batch"],
    "missing_pagination":      ["performance", "batch"],
    "missing_connection_pool": ["performance", "resilience"],
    "no_chunking":             ["batch"],
    "ignores_do_not":          ["security", "batch", "resilience", "performance"],
    "overly_risky":           ["security"],
}

# ── Repo context profiles (the prompt's archetypes) ─────────────────────────
ARCHETYPES: Dict[str, str] = {
    "performance": "Performance-Intensive / Low-Latency",
    "security":    "Security / Compliance",
    "resilience":  "High-Resilience / Distributed",
    "batch":       "Batch / High-Throughput",
}

# A human-readable "DO / DO-NOT" line per archetype, used in REPO_CONTEXT text.
ARCHETYPE_CONTEXT: Dict[str, str] = {
    "performance": ("The service is performance-intensive and latency-sensitive. It must stream and paginate; "
                   "no unbounded in-memory buffering, no blocking calls, no connection-per-request."),
    "security":    ("The service is security- and compliance-sensitive. It must use parameterized queries, resolve secrets "
                   "from a vault, apply least-privilege access, and write audit logs for sensitive actions; no hardcoded secrets, "
                   "no open trust boundaries, no raw queries."),
    "resilience":  ("The service is distributed and high-resilience. Every remote call needs a timeout, a circuit breaker, "
                    "bulkhead isolation, idempotency, and a dead-letter / fallback path; no unshielded remote calls."),
    "batch":       ("The job is batch / high-throughput. It must chunk work into bounded transactions with strict heap and memory caps; "
                   "no single unbounded transaction, no loading whole datasets into memory."),
}


def laya_questions() -> Dict[str, Dict[str, Any]]:
    """The two questions in LAYA's schema."""
    return {
        "verdict": {
            "type": "choice",
            "instructions": "Judge the PLAN against the REPO_CONTEXT. Is it architecturally acceptable as it stands?",
            "criteria": VERDICT_OPTIONS,
        },
        "criterion": {
            "type": "choice",
            "instructions": "Which single architectural defect best describes what is wrong with the PLAN (none if nothing is)?",
            "criteria": CRITERIA,
        },
    }


def state_for(repo_context: str, plan: str) -> Dict[str, str]:
    """The item under judgement: the repo profile and the candidate plan."""
    return {"REPO_CONTEXT": repo_context, "PLAN": plan}


SHORT_CRITERIA: Dict[str, str] = {k: k.replace("_", " ") for k in CRITERIA}
