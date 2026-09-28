import * as fs from 'fs';
import * as path from 'path';
import * as crypto from 'crypto';
import { AuditEntry, TraceSession, PermissionProfile } from './types';

/**
 * trace.ts
 * ---------
 * Append-only, human-readable audit trail + per-session observability.
 *
 * Industry practice (2026): production agents emit OpenTelemetry spans and keep an
 * immutable record tying intent → agent run → approval → result, for SOC-2 / SIEM
 * audits and deterministic replay. We write an append-only JSONL (and optional
 * markdown mirror) so a human or SIEM forwarder can consume it without tooling.
 *
 * Cost estimation: rough token→USD using a per-model rate (convert EUR to USD at a
 * fixed approximation; dollars per million tokens). Deterministic and cheap.
 */
const TOKEN_RATES_USD: Record<string, { in: number; out: number }> = {
  'gemini-1.5-pro': { in: 1.25, out: 5.0 },          // per 1M tokens (approx list price)
  'gemini-1.5-flash': { in: 0.35, out: 1.05 },
  'gemini-2.0-flash': { in: 0.10, out: 0.40 },
};

export function estimateCostUsd(model: string, tokensIn: number, tokensOut: number): number {
  const r = TOKEN_RATES_USD[model] || TOKEN_RATES_USD['gemini-1.5-pro'];
  return (tokensIn / 1_000_000) * r.in + (tokensOut / 1_000_000) * r.out;
}

// ── Shared token aggregate (module-level) ──────────────────────────
const _agg = { tokensIn: 0, tokensOut: 0, calls: 0 };

/** Record token usage for the global/session aggregate (used by dashboard + budgets). */
export function reportTokenUsage(_model: string, tokensIn: number, tokensOut: number): void {
  _agg.tokensIn += tokensIn;
  _agg.tokensOut += tokensOut;
  _agg.calls += 1;
}

/** Read the aggregate — used by UI to show cost/budget without a session. */
export function getTokenUsage(): { tokensIn: number; tokensOut: number; calls: number; costUsd: number } {
  return {
    tokensIn: _agg.tokensIn,
    tokensOut: _agg.tokensOut,
    calls: _agg.calls,
    costUsd: _agg.tokensIn > 0
      ? estimateCostUsd('gemini-1.5-pro', _agg.tokensIn, _agg.tokensOut)
      : 0,
  };
}

export class TraceService {
  private entries: AuditEntry[] = [];
  private seq = 0;
  readonly sessionId: string;

  constructor(
    private logDir: string,
    private model = 'gemini-1.5-pro',
    readonly workspaceRoot: string,
    readonly profile: PermissionProfile,
  ) {
    this.sessionId = crypto.randomUUID();
    fs.mkdirSync(logDir, { recursive: true });
  }

  private get jsonlPath(): string {
    return path.join(this.logDir, `${this.sessionId}.jsonl`);
  }

  private get mdPath(): string {
    return path.join(this.logDir, `${this.sessionId}.md`);
  }

  private append(entry: Omit<AuditEntry, 'seq' | 'timestamp' | 'sessionId'>): AuditEntry {
    const full: AuditEntry = {
      seq: ++this.seq,
      timestamp: new Date().toISOString(),
      sessionId: this.sessionId,
      ...entry,
    };
    this.entries.push(full);
    fs.appendFileSync(this.jsonlPath, JSON.stringify(full) + '\n', 'utf-8');
    this.appendMarkdown(full);
    return full;
  }

  private appendMarkdown(e: AuditEntry): void {
    const line = `- \`${e.timestamp}\` **[${e.source}]** ${e.action} — ${e.detail}${e.tool ? ` \`tool=${e.tool}\`` : ''}${e.riskLevel ? ` \`risk=${e.riskLevel}\`` : ''}${e.costEstimateUsd !== undefined ? ` \`cost≈$${e.costEstimateUsd.toFixed(4)}\`` : ''}\n`;
    fs.appendFileSync(this.mdPath, line, 'utf-8');
  }

  agent(action: string, detail: string, opts?: Partial<AuditEntry>): AuditEntry {
    return this.append({ source: 'agent', action, detail, ...opts });
  }

  user(action: string, detail: string, opts?: Partial<AuditEntry>): AuditEntry {
    return this.append({ source: 'user', action, detail, ...opts });
  }

  system(action: string, detail: string, opts?: Partial<AuditEntry>): AuditEntry {
    return this.append({ source: 'system', action, detail, ...opts });
  }

  llm(action: string, detail: string, tokensIn: number, tokensOut: number, opts?: Partial<AuditEntry>): AuditEntry {
    return this.append({
      source: 'llm',
      action,
      detail,
      tokensIn,
      tokensOut,
      costEstimateUsd: estimateCostUsd(this.model, tokensIn, tokensOut),
      ...opts,
    });
  }

  getEntries(): AuditEntry[] {
    return [...this.entries];
  }

  /** Summarise the session for dashboards / SIEM. */
  summary(): TraceSession {
    const llms = this.entries.filter((e) => e.source === 'llm');
    const approvals = this.entries.filter((e) => e.action === 'approval.request' || e.action === 'approval.grant');
    const errors = this.entries.filter((e) => e.action === 'error');
    return {
      sessionId: this.sessionId,
      startedAt: this.entries[0]?.timestamp || new Date().toISOString(),
      workspaceRoot: this.workspaceRoot,
      permissionProfile: this.profile,
      llmModel: this.model,
      totalTokensIn: llms.reduce((s, e) => s + (e.tokensIn || 0), 0),
      totalTokensOut: llms.reduce((s, e) => s + (e.tokensOut || 0), 0),
      totalCostUsd: llms.reduce((s, e) => s + (e.costEstimateUsd || 0), 0),
      stepsCompleted: this.entries.filter((e) => e.action === 'step.completed').length,
      approvalsRequested: approvals.filter((e) => e.action === 'approval.request').length,
      approvalsGranted: approvals.filter((e) => e.approved === true).length,
      errors: errors.length,
    };
  }

  /** Immutable copy protection concept: hash each line so tampering is detectable. */
  integritySnapshot(): { count: number; md5: string } {
    const file = this.jsonlPath;
    if (!fs.existsSync(file)) return { count: 0, md5: '' };
    const content = fs.readFileSync(file, 'utf-8');
    return { count: this.entries.length, md5: crypto.createHash('md5').update(content).digest('hex') };
  }
}
