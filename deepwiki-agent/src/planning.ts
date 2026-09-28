import { Plan, PlanStep, Risk } from './types';

/**
 * Planning system for the agent.
 * Produces 3 best deterministic plans, each with a Mermaid flowchart.
 * The human picks A / B / C or chats with the agent to create a new combined plan.
 */
export class PlanningService {
  constructor() {}

  mermaidFor(steps: PlanStep[]): string {
    const lines: string[] = ['flowchart TD'];
    lines.push('  A([Start])');
    let prev = 'A';
    steps.forEach((s, i) => {
      const id = `S${i + 1}`;
      const shape =
        s.action === 'ask-human'
          ? `{${s.description.replace(/"/g, "'")}}`
          : s.action === 'verify' || s.action === 'test'
            ? `>${s.description.replace(/"/g, "'")}]`
            : `[${s.description.replace(/"/g, "'")}]`;
      lines.push(`  ${id}${shape}`);
      lines.push(`  ${prev} --> ${id}`);
      prev = id;
    });
    lines.push(`  ${prev} --> Z([Done])`);
    return lines.join('\n');
  }

  /** Deterministic fallback plans when the LLM is unavailable — still grounded in facts. */
  buildFallback(stepIdea: PlanStep[], risks: Risk[], labelPrefix: string, targetRisk = ''): Plan[] {
    const base: Plan[] = [
      this.makePlan('A', 'Investigate', stepIdea, risks),
      this.makePlan('B', 'Verify-first', stepIdea, risks),
      this.makePlan('C', 'Test-driven', stepIdea, risks),
    ];
    return base;
  }

  private makePlan(label: string, title: string, steps: PlanStep[], risks: Risk[]): Plan {
    const reordered = this.reorder(label, steps);
    return {
      id: `plan-${label.toLowerCase()}-${Date.now()}`,
      label,
      title,
      summary: `${title}: deterministic ground-truth-first execution of ${reordered.length} steps`,
      steps: reordered,
      mermaid: this.mermaidFor(reordered),
      risks,
      confidence: 'LOW (deterministic recompute)',
    };
  }

  /** Deterministic step reordering based on the variation label. */
  reorder(label: string, steps: PlanStep[]): PlanStep[] {
    const copy = [...steps];
    if (label === 'A') {
      // investigate-heavy
      copy.sort((a, b) => (a.action === 'investigate' ? -1 : 0) - (b.action === 'investigate' ? -1 : 0));
    } else if (label === 'B') {
      // verify-heavy at the start
      copy.sort((a, b) => (a.action === 'verify' ? -1 : 0) - (b.action === 'verify' ? -1 : 0));
    } else if (label === 'C') {
      // test-first
      copy.sort((a, b) => (a.action === 'test' ? -1 : 0) - (b.action === 'test' ? -1 : 0));
    }
    return copy.map((s, i) => ({ ...s, order: i + 1 }));
  }
}
