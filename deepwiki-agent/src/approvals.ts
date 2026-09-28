import { RiskLevel, PermissionProfile } from './types';
import { PermissionManager } from './permission';
import { TraceService } from './trace';

/**
 * approvals.ts
 * -------------
 * Risk-graded human-in-the-loop approval gate.
 *
 * Industry practice (2026): low-risk actions may auto-run with logging, but
 * medium/high-risk actions (PRs, prod code, network, destructive ops) require
 * explicit human approval, and irreversible actions can never be self-approved by the
 * initiating automation (GitHub Copilot cloud-agent rules, MCP incremental-scope
 * consent). We model that as a deterministic matrix: every tool/step carries a
 * risk level; the matrix decides if approval is needed for the current permission
 * profile; the human (or a policy auto-approve for trusted low-risk) resolves.
 */

interface ApprovalDecision {
  requiresHuman: boolean;
  allowed: boolean; // whether it can run without a human in this session
  reason: string;
  riskLevel: RiskLevel;
  canApprove: boolean;
}

const RISK_RANK: Record<RiskLevel, number> = { info: 0, low: 1, medium: 2, high: 3, critical: 4 };

/** Threshold: a step of this risk can auto-run only if its rank <= the profile's max. */
const PROFILE_MAX_RISK: Record<PermissionProfile, RiskLevel> = {
  // read-only can still auto-perform low-risk reads/docs; anything else needs approval
  readonly: 'low',
  'workspace-write': 'low',
  'workspace-exec': 'medium',
  elevated: 'high',
};

export class ApprovalEngine {
  constructor(
    private perms: PermissionManager,
    private trace: TraceService,
    private autoApprovePatterns: { action?: string; tool?: string; risk?: RiskLevel }[] = [],
  ) {}

  /**
   * Evaluate a proposed action under the current permission profile.
   * Returns whether it needs a human gate before execution.
   */
  evaluate(action: string, description: string, opts?: { risk?: RiskLevel; tool?: string; irreversible?: boolean }): ApprovalDecision {
    const risk: RiskLevel = opts?.risk || this.guessRisk(action, opts?.tool);
    const profile = this.perms.def();
    const maxAuto = PROFILE_MAX_RISK[this.perms.current().profile];

    // Irreversible/destructive always require a human, never auto.
    if (opts?.irreversible || RISK_RANK[risk] >= RISK_RANK.critical) {
      return {
        requiresHuman: true,
        allowed: false,
        riskLevel: 'critical',
        reason: `Critical/irreversible action "${action}" always requires explicit human approval.`,
        canApprove: false,
      };
    }

    // Does the current profile permit this kind of operation at all?
    const capabilityOk = this.capabilityOk(action, opts?.tool);
    if (!capabilityOk.allowed) {
      return { ...capabilityOk, requiresHuman: true, canApprove: true, riskLevel: risk };
    }

    // Auto-approve if within profile threshold and matches an allowed pattern.
    const auto = this.autoApprovePatterns.some(
      (p) =>
        (!p.action || p.action === action) &&
        (!p.tool || p.tool === opts?.tool) &&
        (!p.risk || p.risk === risk),
    );
    if (auto && RISK_RANK[risk] <= RISK_RANK[maxAuto]) {
      return {
        requiresHuman: false,
        allowed: true,
        canApprove: true,
        riskLevel: risk,
        reason: `Auto-approved (${risk} ≤ ${maxAuto}).`,
      };
    }

    return {
      requiresHuman: RISK_RANK[risk] > RISK_RANK[maxAuto],
      allowed: false,
      canApprove: true,
      riskLevel: risk,
      reason: `Step "${action}" (${risk}) exceeds profile max (${maxAuto}). Needs approval.`,
    };
  }

  private capabilityOk(action: string, tool?: string): { allowed: boolean; reason: string } {
    const d = this.perms.def();
    if (!d.allowExec && /^(exec|run|bash|shell|command|install|delete|rm)/.test(action)) {
      return { allowed: false, reason: `Profile "${d.label}" does not allow execution.` };
    }
    if (!d.allowNetwork && (tool === 'mcp' || /^(web|mcp|http|fetch)/.test(action))) {
      return { allowed: false, reason: `Profile "${d.label}" does not allow network/MCP.` };
    }
    if (tool === 'write' && !d.allowWrite.length) {
      return { allowed: false, reason: `Profile "${d.label}" is read-only.` };
    }
    return { allowed: true, reason: 'capability ok' };
  }

  private guessRisk(action: string, tool?: string): RiskLevel {
    const low = /^(read|list|search|summarize|summarise|chat|analy|query|inspect|info|lint|format)/i;
    const med = /^(write|edit|patch|update|refactor|test|build|compile|verify)/i;
    const high = /^(exec|run|install|network|mcp|pr|push|merge|deploy|delete|rm|move|rename|approve|grant)/i;
    if (/^(delete|rm|drop|destroy|force)/i.test(action)) return 'critical';
    if (high.test(action) || (tool === 'mcp' && action !== 'read')) return 'high';
    if (med.test(action)) return 'medium';
    if (low.test(action)) return 'low';
    return 'info';
  }

  /**
   * Record an approval-gate event to the audit trail (approval opinion persisted).
   */
  request(action: string, description: string, d: ApprovalDecision): void {
    this.trace.agent('approval.request', `${action} — ${description} — ${d.reason}`, {
      action: 'approval.request',
      riskLevel: d.riskLevel,
      approved: false,
    });
  }

  grant(action: string, by: 'user' | 'policy-auto'): void {
    this.trace.agent('approval.grant', `${action} approved by ${by}`, {
      action: 'approval.grant',
      approved: true,
    });
  }
}
