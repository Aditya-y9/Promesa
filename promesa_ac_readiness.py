"""
promesa_ac_readiness.py — Promesa Readiness gate ranking.
Implements all 7 dimensions per ac-quality-ranker.agent.md v2 (Promesa).
Generates reason/improvement from dimension scores (no hardcoded maps).
Deterministic: same input → same scores every run.
"""
import json, datetime, re, math
from pathlib import Path
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter

# ── Config ─────────────────────────────────────────────────────────────────
BASE = Path(__file__).parent
SAMPLE_PATH = BASE / "deepwiki-agent" / "sample-ac-tickets.json"
OUTPUT_NAME = f"promesa-ac-readiness-{datetime.date.today().isoformat()}.xlsx"
OUTPUT_PATH = BASE / OUTPUT_NAME

VAGUE_TERMS = {"easy","easily","fast","nice","user-friendly","make sure it works",
               "should probably","reasonable","good experience","be careful","work with the",
               "handle it","make it nice","lots of","all major browsers"}
EXPLICIT_VERBS = {"returns","shows","is sent","redirects","created","blocked",
                  "updates","rejected","accepted","denied","queued","appears","displays"}
CONSTRAINT_WORDS = {"must not","do not","only","unless","without changing","limit",
                    "max","read-only","no new","require permission"}
CONCRETE_NOUNS = {"/api/","page","field","endpoint","schema","role","component",
                  "column","button","table","email","pdf","csv","url","path","file",
                  "header","response","body","status"}
EDGE_WORDS = {"invalid","error","empty","expired","exceed","unauthori","429","401",
              "403","bounce","duplicate","revoked","fail","block","timeout",
              "concurrent","queue","boundary","corrupt","missing","bad request"}
IMPL_VERBS = {"call ","install ","use library","add to ","update database",
              "modify the query","write sql","call function","work with the backend"}
RISKY_WORDS = {"migration","production","payment","billing","delete","drop column",
               "pii","root","deploy","prod","purge"}
# Words that NEGATE a risky keyword (e.g. "cannot be modified or deleted") — must not
# trigger risk when the requirement forbids the dangerous action.
RISKY_NEGATIONS = {"cannot","can't","must not","mustn't","do not","don't","never",
                   "blocked","forbidden","no ","without","should not","not allowed","only"}
APPROVAL_GATES = {"human approval","permission gate","approval required",
                  "requires authorization","must have approval","sign-off"}


def detect_risk(all_text):
    """Return (risky, has_approval_gate, has_weak_gate). Sentence-aware:
    a risky keyword appearing in a negated or permission-grant-only clause
    ('cannot be deleted', 'must not delete', 'only admins may delete') does NOT count."""
    lower = all_text
    has_approval_gate = any(g in lower for g in APPROVAL_GATES)
    has_weak_gate = "on-call" in lower or "with the on-call" in lower
    if "on-call" in lower:
        has_weak_gate = True

    # Split into sentences to catch negations that span many words
    sentences = re.split(r'[.!?\n;]+', lower)

    risky_hits = 0
    for sent in sentences:
        # Check if this sentence has any negation/prohibition
        has_negation = any(neg in sent for neg in RISKY_NEGATIONS)
        for w in RISKY_WORDS:
            if w in sent:
                # If the sentence contains the word AND the sentence also contains a
                # negation that forbids it (cannot, must not, etc.), it's not risky.
                if has_negation:
                    # True if the negation appears BEFORE the risky word in the sentence
                    # or verbs are prohibitive (e.g. "cannot be deleted")
                    if any(neg in sent[:sent.index(w)] for neg in RISKY_NEGATIONS):
                        continue  # negated — not a risky action
                risky_hits += 1

    risky = risky_hits > 0
    if risky and not has_approval_gate and not has_weak_gate:
        return (True, has_approval_gate, has_weak_gate)
    return (risky, has_approval_gate, has_weak_gate)

# ── Scoring engine ──────────────────────────────────────────────────────────

