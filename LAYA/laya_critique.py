#!/usr/bin/env python3
"""
LAYA Critique Runner — laya_critique.py
=========================================

Critique acceptance criteria tickets and Promesa Decision Canvases using
the LAYA System 1 decision engine.

The LAYA engine produces *calibrated structured decisions* (enum choices,
scores, booleans) with principled confidence estimates — faster and more
repeatable than generative LLM critique.

Usage
-----
    # Score all tickets from sample-ac-tickets.json
    python LAYA/laya_critique.py tickets

    # Score specific tickets by key
    python LAYA/laya_critique.py tickets --keys PROJ-104 PROJ-109

    # Critique all Promesa canvases in promesa-canvases/
    python LAYA/laya_critique.py canvases

    # Critique one specific canvas
    python LAYA/laya_critique.py canvases --file promesa-canvases/promesa-canvas-PROJ-104.md

    # Combined: tickets + canvases in one run
    python LAYA/laya_critique.py all

    # Offline mode (skip LAYA model download, use deterministic fallback)
    python LAYA/laya_critique.py tickets --offline

    # Output as JSON
    python LAYA/laya_critique.py tickets --json
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# Ensure UTF-8 console output on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("laya_critique")

# ── Paths ──────────────────────────────────────────────────────────────────
HERE = Path(__file__).resolve().parent.parent  # experiments/
SAMPLE_PATH = HERE / "deepwiki-agent" / "sample-ac-tickets.json"
CANVAS_DIR = HERE / "promesa-canvases"
OUT_DIR = HERE / "LAYA" / "reports"


def _ensure_out_dir():
    OUT_DIR.mkdir(parents=True, exist_ok=True)


# ── Load tickets ──────────────────────────────────────────────────────────

def load_tickets(path: Optional[Path] = None) -> List[dict]:
    """Load tickets from sample-ac-tickets.json."""
    p = path or SAMPLE_PATH
    if not p.exists():
        print(f"❌ Ticket file not found: {p}")
        sys.exit(1)
    with open(p, "r", encoding="utf-8") as f:
        data = json.load(f)
    issues = data.get("issues", [])
    for iss in issues:
        iss["_source"] = p.name
    print(f"📋 Loaded {len(issues)} tickets from {p}")
    return issues


def load_canvas_files() -> List[Path]:
    """Find all canvas markdown files."""
    d = CANVAS_DIR
    if not d.exists():
        print(f"❌ Canvas directory not found: {d}")
        return []
    files = sorted(d.glob("promesa-canvas-*.md"))
    print(f"📋 Found {len(files)} canvas files in {d}")
    return files


# ── Import critic modules ─────────────────────────────────────────────────

def _get_ac_critic(offline: bool = False):
    sys.path.insert(0, str(HERE))
    from LAYA.critique.ac_critique import AcceptanceCriteriaCritic
    from LAYA.critique.engine import LayaCritic
    laya = LayaCritic(prefer_offline=offline)
    return AcceptanceCriteriaCritic(laya_critic=laya)


def _get_canvas_critic(offline: bool = False):
    sys.path.insert(0, str(HERE))
    from LAYA.critique.canvas_critique import CanvasCritic
    from LAYA.critique.engine import LayaCritic
    laya = LayaCritic(prefer_offline=offline)
    return CanvasCritic(laya_critic=laya)


# ── Commands ──────────────────────────────────────────────────────────────

def cmd_tickets(args: argparse.Namespace):
    """Critique acceptance criteria tickets."""
    _ensure_out_dir()
    tickets = load_tickets()
    if args.keys:
        tickets = [t for t in tickets if t.get("key") in args.keys]
        found = [t.get("key", "?") for t in tickets]
        if args.keys:
            missing = set(args.keys) - set(found)
            if missing:
                print(f"⚠️  Keys not found: {', '.join(missing)}")

    critic = _get_ac_critic(offline=args.offline)
    print(f"🧠 LAYA engine: {'online' if not critic._laya._fallback_active else 'OFFLINE (fallback)'}")

    reports = critic.critique_batch(tickets)

    # Sort by overall score
    reports.sort(key=lambda r: r.get("overall", 0), reverse=True)

    # Save individual reports
    for r in reports:
        key = r["key"]
        fname = OUT_DIR / f"laya-critique-{key}.md"
        fname.write_text(r["report_markdown"], encoding="utf-8")
        print(f"  ✍️  Wrote {fname.name}")

    # Summary table
    print("\n" + "═" * 72)
    print("  LAYA CRITIQUE SUMMARY")
    print("═" * 72)
    print(f"  {'Key':<12} {'Score':>7}  {'Gate':<18} {'ACs':>4}")
    print(f"  {'—'*12} {'—'*7}  {'—'*18} {'—'*4}")
    for r in reports:
        print(f"  {r['key']:<12} {r['overall']:>6.1f}  {r['gate']:<18} {r['ac_count']:>4}")
    print("═" * 72)

    # Combined JSON
    if args.json:
        json_path = OUT_DIR / "laya-critique-tickets.json"
        json_data = []
        for r in reports:
            json_data.append({
                "key": r["key"],
                "summary": r["summary"],
                "overall": r["overall"],
                "gate": r["gate"],
                "qualifier": r.get("qualifier", ""),
                "ac_count": r["ac_count"],
                "dimensions": {
                    d: {"score": v["score"], "finding": v["finding"]}
                    for d, v in r.get("dimensions", {}).items()
                },
                "generated_at": r["generated_at"],
            })
        json_path.write_text(
            json.dumps(json_data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"\n📊 JSON summary: {json_path}")

    # Composite markdown report
    md_path = OUT_DIR / "laya-critique-all-tickets.md"
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(f"# LAYA Critique — All Tickets\n\n")
        f.write(f"_Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_\n\n")
        f.write(f"| Key | Score | Gate | ACs | Dimensions |\n")
        f.write(f"|-----|-------|------|-----|------------|\n")
        for r in reports:
            dim_scores = ", ".join(
                f"{d}: {v['score']:.2f}"
                for d, v in r.get("dimensions", {}).items()
            )
            f.write(f"| {r['key']} | {r['overall']:.1f} | {r['gate']} | {r['ac_count']} | {dim_scores} |\n")
        f.write("\n---\n## Individual Reports\n\n")
        for r in reports:
            f.write(r["report_markdown"])
            f.write("\n\n---\n\n")
    print(f"📄 Composite report: {md_path}")


def cmd_canvases(args: argparse.Namespace):
    """Critique Promesa Decision Canvases."""
    _ensure_out_dir()

    if args.file:
        paths = [Path(args.file)]
    else:
        paths = load_canvas_files()

    if not paths:
        print("❌ No canvas files to critique.")
        return

    critic = _get_canvas_critic(offline=args.offline)
    reports = critic.critique_batch(paths)
    reports.sort(key=lambda r: r.get("overall", 0), reverse=True)

    # Save individual reports
    for r in reports:
        fname = OUT_DIR / f"laya-canvas-critique-{r['key']}.md"
        fname.write_text(r["report_markdown"], encoding="utf-8")
        print(f"  ✍️  Wrote {fname.name}")

    print("\n" + "═" * 72)
    print("  LAYA CANVAS CRITIQUE SUMMARY")
    print("═" * 72)
    print(f"  {'Key':<12} {'Score':>7}  {'Gate':<18} {'ACs':>4}")
    print(f"  {'—'*12} {'—'*7}  {'—'*18} {'—'*4}")
    for r in reports:
        print(f"  {r['key']:<12} {r['overall']:>6.1f}  {r['gate']:<18} {r['ac_count']:>4}")
    print("═" * 72)

    # Composite
    md_path = OUT_DIR / "laya-critique-all-canvases.md"
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(f"# LAYA Critique — All Canvases\n\n")
        f.write(f"_Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_\n\n")
        f.write(f"| Key | Score | Gate | ACs |\n")
        f.write(f"|-----|-------|------|-----|\n")
        for r in reports:
            f.write(f"| {r['key']} | {r['overall']:.1f} | {r['gate']} | {r['ac_count']} |\n")
        f.write("\n---\n## Individual Reports\n\n")
        for r in reports:
            f.write(r["report_markdown"])
            f.write("\n\n---\n\n")
    print(f"📄 Composite report: {md_path}")


def cmd_all(args: argparse.Namespace):
    """Run both tickets and canvases critique."""
    print("═" * 72)
    print("  LAYA CRITIQUE — FULL RUN")
    print("═" * 72)
    cmd_tickets(args)
    print()
    cmd_canvases(args)


# ── CLI ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="LAYA Critique Agent — grade AC tickets and Promesa canvases",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "command",
        choices=["tickets", "canvases", "all"],
        help="What to critique",
    )
    parser.add_argument(
        "--keys", nargs="+", default=None,
        help="Ticket keys to critique (e.g. PROJ-104 PROJ-109)",
    )
    parser.add_argument(
        "--file", type=str, default=None,
        help="Single canvas file to critique",
    )
    parser.add_argument(
        "--offline", action="store_true",
        help="Skip LAYA model download; use deterministic fallback",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Output JSON summary alongside markdown reports",
    )

    args = parser.parse_args()

    if args.command == "tickets":
        cmd_tickets(args)
    elif args.command == "canvases":
        cmd_canvases(args)
    elif args.command == "all":
        cmd_all(args)


if __name__ == "__main__":
    main()