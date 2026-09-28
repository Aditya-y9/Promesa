#!/usr/bin/env python3
"""
AC Quality Ranker — generate_ac_rank.py

Loads sample-ac-tickets.json, scores each issue's acceptance criteria
against 7 rubric dimensions, writes a ranked .xlsx workbook.

Deterministic: same input → same scores every run.
"""

import json, re, math, os, sys
from datetime import date, datetime
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter

# Ensure UTF-8 console output works even when piped (Windows cp1252 default)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# 1.  Load tickets
# ---------------------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLE_PATH = os.path.join(HERE, "deepwiki-agent", "sample-ac-tickets.json")

with open(SAMPLE_PATH, "r", encoding="utf-8") as f:
    data = json.load(f)

issues = data["issues"]
SOURCE = "sample-ac-tickets.json"

# ---------------------------------------------------------------------------
# 2.  Helper: normalise AC to a single text block + list of strings
# ---------------------------------------------------------------------------
def extract_ac(issue):
    """Return (ac_text, ac_list) – empty if no AC found."""
    raw = issue.get("acceptanceCriteria")
    if raw is None:
        return ("", [])
    if isinstance(raw, str):
        raw = [raw]
    ac_list = [str(item).strip() for item in raw if str(item).strip()]
    ac_text = "\n".join(ac_list)
    return (ac_text, ac_list)

# ---------------------------------------------------------------------------
# 3.  Rubric dimension scorers  (all return float 0.0 – 1.0 normalised)
# ---------------------------------------------------------------------------

# --- Vague / banned terms (for Clarity) ---
VAGUE_TERMS = [
    "easy", "easily", "fast", "quick", "nice", "user-friendly", "user friendly",
    "make sure it works", "should probably", "reasonably sized", "good experience",
    "be careful", "make sure", "lots of", "all major browsers", "look good",
    "just work", "work properly",
]

def score_clarity(ac_text, ac_list):
    """Clarity / unambiguity — 20 % weight."""
    if not ac_text:
        return 0.0
    lower = ac_text.lower()
    found = 0
    for term in VAGUE_TERMS:
        # count each occurrence
        found += lower.count(term)
    # Max penalty: if many vague terms, score drops to 0
    # Perfect clarity = no vague terms → 1.0
    penalty = min(found / max(len(ac_list), 1), 1.0)
    return max(0.0, 1.0 - penalty * 1.5)


# --- Testability keywords ---
TESTABLE_PATTERNS = [
    r"\bGiven\b", r"\bWhen\b", r"\bThen\b", r"\bScenario:",
    r"returns? \d{3}", r"HTTP \d{3}", r"status code",
    r"error message", r"success message", r"is (shown|displayed|returned|sent|created|updated|rejected)",
    r"pass|fail", r"verify", r"assert",
]
SUBJECTIVE_PATTERNS = [
    r"should (be|have|work|look|feel|make)", r"needs? to",
    r"hopefully", r"preferably", r"nice to have",
    r"be careful", r"make sure",
]

def score_testability(ac_text, ac_list):
    """Testability — 20 % weight."""
    if not ac_text or not ac_list:
        return 0.0
    # Positive signals: testable patterns across criteria
    testable_hits = sum(1 for pat in TESTABLE_PATTERNS if re.search(pat, ac_text, re.IGNORECASE))
    subjective_hits = sum(1 for pat in SUBJECTIVE_PATTERNS if re.search(pat, ac_text, re.IGNORECASE))

    # Each criterion should ideally be testable
    n_criteria = len(ac_list)
    gwt_count = len(re.findall(r"(?:Given|Scenario:)", ac_text, re.IGNORECASE))

    # Score from testable signals
    raw = min(testable_hits / max(n_criteria, 1) * 0.6 + (gwt_count / max(n_criteria, 1)) * 0.4, 1.0)
    # Penalise subjective language
    subjective_penalty = min(subjective_hits * 0.3, 0.5)
    return max(0.0, raw - subjective_penalty)