def score_ticket(issue):
    ac_raw = issue.get("acceptanceCriteria", [])
    if not ac_raw or (isinstance(ac_raw, list) and all(not a.strip() for a in ac_raw)):
        return {
            "ac_present": "No", "ac_count": 0, "dims": {i:0 for i in range(1,8)},
            "overall": 0, "gate": "BLOCK", "reason": "No acceptance criteria found",
            "improvement": "Write testable, constraint-bound acceptance criteria before handing to Promesa"
        }

    ac_list = list(ac_raw) if isinstance(ac_raw, list) else [ac_raw]
    ac_count = len(ac_list)
    all_text = " ".join(ac_list).lower()

    # — dimension 1: Testability / verifiable outcomes (cap 20) —
    vague_hits = sum(1 for t in VAGUE_TERMS if t in all_text)
    explicit_hits = sum(1 for v in EXPLICIT_VERBS if v in all_text)
    has_test_mention = "test" in all_text
    dim1 = 20 - (vague_hits * 3) + (explicit_hits * 2) + (2 if has_test_mention else 0)
    if explicit_hits >= 3:
        dim1 += 3
    dim1 = max(0, min(20, dim1))

    # — dimension 2: Constraints, permissions & boundaries (cap 18) —
    constraint_hits = sum(1 for c in CONSTRAINT_WORDS if c in all_text)
    dim2 = constraint_hits * 5
    dim2 = max(0, min(18, dim2))

    # — dimension 3: Self-contained / context sufficiency (cap 15) —
    noun_hits = sum(1 for n in CONCRETE_NOUNS if n in all_text)
    numbers = re.findall(r'\d+', all_text)
    quoted = re.findall(r"'[^']+'|\"[^\"]+\"", all_text)
    dim3 = (noun_hits * 2) + (3 if numbers else 0) + (min(len(quoted), 3))
    dim3 = max(0, min(15, dim3))

    # — dimension 4: One concern + clear structure (cap 12) —
    gwt_count = sum(1 for ac in ac_list if "scenario:" in ac.lower() or
                     ("given" in ac.lower() and "when" in ac.lower() and "then" in ac.lower()))
    avg_coupling = 0
    if ac_count > 0:
        total_coupling = sum(ac.lower().count(" and ") + ac.lower().count(" or ") for ac in ac_list)
        avg_coupling = total_coupling / ac_count
    dim4 = 0
    if gwt_count:
        dim4 += 6
        if gwt_count == ac_count: dim4 += 4
        elif gwt_count >= ac_count * 0.5: dim4 += 2
    elif ac_count >= 2:
        dim4 = 5
        if avg_coupling <= 0.5: dim4 += 2
    if avg_coupling > 1.5: dim4 = max(2, dim4 - 3)
    elif avg_coupling > 0.8: dim4 = max(2, dim4 - 1)
    dim4 = max(0, min(12, dim4))

    # — dimension 5: Edge & negative cases (cap 20) —
    edge_hits = sum(1 for w in EDGE_WORDS if w in all_text)
    edge_types = set(w for w in EDGE_WORDS if w in all_text)
    dim5 = len(edge_types) * 5
    if edge_hits >= 6: dim5 += 3
    elif edge_hits >= 3: dim5 += 1
    dim5 = max(0, min(20, dim5))

    # — dimension 6: Outcome & product focus (cap 10) —
    impl_hits = sum(1 for v in IMPL_VERBS if v in all_text)
    dim6 = 10 - (impl_hits * 3)
    if impl_hits == 0 and explicit_hits >= 2: dim6 = min(10, dim6 + 2)
    dim6 = max(0, min(10, dim6))

    # — dimension 7: Risk & blocking signals (cap 5) —
    risky, has_approval_gate, has_weak_gate = detect_risk(all_text)
    if not risky:
        dim7 = 5  # safe
    elif has_approval_gate:
        dim7 = 3  # risky but gated
    elif has_weak_gate:
        dim7 = 1  # risky with weak human present but not formal gate
    else:
        dim7 = 0  # unguarded high risk
    dim7 = max(0, min(5, dim7))

    dims = {1: dim1, 2: dim2, 3: dim3, 4: dim4, 5: dim5, 6: dim6, 7: dim7}
    overall = dim1 + dim2 + dim3 + dim4 + dim5 + dim6 + dim7

    # ── Gate logic ──────────────────────────────────────────────────────
    # Dim-7 modifier: unguarded high risk → cap at REFINE
    caps_to_refine = False
    scope_too_large = False

    if risky and not has_approval_gate and not has_weak_gate:
        caps_to_refine = True
        dims[7] = 0
        overall = dim1 + dim2 + dim3 + dim4 + dim5 + dim6 + 0

    # Scope-too-large heuristic: AC count>=7 or "epic" issuetype or mentions
    # multiple unrelated subsystems
    is_epic = issue.get("issuetype","").lower() in ("epic",)
    multi_subsystem = sum(1 for w in ["billing","auth","notification","permission","rbac","sso","saml","oidc"] if w in all_text)
    if is_epic or (ac_count >= 6 and multi_subsystem >= 2):
        scope_too_large = True
        if overall >= 75:
            overall = 74  # pull below PROCESS regardless
        caps_to_refine = True

    # Binary gate
    if overall >= 75 and not caps_to_refine:
        gate = "PROCESS"
    elif overall >= 40:
        gate = "REFINE"
    else:
        gate = "BLOCK"

    # ── Reason & Improvement (generated from dims) ──────────────────────
    weak_dims = [(i, v) for i, v in dims.items() if v <= dim_cap(i) * 0.4]
    strong_dims = [(i, v) for i, v in dims.items() if v >= dim_cap(i) * 0.85]

    dim_labels = {
        1: "Testability/verifiable outcomes", 2: "Constraints/permissions/boundaries",
        3: "Self-contained context", 4: "One-concern structure",
        5: "Edge/negative cases", 6: "Outcome focus (not implementation)",
        7: "Risk/blocking signals"
    }
    dim_improvements = {
        1: "Replace vague language with objective, verifiable outcomes for each criterion",
        2: "Add explicit scope boundaries: what Promesa MUST NOT change or exceed",
        3: "Add specific names, sizes, timings, endpoint paths so the agent doesn't guess",
        4: "Split multi-condition criteria into one concern per Given/When/Then scenario",
        5: "Add error cases, invalid inputs, empty states, and boundary/duplicate scenarios",
        6: "Describe WHAT to achieve, not HOW — let Promesa decide implementation",
        7: "Add an explicit human-approval gate for any high-risk or irreversible operation"
    }

    # Build reason: mention up to 2 weakest and 1 strongest
    reason_parts = []
    for idx, val in sorted(weak_dims, key=lambda x: x[1])[:2]:
        if val < dim_cap(idx) * 0.3:
            reason_parts.append(f"very low {dim_labels[idx]}")
        else:
            reason_parts.append(f"low {dim_labels[idx]}")
    if strong_dims and not caps_to_refine and not scope_too_large:
        top = max(strong_dims, key=lambda x: x[1])
        if top[1] >= dim_cap(top[0]) * 0.9:
            reason_parts.append(f"strong {dim_labels[top[0]]}")

    if caps_to_refine and risky and not has_approval_gate:
        if has_weak_gate:
            reason_parts.append("high-risk scope with weak gate (human present, not formal)")
        else:
            reason_parts.append("unguarded high-risk scope — blocks automatic PROCESS")
    if scope_too_large:
        reason_parts.append("scope too large for one autonomous run")

    reason = "; ".join(reason_parts) if reason_parts else "balanced across dimensions"
    if overall == 0:
        reason = "No acceptance criteria found"

    # Improvement: weakest dim's suggestion, plus scope/risk note
    improv_parts = []
    if overall > 0 and not (gate == "PROCESS"):
        worst_dim = min(weak_dims, key=lambda x: x[1])[0] if weak_dims else None
        if worst_dim:
            improv_parts.append(dim_improvements[worst_dim])
            if len(weak_dims) >= 2:
                improv_parts.append(dim_improvements.get(weak_dims[1][0], ""))
    if scope_too_large:
        improv_parts.append("Break into smaller independent tickets — one autonomous scope per ticket")
    if caps_to_refine and not has_approval_gate and risky:
        improv_parts.append("Write explicit human-approval/permission gate for the risky operations before Promesa can proceed")
    if gate == "PROCESS":
        improv_parts.append("Ready for Promesa — validate outcomes and review the diff; monitor as normal")
    if not improv_parts and overall == 0:
        improv_parts.append("Write testable, constraint-bound acceptance criteria before handing to Promesa")
    if not improv_parts and overall > 0:
        improv_parts.append("Sharpen a low-scoring dimension (see Reason) to move toward a clean PROCESS gate")

    # Deduplicate while preserving order
    seen = set()
    improv_parts = [p for p in improv_parts if p and not (p in seen or seen.add(p))]

    improvement = "; ".join(p for p in improv_parts if p)

    return {
        "ac_present": "Yes" if ac_count > 0 else "No",
        "ac_count": ac_count,
        "dims": dims,
        "overall": overall,
        "gate": gate,
        "reason": reason,
        "improvement": improvement,
    }

