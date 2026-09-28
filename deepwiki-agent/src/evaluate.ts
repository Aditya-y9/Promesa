import { z } from 'zod';
import { Plan, PlanStep, Risk } from './types';
import { ValidationResult } from './types';

/**
 * evaluate.ts
 * -----------
 * Schema validation for agent outputs (plans, steps, risks).
 *
 * Industry practice (2026): structured output schemas (Zod, JSON Schema) are the
 * deterministic way to enforce that LLM-generated plans and tool calls conform to
 * what the agent loop expects. Without it, malformed output can break execution or
 * introduce logic bugs.
 *
 * We use Zod because it is a zero-dep-from-scratch-avoidance pick and gives us typed
 * parse-with-fallback at runtime — the agent never proceeds with an invalid plan.
 */

// ── Zod schemas ────────────────────────────────────────────────────────

const planStepSchema = z.object({
  order: z.number().int().positive(),
  action: z.enum(['modify', 'create', 'verify', 'investigate', 'test', 'ask-human']),
  description: z.string().min(1, 'step description required'),
  filesAffected: z.array(z.string()).default([]),
  guardrailNote: z.string().optional(),
});

const riskSchema = z.object({
  level: z.enum(['LOW', 'MEDIUM', 'HIGH', 'CRITICAL']),
  kind: z.enum(['upstream', 'downstream', 'contract', 'policy', 'assumption']),
  description: z.string().min(1),
  mitigation: z.string().optional(),
  verifiedFrom: z.array(z.string()).default([]),
});

export const planSchema = z.object({
  title: z.string().min(1, 'plan title required'),
  summary: z.string().min(1, 'plan summary required'),
  steps: z.array(planStepSchema).min(1, 'at least one step required'),
  risks: z.array(riskSchema).default([]),
  confidence: z.string().optional(),
});

// ── Evaluators ─────────────────────────────────────────────────────────

/** Validate a single Plan object. Returns structured errors + warnings. */
export function validatePlan(p: Partial<Plan>): ValidationResult {
  const errors: string[] = [];
  const warnings: string[] = [];

  // Required fields
  if (!p.title) errors.push('Plan is missing a title.');
  if (!p.steps || p.steps.length === 0) errors.push('Plan has no steps.');

  if (p.steps) {
    // Check for duplicate order numbers
    const orders = p.steps.map((s) => s.order);
    if (new Set(orders).size !== orders.length) {
      errors.push('Steps have duplicate order numbers.');
    }
    // Check for gaps in ordering
    const sorted = [...orders].sort((a, b) => a - b);
    for (let i = 0; i < sorted.length; i++) {
      if (sorted[i] !== i + 1) {
        warnings.push(`Step order has gap or unexpected sequence: expected ${i + 1}, got ${sorted[i]}.`);
        break;
      }
    }
    // Check for steps with no files affected in modify/create actions
    for (const s of p.steps) {
      if ((s.action === 'modify' || s.action === 'create') && (!s.filesAffected || s.filesAffected.length === 0)) {
        warnings.push(`Step ${s.order}: "${s.action}" action but no filesAffected listed.`);
      }
    }
  }

  if (p.risks) {
    for (const [i, r] of p.risks.entries()) {
      if (!r.description) errors.push(`Risk #${i + 1} is missing a description.`);
      if (r.level && !['LOW', 'MEDIUM', 'HIGH', 'CRITICAL'].includes(r.level)) {
        errors.push(`Risk #${i + 1} has invalid level "${r.level}".`);
      }
    }
  }

  if (errors.length === 0 && (!p.mermaid || typeof p.mermaid !== 'string')) {
    warnings.push('Plan has no Mermaid flowchart.');
  }

  return { ok: errors.length === 0, errors, warnings };
}

/** Validate a raw JSON parse (from LLM output) vs the schema. Returns sanitized Plan or null. */
export function validateAndSanitizePlan(raw: unknown): { plan: Plan | null; result: ValidationResult } {
  const parsed = planSchema.safeParse(raw);
  if (!parsed.success) {
    const errs = parsed.error.issues.map((i) => `${i.path.join('.')}: ${i.message}`);
    return { plan: null, result: { ok: false, errors: errs, warnings: [] } };
  }
  const p = parsed.data;
  const plan: Plan = {
    id: `plan-validated-${Date.now()}`,
    label: '?',
    title: p.title,
    summary: p.summary,
    steps: p.steps,
    risks: (p.risks || []) as Risk[],
    mermaid: '',
    confidence: p.confidence || 'MEDIUM',
  };
  return { plan, result: { ok: true, errors: [], warnings: [] } };
}

/** Deterministic check: all plan steps reference real actions. */
export function hasUnsafeActions(steps: PlanStep[]): string[] {
  const forbidden = ['delete_branch', 'force_push', 'drop_table', 'rm_rf', 'sudo_exec'];
  return steps
    .filter((s) => forbidden.some((f) => s.description.toLowerCase().includes(f)))
    .map((s) => `Step ${s.order} may use unsafe operation: "${s.description}"`);
}