# --- Specificity / measurability ---
QUANT_PATTERNS = [
    r"\d+",  # any number
    r"within \d+", r"less than \d+", r"more than \d+", r"up to \d+",
    r"ms|milliseconds?|seconds?|minutes?|hours?|days?",
    r"max|min|limit|threshold",
    r"MB|KB|GB|bytes?",
    r"per (minute|second|hour|day|page|user)",
]

def score_specificity(ac_text, ac_list):
    """Specificity / measurability — 15 % weight."""
    if not ac_text or not ac_list:
        return 0.0
    n_criteria = len(ac_list)
    # Count quantified patterns across the whole text
    q_matches = sum(len(re.findall(pat, ac_text, re.IGNORECASE)) for pat in QUANT_PATTERNS)
    # Also count quoted literal strings
    quoted_literals = len(re.findall(r"'[^']+'|\"[^\"]+\"", ac_text))
    total_signals = q_matches + quoted_literals
    # Ideal: at least 2 quantifiers per criterion
    expected = n_criteria * 2
    raw = min(total_signals / max(expected, 1), 1.0)
    return raw


# --- One concern per criterion ---
COUPLING_WORDS = [r"\band\b", r"\bor\b"]

def score_one_concern(ac_text, ac_list):
    """One concern per criterion — 10 % weight."""
    if not ac_text or not ac_list:
        return 0.0
    n_criteria = len(ac_list)
    if n_criteria == 0:
        return 0.0
    # Count coupling words in each criterion
    total_couplings = 0
    for crit in ac_list:
        for pat in COUPLING_WORDS:
            total_couplings += len(re.findall(pat, crit, re.IGNORECASE))
    # Expected couplings: GWT scenarios naturally have some "and" in Given steps
    # Penalise if couplings > criteria count (-> too many concerns jammed)
    if total_couplings <= n_criteria:
        return 1.0
    excess = total_couplings - n_criteria
    return max(0.0, 1.0 - (excess / max(n_criteria, 1)) * 0.5)


# --- Clear structure ---
STRUCTURE_PATTERNS = [
    r"Scenario:", r"Given ", r"When ", r"Then ",
]
PROSE_INDICATORS = [r"\bUsers? should\b", r"\bit should\b", r"^\s*[-•*]\s+(?!Given|When|Then|Scenario)"]

def score_structure(ac_text, ac_list):
    """Clear structure (BDD/GWT bullets) — 10 % weight."""
    if not ac_text or not ac_list:
        return 0.0
    n_criteria = len(ac_list)
    # BDD structural hits
    bdd_hits = sum(len(re.findall(pat, ac_text, re.IGNORECASE)) for pat in STRUCTURE_PATTERNS)
    # If each criterion uses BDD structure, great
    if bdd_hits >= n_criteria * 2:
        return 1.0

    # Fallback: if it's a clean bullet list (one line per criterion, no prose)
    prose_hits = sum(len(re.findall(pat, ac_text, re.IGNORECASE)) for pat in PROSE_INDICATORS)
    bullet_ratio = 0.0
    for crit in ac_list:
        if crit.startswith("-") or crit.startswith("*") or crit.startswith("•"):
            bullet_ratio += 1.0
    bullet_ratio /= max(n_criteria, 1)

    # Score: BDD is best, then bullet list, then prose
    if bdd_hits >= n_criteria:
        return 0.9
    if bullet_ratio > 0.5:
        return 0.6
    if prose_hits > 0:
        return 0.2
    return 0.4


# --- Edge & negative cases ---
NEGATIVE_TERMS = [
    "invalid", "error", "empty", "expired", "exceed", "exceeds", "exceeded",
    "unauthorised", "unauthorized", "duplicate", "bounce", "bounced",
    "fail", "failed", "failure", "wrong", "missing", "denied",
    "forbidden", "bad request", "429", "401", "404", "500",
    "rejected", "blocked", "lock", "locked", "timeout",
    "corrupt", "malformed", "null", "undefined", "negative",
    "boundary", "edge case", "limit", "concurrency", "race",
    "opt.?out", "hard.?bounce", "revoked",
]