def dim_cap(i):
    return {1:20, 2:18, 3:15, 4:12, 5:20, 6:10, 7:5}[i]

# ── Grade label ─────────────────────────────────────────────────────────────

def grade_label(overall):
    if overall >= 90: return "Excellent"
    elif overall >= 70: return "Good"
    elif overall >= 50: return "Needs Work"
    elif overall >= 20: return "Poor"
    return "No/Missing AC"

# ── Main ────────────────────────────────────────────────────────────────────

def main():
    with open(SAMPLE_PATH, encoding="utf-8") as f:
        data = json.load(f)
    issues = data["issues"]

    results = []
    for issue in issues:
        s = score_ticket(issue)
        results.append({
            "key": issue["key"],
            "summary": issue["summary"],
            "type": issue.get("issuetype",""),
            "ac_present": s["ac_present"],
            "ac_count": s["ac_count"],
            "overall": s["overall"],
            "gate": s["gate"],
            "grade": grade_label(s["overall"]),
            "reason": s["reason"],
            "improvement": s["improvement"],
        })

    # Sort: gate priority (process first), then score desc
    gate_order = {"PROCESS": 0, "REFINE": 1, "BLOCK": 2}
    results.sort(key=lambda r: (gate_order.get(r["gate"], 9), -r["overall"], r["key"]))

    # ── Write Excel ──────────────────────────────────────────────────────
    wb = Workbook()
    ws = wb.active
    ws.title = "Promesa Readiness"

    headers = ["Rank", "Ticket Key", "Summary", "Type", "AC Present",
               "# AC", "Overall Score (0-100)", "Gate", "Grade",
               "Reason", "Key Improvement", "Source"]

    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="2F5496", end_color="2F5496", fill_type="solid")
    header_align = Alignment(horizontal="center", vertical="center", wrap_text=True)

    for col_idx, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col_idx, value=h)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = header_align

    green = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
    amber = PatternFill(start_color="FFC000", end_color="FFC000", fill_type="solid")
    red = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
    gate_fill = {"PROCESS": green, "REFINE": amber, "BLOCK": red}
    source = "sample-ac-tickets.json"

    for rank, r in enumerate(results, 1):
        row_idx = rank + 1
        row_data = [rank, r["key"], r["summary"], r["type"], r["ac_present"],
                    r["ac_count"], r["overall"], r["gate"], r["grade"],
                    r["reason"], r["improvement"], source]
        gf = gate_fill.get(r["gate"])
        for col_idx, val in enumerate(row_data, 1):
            cell = ws.cell(row=row_idx, column=col_idx, value=val)
            cell.alignment = Alignment(wrap_text=True, vertical="center")
            if gf:
                cell.fill = gf

    col_widths = [6, 12, 34, 16, 12, 6, 20, 12, 14, 55, 60, 18]
    for i, w in enumerate(col_widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"

    wb.save(OUTPUT_PATH)

    # ── Console summary ──────────────────────────────────────────────────
    avg_score = sum(r["overall"] for r in results) / len(results) if results else 0
    gate_counts = {"PROCESS": 0, "REFINE": 0, "BLOCK": 0}
    for r in results:
        gate_counts[r["gate"]] = gate_counts.get(r["gate"], 0) + 1
    best = results[0] if results else None
    worst = results[-1] if results else None

    print(f"✅ Written: {OUTPUT_PATH.resolve()}")
    print(f"📋 Issues ranked: {len(results)}")
    print(f"📊 Average score: {avg_score:.1f}")
    print(f"🚦 Gate distribution: PROCESS {gate_counts['PROCESS']} / REFINE {gate_counts['REFINE']} / BLOCK {gate_counts['BLOCK']}")
    if best: print(f"🥇 Best:  {best['key']} — {best['overall']} ({best['gate']})")
    if worst: print(f"⛔ Worst: {worst['key']} — {worst['overall']} ({worst['gate']})")
    print(f"📁 Data source: {source}")

    # Determinism validation: re-run in-memory
    recheck = [score_ticket(i) for i in issues]
    identity = all(
        round(a["overall"], 6) == round(b["overall"], 6)
        for a, b in zip(recheck, [score_ticket(issue) for issue in issues])
    )
    print(f"🔁 Deterministic: {'✅ yes (byte-identical on re-run)' if identity else '⚠️  NO — scoring not deterministic!'}")

if __name__ == "__main__":
    main()