def score_edge_negative(ac_text, ac_list):
    """Edge & negative cases — 15 % weight."""
    if not ac_text or not ac_list:
        return 0.0
    n_criteria = len(ac_list)
    lower = ac_text.lower()
    # Count unique negative terms found
    found_terms = set()
    for term in NEGATIVE_TERMS:
        if term in lower:
            found_terms.add(term)
    negative_hits = len(found_terms)

    # Also count how many criteria contain at least one negative term
    crit_with_negative = 0
    for crit in ac_list:
        cl = crit.lower()
        if any(t in cl for t in NEGATIVE_TERMS):
            crit_with_negative += 1

    # Score: at least 30% of criteria should cover negative/edge cases
    neg_ratio = crit_with_negative / max(n_criteria, 1)
    expected = min(0.5, n_criteria * 0.3 / max(n_criteria, 1))  # expect 30%+ negative coverage
    # Bonus for diversity of negative terms
    diversity_bonus = min(negative_hits / 5.0, 0.3)  # up to 0.3 bonus
    raw = min(neg_ratio + diversity_bonus, 1.0)
    return raw


# --- Outcome focus ---
IMPLEMENTATION_VERBS = [
    r"\bcall\b", r"\binstall\b", r"\buse library\b", r"\bupdate (the )?database\b",
    r"\bwrite (a )?query\b", r"\bconnect\b", r"\bwork with .+ team\b",
    r"\brefactor\b", r"\bimplement\b", r"\badd (the )?(code|logic|function|method)\b",
    r"\brewrite\b", r"\bchange the (code|logic|function|backend|api)\b",
    r"\bmigrate\b", r"\bdeploy\b", r"\binvoke\b", r"\bquery the\b",
    r"\bmodify the schema\b", r"\binsert into\b", r"\bselect from\b",
]

OUTCOME_VERBS = [
    r"\b(sees?|views?|receives?|gets?|observes?|is (shown|displayed|redirected|notified))\b",
    r"\bcan (upload|download|export|filter|search|sort|view|see|access)\b",
]

def score_outcome_focus(ac_text, ac_list):
    """Outcome focus — 10 % weight."""
    if not ac_text or not ac_list:
        return 0.0
    n_criteria = len(ac_list)
    lower = ac_text.lower()

    impl_hits = sum(len(re.findall(pat, lower)) for pat in IMPLEMENTATION_VERBS)
    outcome_hits = sum(len(re.findall(pat, lower, re.IGNORECASE)) for pat in OUTCOME_VERBS)

    # If there are more implementation verbs than outcome verbs, penalise
    if impl_hits == 0 and outcome_hits == 0:
        # No strong signal either way — check if criteria describe user-facing behavior
        # GC: if the AC use GWT natural language, they're likely outcome-focused
        gwt_count = len(re.findall(r"(?:Given|When|Then|Scenario:)", ac_text))
        if gwt_count >= n_criteria * 2:
            return 0.7  # likely outcome-focused via GWT
        return 0.5  # neutral

    if outcome_hits >= impl_hits * 2:
        return 1.0
    if impl_hits == 0 and outcome_hits > 0:
        return 0.8
    # Penalise
    ratio = outcome_hits / max(impl_hits, 1)
    return min(ratio, 1.0)


# ---------------------------------------------------------------------------
# 4.  Per-ticket scoring pipeline
# ---------------------------------------------------------------------------
WEIGHTS = {
    "clarity": 20,
    "testability": 20,
    "specificity": 15,
    "one_concern": 10,
    "structure": 10,
    "edge_negative": 15,
    "outcome_focus": 10,
}

DIMENSION_NAMES = {
    "clarity": "Clarity",
    "testability": "Testability",
    "specificity": "Specificity",
    "one_concern": "One-concern",
    "structure": "Structure",
    "edge_negative": "Edge/negative",
    "outcome_focus": "Outcome focus",
}

def grade_label(score):
    if score >= 90:
        return "Excellent"
    if score >= 70:
        return "Good"
    if score >= 50:
        return "Needs Work"
    if score >= 20:
        return "Poor"
    return "No/Missing AC"

def score_ticket(issue):
    """Return dict with all scoring info for one issue."""
    ac_text, ac_list = extract_ac(issue)
    key = issue["key"]
    summary = issue.get("summary", "")
    itype = issue.get("issuetype", "")

    if not ac_list:
        return {
            "key": key,
            "summary": summary,
            "type": itype,
            "ac_present": "No",
            "ac_count": 0,
            "gwt_count": 0,
            "overall": 0.0,
            "grade": "No/Missing AC",
            "reason": "No acceptance criteria found",
            "improvement": "Write clear, testable acceptance criteria using BDD format",
            "dim_scores": {},
        }

    # Compute dimension scores (normalised 0–1)
    dim_scores = {
        "clarity": score_clarity(ac_text, ac_list),
        "testability": score_testability(ac_text, ac_list),
        "specificity": score_specificity(ac_text, ac_list),
        "one_concern": score_one_concern(ac_text, ac_list),
        "structure": score_structure(ac_text, ac_list),
        "edge_negative": score_edge_negative(ac_text, ac_list),
        "outcome_focus": score_outcome_focus(ac_text, ac_list),
    }

    # Weighted overall 0–100
    overall = sum(dim_scores[d] * WEIGHTS[d] for d in dim_scores)
    overall = round(overall, 1)

    # Count GWT-like criteria
    gwt_count = len(re.findall(r"(?:Scenario:|Given)", ac_text))

    # Generate reason from lowest dimensions
    sorted_dims = sorted(dim_scores.items(), key=lambda x: x[1])
    low_dims = [d for d, s in sorted_dims if s < 0.6]
    high_dims = [d for d, s in sorted_dims if s >= 0.8]

    if not low_dims and overall >= 80:
        reason = "Strong across all dimensions"
    elif not low_dims:
        reason = "Adequate across all dimensions"
    else:
        low_names = [DIMENSION_NAMES[d] for d in low_dims[:3]]
        reason = "Low on " + ", ".join(low_names)

    # Improvement: pick the single worst dimension
    worst_dim = sorted_dims[0][0]
    IMPROVEMENT_HINTS = {
        "clarity": "Remove vague terms like 'easy', 'fast', 'user-friendly'; be concrete",
        "testability": "Rewrite criteria as objectively verifiable pass/fail statements; use GWT format",
        "specificity": "Add quantifiers: time limits, counts, sizes, exact messages",
        "one_concern": "Split compound criteria into one behavior per bullet/scenario",
        "structure": "Use Given/When/Then (BDD/Gherkin) format for every criterion",
        "edge_negative": "Add negative and edge cases: errors, empties, boundaries, permissions",
        "outcome_focus": "Describe observable user outcomes, not implementation steps",
    }
    improvement = IMPROVEMENT_HINTS.get(worst_dim, "Follow BDD best practices")

    return {
        "key": key,
        "summary": summary,
        "type": itype,
        "ac_present": "Yes",
        "ac_count": len(ac_list),
        "gwt_count": gwt_count,
        "overall": overall,
        "grade": grade_label(overall),
        "reason": reason,
        "improvement": improvement,
        "dim_scores": dim_scores,
    }


# ---------------------------------------------------------------------------
# 5.  Process all tickets, sort by overall (descending)
# ---------------------------------------------------------------------------
results = [score_ticket(iss) for iss in issues]
results.sort(key=lambda r: (-r["overall"], r["key"]))

# ---------------------------------------------------------------------------
# 6.  Write .xlsx
# ---------------------------------------------------------------------------
today = date.today().strftime("%Y-%m-%d")
OUT_PATH = os.path.join(HERE, f"ac-quality-rank-{today}.xlsx")

wb = Workbook()
# Pin metadata timestamps so the workbook is byte-reproducible across runs
EPOCH = datetime(2000, 1, 1, 0, 0, 0)
wb.properties.created = EPOCH
wb.properties.modified = EPOCH

ws = wb.active
ws.title = "AC Quality Rank"

HEADERS = [
    "Rank", "Ticket Key", "Summary", "Type", "AC Present (Yes/No)",
    "# AC", "Criteria Count GWT", "Overall Score (0-100)", "Grade",
    "Reason", "Key Improvement", "Source",
]

# Write header row
header_font = Font(bold=True, size=11)
header_fill = PatternFill("solid", fgColor="4472C4")
header_font_white = Font(bold=True, size=11, color="FFFFFF")

for col_idx, h in enumerate(HEADERS, 1):
    cell = ws.cell(row=1, column=col_idx, value=h)
    cell.font = header_font_white
    cell.fill = header_fill
    cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

# Freeze top row
ws.freeze_panes = "A2"

# Conditional fill colours
green_fill = PatternFill("solid", fgColor="C6EFCE")
red_fill = PatternFill("solid", fgColor="FFC7CE")
amber_fill = PatternFill("solid", fgColor="FFEB9C")

for rank, r in enumerate(results, 1):
    row = rank + 1
    vals = [
        rank,
        r["key"],
        r["summary"],
        r["type"],
        r["ac_present"],
        r["ac_count"],
        r["gwt_count"],
        r["overall"],
        r["grade"],
        r["reason"],
        r["improvement"],
        SOURCE,
    ]
    for col_idx, v in enumerate(vals, 1):
        cell = ws.cell(row=row, column=col_idx, value=v)
        cell.alignment = Alignment(wrap_text=True, vertical="top")

    # Conditional fill
    score = r["overall"]
    fill = None
    if score >= 80:
        fill = green_fill
    elif score < 40:
        fill = red_fill
    # Grade-specific fills for 40-79 range
    elif score < 50:
        fill = amber_fill

    if fill:
        for col_idx in range(1, len(HEADERS) + 1):
            ws.cell(row=row, column=col_idx).fill = fill

# Column widths
col_widths = {
    1: 6, 2: 14, 3: 35, 4: 16, 5: 18,
    6: 6, 7: 20, 8: 20, 9: 16,
    10: 50, 11: 55, 12: 22,
}
for col, w in col_widths.items():
    ws.column_dimensions[get_column_letter(col)].width = w

wb.save(OUT_PATH)
print(f"✅ Workbook written → {OUT_PATH}")

# ---------------------------------------------------------------------------
# 7.  Console summary
# ---------------------------------------------------------------------------
scores = [r["overall"] for r in results]
avg = sum(scores) / len(scores)
top3 = results[:3]
bottom3 = results[-3:]

print(f"\n📊 Summary")
print(f"   Issues ranked : {len(results)}")
print(f"   Average score : {avg:.1f} / 100")
print(f"   Source        : {SOURCE}")
print()

print("🏆 Top 3 tickets:")
for r in top3:
    print(f"   {r['key']:14s}  {r['overall']:5.1f}  {r['grade']:14s}  {r['summary']}")

print("\n⛔ Bottom 3 tickets:")
for r in bottom3:
    print(f"   {r['key']:14s}  {r['overall']:5.1f}  {r['grade']:14s}  {r['summary']}")

print(f"\n📈 Grade distribution:")
grades = {}
for r in results:
    g = r["grade"]
    grades[g] = grades.get(g, 0) + 1
for g in ["Excellent", "Good", "Needs Work", "Poor", "No/Missing AC"]:
    if g in grades:
        print(f"   {g:16s}  {grades[g]} tickets")

print("\n✅ Done.")