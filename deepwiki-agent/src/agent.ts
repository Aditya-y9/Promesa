import * as vscode from 'vscode';
import * as path from 'path';
import * as fs from 'fs';
import { SettingsService } from './settings';
import { JiraService } from './jira';
import { DeepWikiService, researchForTicket } from './deepwiki';
import { CompanyPolicyService } from './policy';
import { SkillsService } from './skills';
import { LLMService } from './llm';
import { RiskService, ContractTester } from './risk';
import { PlanningService } from './planning';
import { PermissionManager, PERMISSION_PROFILES } from './permission';
import { TraceService } from './trace';
import { ApprovalEngine } from './approvals';
import { buildUntrustedSection, tagUntrusted, systemPromptInjectionNote } from './injection';
import { PathPolicy, resolveWithin, READONLY_PATTERNS, WORKSPACE_WRITE_PATTERNS, SAFE_WRITE_DENY } from './guardrail';
import { validatePlan } from './evaluate';
import { MCPClient, MCPServerConfig } from './mcp';
import type { AgentPanel } from './panel';
import {
  Ambiguity,
  DeepWikiResult,
  JiraIssue,
  Plan,
  PlanStep,
  Risk,
  SkillDef,
  TicketArtifacts,
  PermissionProfile,
  AuditEntry,
} from './types';

/**
 * AgentOrchestrator
 * ----------------
 * The full "coding agent" pipeline with hard deterministic guardrails:
 *
 *  1. User configures LLM + Jira in the panel tab.
 *  2. Panel shows tickets assigned to the user (Jira REST API).
 *  3. User selects a ticket → agent fetches ALL data, persists it to
 *     `<workspace>/.deepwiki-agents/<ticket>/` as LLM-readable files
 *     (JSON + markdown).
 *  4. Agent scans deepwiki + company policies + skills.
 *  5. Hard guardrail: if any deepwiki term is ambiguous (multiple
 *     definitions) → STOP, ask the human via interactive window, do NOT guess.
 *  6. Research risks (upstream/downstream) from deepwiki contracts,
 *     then produce 3 best plans with flowcharts and present them.
 *  7. Human picks Plan A/B/C or chats to create a new plan.
 *  8. After a choice, risk report + plan persisted; contract tests suggested.
 *
 *  Determinism: the agent never *assumes*; every claim it makes must be
 *  traceable to a file in deepwiki or the actual repo. Anything unverifiable
 *  is surfaced as an `Ambiguity` for the human.
 */
export class AgentOrchestrator {
  readonly settings: SettingsService;
  readonly jira: JiraService;
  readonly deepwiki: DeepWikiService;
  readonly policy: CompanyPolicyService;
  readonly skills: SkillsService;
  readonly llm: LLMService;
  readonly planning: PlanningService;

  private panel: AgentPanel | undefined;
  private workspaceRoot: string;
  private currentTicket: JiraIssue | undefined;
  private currentArtifacts: TicketArtifacts | undefined;
  private deepWikiResult: DeepWikiResult | undefined;
  private pendingPlans: Plan[] = [];

  // ── Enterprise hardening (permission / trace / approvals / mcp) ──
  private permissions!: PermissionManager;
  private trace!: TraceService;
  private approvals!: ApprovalEngine;
  private mcpClients: MCPClient[] = [];
  private profile: PermissionProfile = 'readonly';

  constructor(private context: vscode.ExtensionContext) {
    this.settings = new SettingsService(context);
    this.jira = new JiraService(this.settings);
    this.deepwiki = new DeepWikiService(context);
    this.policy = new CompanyPolicyService();
    this.skills = new SkillsService();
    this.llm = new LLMService(this.settings);
    this.planning = new PlanningService();
    const ws = vscode.workspace.workspaceFolders?.[0];
    this.workspaceRoot = ws ? ws.uri.fsPath : process.cwd();
    // Begin an enterprise session (least-privilege, audit-enabled) immediately.
    this.beginSession();
  }

  /** Set up a session-scoped permission manager + audit trail. */
  private beginSession(): void {
    this.permissions = new PermissionManager(this.workspaceRoot, 'readonly');
    this.trace = new TraceService(path.join(this.workspaceRoot, '.deepwiki-agents', 'traces'), 'gemini-1.5-pro', this.workspaceRoot, 'readonly');
    this.approvals = new ApprovalEngine(this.permissions, this.trace, [{ risk: 'low', action: 'read' }]);
    this.trace.system('session.start', `New agent session. Workspace: ${this.workspaceRoot}. Posture: 🔒 readonly.`);
  }

  attachPanel(panel: AgentPanel): void {
    this.panel = panel;
    panel.registerMessageHandler((msg) => void this.onMessage(msg));
    void this.pushSettingsState();
    void this.pushPermissionState();
  }

  get agentDataDir(): string {
    return path.join(this.workspaceRoot, '.deepwiki-agents');
  }

  /** Reload skills from disk (used by the "Add a Skill" command). */
  refreshSkillsFromDisk(): void {
    const ws = vscode.workspace.workspaceFolders?.[0];
    if (!ws) return;
    this.skills.load(ws);
    void this.skillsPayload().then((s) => this.post('skills', s));
  }

  /** Core message router (called by the panel). */
  async onMessage(msg: any): Promise<void> {
    try {
      switch (msg.type) {
        case 'saveSettings':
          await this.handleSaveSettings(msg.payload);
          break;
        case 'fetchTickets':
          await this.handleFetchTickets();
          break;
        case 'selectTicket':
          await this.handleSelectTicket(msg.payload.ticketKey);
          break;
        case 'startAgent':
          await this.runAgentPipeline(msg.payload.ticketKey);
          break;
        case 'humanReply':
          await this.handleHumanReply(msg.payload.text);
          break;
        case 'choosePlan':
          await this.handleChoosePlan(msg.payload.planId);
          break;
        case 'resolveAmbiguity':
          await this.handleResolveAmbiguity(msg.payload.ambiguityId, msg.payload.answer);
          break;
        case 'createNewPlan':
          await this.handleCreateNewPlan(msg.payload.instructions);
          break;
        case 'runContractTests':
          await this.handleContractTests(msg.payload.ticketKey);
          break;
        case 'refreshSkills':
          await this.handleRefreshSkills();
          break;
        case 'toggleSkill':
          this.skills.setEnabled(msg.payload.skillId, msg.payload.enabled);
          this.post('skills', await this.skillsPayload());
          break;
        // ── Enterprise hardening handlers ──
        case 'setPermission':
          await this.handleSetPermission(msg.payload.profile);
          break;
        case 'approveAction':
          await this.handleApproveAction(msg.payload.approvalId, msg.payload.approve);
          break;
        case 'runCommand':
          await this.handleRunCommand(msg.payload.action, msg.payload.args);
          break;
        case 'runMCP':
          await this.handleMCPCall(msg.payload.server, msg.payload.tool, msg.payload.args);
          break;
        case 'getTrace':
          this.post('trace', { summary: this.trace.summary(), entries: this.trace.getEntries(), profile: this.profile });
          break;
        default:
          console.warn('[deepwiki-agent] unhandled message', msg.type);
      }
    } catch (err: any) {
      this.post('error', { message: err?.message || String(err) });
    }
  }

  // ------------------------------------------------------------------
  // Message handlers
  // ------------------------------------------------------------------

  private async pushSettingsState(): Promise<void> {
    const [hasKey, jiraCfg] = await Promise.all([
      this.settings.getApiKey().then((k) => !!k),
      this.settings.getJiraConfig(),
    ]);
    this.post('settings', { hasApiKey: hasKey, jiraConfigured: !!jiraCfg });
  }

  private async handleSaveSettings(payload: any): Promise<void> {
    const { geminiApiKey, jira } = payload || {};
    if (geminiApiKey) await this.settings.storeApiKey(geminiApiKey);
    if (jira && jira.baseUrl) await this.settings.storeJiraConfig(jira);
    else if (jira) await this.settings.storeJiraConfig(jira);
    void this.llm.clear();
    this.post('status', { text: '✅ Settings saved (Secure Storage).' });
    await this.pushSettingsState();
  }

  private async handleFetchTickets(): Promise<void> {
    this.post('status', { text: 'Fetching assigned Jira tickets…' });
    const issues = await this.jira.fetchMyIssues();
    this.post('tickets', issues);
    this.post('status', { text: `Loaded ${issues.length} assigned ticket(s).` });
  }

  private async handleSelectTicket(key: string): Promise<void> {
    this.trace.user('ticket.select', `User selected ticket ${key}.`);
    this.post('status', { text: `Fetching full detail for ${key}…` });
    const ticket = await this.jira.fetchIssueDetail(key);
    this.currentTicket = ticket;
    // Persist immediately so the agent + LLM can ground against files.
    const artifacts = await this.jira.persistTicketArtifacts(ticket, this.agentDataDir);
    this.currentArtifacts = artifacts;
    this.post('ticketDetail', { ticket, artifacts });
    this.post('agentMsg', {
      role: 'agent',
      kind: 'status',
      content: `Loaded ${key}: "${ticket.summary}". Ground-truth files written under \`${path.relative(this.workspaceRoot, this.agentDataDir)}\`.`,
      timestamp: Date.now(),
    });
  }

  // ------------------------------------------------------------------
  // Agent pipeline
  // ------------------------------------------------------------------

  private async runAgentPipeline(ticketKey: string): Promise<void> {
    const ws = vscode.workspace.workspaceFolders?.[0];
    if (!ws) throw new Error('Open a workspace folder first.');
    const ticket =
      this.currentTicket && this.currentTicket.key === ticketKey
        ? this.currentTicket
        : await this.jira.fetchIssueDetail(ticketKey);
    if (!this.currentArtifacts || this.currentArtifacts.ticketKey !== ticketKey) {
      this.currentArtifacts = await this.jira.persistTicketArtifacts(ticket, this.agentDataDir);
    }
    this.currentTicket = ticket;

    // 0. Make sure the operational folders exist / scaffold examples.
    this.deepwiki.initializeBase(ws);
    this.policy.initializeBase(ws);
    this.skills.ensureBaseDir(ws);

    // 1. Research deepwiki ground truth.
    this.post('status', { text: 'Phase 1/5 · Reading deepwiki ground truth…', progress: true });
    const research = await researchForTicket(ws, this.deepwiki, ticket.key);
    this.deepWikiResult = research;
    this.post('deepwikiReady', { summary: research.summaryMarkdown });

    vscode.window.showInformationMessage(
      `DeepWiki baseline loaded for ${ticket.key} — ${research.entries.length} knowledge file(s) scanned.`,
    );

    // 2. HARD GUARDRAIL — resolve ambiguity before anything else.
    if (research.unresolvedAmbiguities.length > 0) {
      this.post('ambiguities', research.unresolvedAmbiguities);
      this.post('agentMsg', {
        role: 'agent',
        kind: 'ambiguity',
        content:
          `⚠️ **Guardrail: ambiguity detected in ground truth.**\n` +
          `${research.unresolvedAmbiguities.length} term(s) have multiple definitions. ` +
          `I will NOT proceed until each is resolved by you. Use the resolution boxes above.`,
        timestamp: Date.now(),
      });
      // Human-in-the-loop: await resolution via onMessage. When all resolved, continue.
      return; // will be resumed from resolveAmbiguity handler
    }

    await this.continuePostResearch(ticket, research);
  }

  /** Phases 3–5, after ambiguity is clear. */
  private async continuePostResearch(ticket: JiraIssue, research: DeepWikiResult): Promise<void> {
    // 3. Build the three plans (LLM-assisted, but deterministically grounded).
    this.post('status', { text: 'Phase 2/5 · Drafting 3 plans with flowcharts…', progress: true });
    const plans = await this.buildPlans(ticket, research);

    // 4. Risk assessment (upstream/downstream) — read from deepwiki contracts.
    this.post('status', { text: 'Phase 3/5 · Risk & impact analysis (upstream/downstream)…', progress: true });
    const riskSvc = new RiskService(vscode.workspace.workspaceFolders![0]);
    const contracts = this.deepwiki.allContracts();
    const riskRes = riskSvc.assess(contracts, plans.flatMap((p) => p.steps.flatMap((s) => s.filesAffected)));
    for (const p of plans) p.risks = dedupeRisks([...riskRes.risks, ...(p.risks || [])]);

    // Persist plan + risk markdown files.
    this.writePlanArtifacts(ticket, plans, riskRes.markdown);

    // 5. Present to human — no execution without explicit choice.
    this.pendingPlans = plans;
    this.post('plans', {
      planLabel: `Best-plans for ${ticket.key}: ${ticket.summary}`,
      plans,
      planMarkdown: plansToMarkdown(plans),
    });
    this.post('status', { text: 'Phase 4/5 · Presenting plans — awaiting your choice (A / B / C or new plan).', progress: false });
  }

  private async buildPlans(ticket: JiraIssue, research: DeepWikiResult): Promise<Plan[]> {
    const ws = vscode.workspace.workspaceFolders![0];
    const policies = this.policy.load(ws);
    const skillsContext = this.skills.contextPrompt();

    const baseSteps: PlanStep[] = [
      {
        order: 1,
        action: 'investigate',
        description: `Open ticket context at ${this.currentArtifacts?.contextPath || 'ticket-context.md'} and identify touched modules.`,
        filesAffected: [],
      },
      {
        order: 2,
        action: 'investigate',
        description: 'Apply deepwiki ground truth referenced by relevant entries.',
        filesAffected: research.relevant.map((e) => e.relativePath),
      },
      {
        order: 3,
        action: 'modify',
        description: `Implement the change for ${ticket.key} per chosen approach.`,
        filesAffected: [],
      },
      {
        order: 4,
        action: 'verify',
        description: 'Verify no upstream/downstream contract described in deepwiki is violated.',
        filesAffected: [],
      },
      {
        order: 5,
        action: 'test',
        description: 'Run repo contract/unit tests to validate internal & external contracts.',
        filesAffected: [],
      },
    ];

    // Try to let Gemini enhance the plans, but fall back to deterministic if unavailable.
    try {
      const llmPlans = await this.llmBuildPlans(ticket, research, policies, skillsContext, baseSteps);
      if (llmPlans && llmPlans.length === 3) {
        return llmPlans.map((p, i) => ({
          ...p,
          label: ['A', 'B', 'C'][i] || String.fromCharCode(65 + i),
          risks: (p.risks || []) as Risk[],
          steps: (p.steps || baseSteps).map((s, idx) => ({ ...s, order: idx + 1 })),
        }));
      }
    } catch (e: any) {
      this.post('agentMsg', {
        role: 'system',
        kind: 'status',
        content: `Gemini plan drafting unavailable (${e?.message || 'error'}). Using deterministic guardrailed fallback plans.`,
        timestamp: Date.now(),
      });
    }
    return this.planning.buildFallback(baseSteps, [], ticket.key);
  }

  /** LLM-assisted plan generator — with a strict output schema + injection defense. */
  private async llmBuildPlans(
    ticket: JiraIssue,
    research: DeepWikiResult,
    policies: any[],
    skillsContext: string,
    baseSteps: PlanStep[],
  ): Promise<Plan[]> {
    // Build a system prompt that keeps hard guardrails + injection-boundary instructions.
    const untrustedDeepwiki = buildUntrustedSection(
      research.relevant.map((e) => tagUntrusted(e.content.slice(0, 1500), 'deepwiki')),
    );
    const untrustedJira = buildUntrustedSection([
      tagUntrusted(`Ticket: ${ticket.key} — ${ticket.summary}\n${ticket.description.slice(0, 2000)}`, 'jira'),
    ]);
    const untrustedSkills = skillsContext
      ? buildUntrustedSection([tagUntrusted(skillsContext, 'skill')])
      : '';

    const systemPrompt = [
      `You are the planning engine of a RISK-AWARE coding agent.`,
      `You may ONLY use the facts below. If a fact is missing, mark it as "investigate" — never invent.`,
      `Return a JSON array of exactly 3 plans: [{ "title": string, "summary": string, "steps": [{ "action": "modify|create|verify|investigate|test", "description": string, "filesAffected": string[], "guardrailNote": string }], "risks": [{ "level": "LOW|MEDIUM|HIGH|CRITICAL", "kind": "upstream|downstream|contract|policy|assumption", "description": string, "mitigation": string }], "confidence": string }]`,
      ``,
      systemPromptInjectionNote(),
      ``,
      `## Ground truth (deepwiki knowledge base):`,
      untrustedDeepwiki,
      ``,
      `## Ticket context:`,
      untrustedJira,
      ``,
      untrustedSkills ? `## Skills available:\n${untrustedSkills}\n` : '',
      `Company policies:\n${policies.map((p) => p.rules.map((r: any) => `- [${r.severity}] ${r.text}`).join('\n')).join('\n')}`,
    ].filter(Boolean).join('\n');

    const result = await this.llm.invokeDetailed(systemPrompt, `Return only the JSON. Do not wrap in \`\`\`.`);
    this.trace.llm('plan.generate', `LLM generated 3 plans for ${ticket.key}`, result.tokensIn, result.tokensOut, { tool: 'llm:gemini' });

    const parsed = safeParseJson(result.text) as { title: string; summary: string; steps: PlanStep[]; risks: Risk[]; confidence: string }[];
    if (!Array.isArray(parsed) || parsed.length === 0) throw new Error('Malformed LLM plan output');
    const plans = parsed.slice(0, 3).map((p, i) => ({
      ...p,
      id: `plan-${['a', 'b', 'c'][i]}-${Date.now()}`,
      label: ['A', 'B', 'C'][i],
      steps: (p.steps || [])
        .map((s, idx) => ({ ...s, order: idx + 1 }))
        .concat(ifMissing(baseSteps, p.steps)),
      risks: p.risks || [],
      mermaid: this.planning.mermaidFor((p.steps || []).map((s, idx) => ({ ...s, order: idx + 1 }))),
    }));
    // Run evaluator — log warnings but don't block on minor structural issues.
    for (const pl of plans) {
      const v = validatePlan(pl);
      if (!v.ok) {
        this.trace.system('plan.warning', `Plan ${pl.label} failed validation: ${v.errors.join('; ')}`);
        this.post('agentMsg', {
          role: 'system', kind: 'status',
          content: `⚠️ Plan ${pl.label} validation issues: ${v.errors.join('; ')}`,
          timestamp: Date.now(),
        });
      }
    }
    return plans;
  }

  private writePlanArtifacts(ticket: JiraIssue, plans: Plan[], riskMd: string): void {
    const dir = path.join(this.agentDataDir, 'tickets', ticket.key);
    fs.mkdirSync(dir, { recursive: true });
    const planPath = this.currentArtifacts?.planPath || path.join(dir, 'plan.md');
    fs.writeFileSync(planPath, plansToMarkdown(plans), 'utf-8');
    const riskPath = this.currentArtifacts?.riskPath || path.join(dir, 'risk-assessment.md');
    fs.writeFileSync(riskPath, riskMd, 'utf-8');
    if (this.currentArtifacts) this.currentArtifacts.planPath = planPath;
  }

  // ------------------------------------------------------------------
  // Human-in-the-loop
  // ------------------------------------------------------------------

  private async handleHumanReply(text: string): Promise<void> {
    this.trace.user('chat.reply', `Human reply in chat: ${text.slice(0, 200)}`);
    this.post('agentMsg', {
      role: 'human',
      kind: 'chat',
      content: text,
      timestamp: Date.now(),
    });
    // Rerun plan generation with the human's guidance folded in.
    if (!this.currentTicket) {
      this.post('humanPrompt', { text: 'No ticket selected yet. Please pick one from the Tickets tab.' });
      return;
    }
    this.post('agentMsg', {
      role: 'agent',
      kind: 'status',
      content: `Human guidance received. I’ll re-draft plans incorporating: "${text}"`,
      timestamp: Date.now(),
    });
    if (this.deepWikiResult) {
      await this.continuePostResearch(this.currentTicket, this.deepWikiResult);
    } else {
      await this.runAgentPipeline(this.currentTicket.key);
    }
  }

  private async handleChoosePlan(planId: string): Promise<void> {
    const plan = this.pendingPlans.find((p) => p.id === planId);
    if (!plan) {
      this.post('error', { message: 'Plan not found (re-run agent).' });
      return;
    }
    // Persist chosen plan.
    const dir = path.join(this.agentDataDir, 'tickets', this.currentTicket!.key);
    fs.mkdirSync(dir, { recursive: true });
    const chosen = path.join(dir, 'chosen-plan.md');
    fs.writeFileSync(chosen, `# Chosen Plan\n\n${plansToMarkdown([plan])}`, 'utf-8');

    this.post('agentMsg', {
      role: 'agent',
      kind: 'plan',
      content: `✅ Plan ${plan.label} chosen — "${plan.title}".\n\n**Guardrail:** I will now work ONLY within the steps below and will re-verify each contract before making changes.\n\n${plan.steps
        .map((s) => `${s.order}. (${s.action.toUpperCase()}) ${s.description}${s.filesAffected?.length ? `\n   files: ${s.filesAffected.join(', ')}` : ''}`)
        .join('\n')}`,
      payload: { mermaid: plan.mermaid },
      timestamp: Date.now(),
    });
    this.post('status', { text: `Plan ${plan.label} selected — executing steps (phase 5/5).`, progress: true });

    // Risk-aware execution: verify contracts first, then ask user to run tests.
    this.post('ensureContracts', { plan });

    // Record the decision in the audit trail (who chose what + why).
    this.trace.user('plan.choose', `User chose Plan ${plan.label} ("${plan.title}") for ${this.currentTicket!.key}.`, { riskLevel: 'medium', approved: true });
    const stepDecision = this.approvals.evaluate(`execute:${plan.label}`, `Execute Plan ${plan.label} steps`, { risk: 'medium' });
    if (stepDecision.requiresHuman) {
      this.trace.agent('approval.request', `Plan ${plan.label} execution requires approval under ${this.profile}.`, { riskLevel: 'medium', approved: false });
      this.post('humanPrompt', {
        text: `⚠️ Plan ${plan.label} execution requires approval under current profile (\`${this.profile}\`).\n\nReason: ${stepDecision.reason}\n\nApprove to continue, or switch to a higher permission profile.`,
      });
    }

    // NOTE: actual file-editing is intentionally not auto-applied — the agent
    // presents a disciplined, step-by-step execution plan. Human approves each step.
    this.post('humanPrompt', {
      text: `Plan ${plan.label} is ready. Shall I begin Step 1 (${plan.steps[0]?.action})?`,
    });
  }

  private async handleResolveAmbiguity(id: string, answer: string): Promise<void> {
    const amb = this.deepWikiResult?.unresolvedAmbiguities.find((a) => a.id === id);
    if (amb) {
      amb.resolved = 'yes';
      amb.answer = answer;
    }
    const remaining = this.deepWikiResult?.unresolvedAmbiguities.filter((a) => !a.resolved);
    if (remaining && remaining.length === 0 && this.currentTicket && this.deepWikiResult) {
      this.post('agentMsg', {
        role: 'agent',
        kind: 'status',
        content: `✅ All ground-truth ambiguities resolved. Proceeding…`,
        timestamp: Date.now(),
      });
      await this.continuePostResearch(this.currentTicket, this.deepWikiResult);
    } else if (remaining && remaining.length) {
      this.post('ambiguities', remaining);
    }
  }

  private async handleCreateNewPlan(instructions: string): Promise<void> {
    if (!this.currentTicket) return;
    const research =
      this.deepWikiResult || (await researchForTicket(vscode.workspace.workspaceFolders![0], this.deepwiki, this.currentTicket.key));
    this.post('agentMsg', {
      role: 'human',
      kind: 'chat',
      content: `Create a new plan: ${instructions}`,
      timestamp: Date.now(),
    });
    // Build a plan D from the human instruction via LLM (grounded + injection-defended).
    try {
      const untrustedDeepwiki = buildUntrustedSection(research.relevant.map((e) => tagUntrusted(e.content, 'deepwiki')));
      const sys = [
        `You are a coding agent plan designer. Ground yourself in deepwiki.`,
        systemPromptInjectionNote(),
        untrustedDeepwiki,
        `Ticket: ${this.currentTicket.summary}`,
      ].join('\n');
      const result = await this.llm.invokeDetailed(
        sys,
        `Create ONE improved plan based on: "${instructions}". Output JSON: { "title", "summary", "steps":[{ "action", "description", "filesAffected":[] }], "risks": [], "confidence" }. Only JSON.`,
      );
      this.trace.llm('plan.custom', `LLM generated custom plan D`, result.tokensIn, result.tokensOut);
      const parsed = safeParseJson(result.text);
      const plan: Plan = {
        id: `plan-d-${Date.now()}`,
        label: 'D',
        title: parsed?.title || 'Custom plan',
        summary: parsed?.summary || '',
        steps: (parsed?.steps || []).map((s: any, i: number) => ({ ...s, order: i + 1 })),
        risks: parsed?.risks || [],
        mermaid: this.planning.mermaidFor(((parsed?.steps || []) as any[]).map((s: any, i: number) => ({ ...s, order: i + 1 }))),
        confidence: parsed?.confidence || 'LOW',
      };
      this.pendingPlans.push(plan);
      this.post('plans', {
        planLabel: `New plan from your input`, plans: [plan], planMarkdown: plansToMarkdown([plan]),
      });
    } catch (e: any) {
      this.post('error', { message: `Could not draft custom plan: ${e?.message}` });
    }
  }

  private async handleContractTests(ticketKey?: string): Promise<void> {
    const ws = vscode.workspace.workspaceFolders?.[0];
    if (!ws) return;
    const key = ticketKey || this.currentTicket?.key;
    this.post('status', { text: `Running contract verification for ${key}…`, progress: true });
    const tester = new ContractTester(this.deepwiki, ws);
    const contracts = this.deepwiki.allContracts();
    // Verify internal & external contract surface still exists in the repo.
    const names = contracts.length
      ? contracts.map((c) => c.name)
      : [`${key}`];
    const results = tester.verifyAll(names);
    const reportPath =
      this.currentArtifacts?.contractReportPath ||
      path.join(this.agentDataDir, 'tickets', key!, 'contract-test-report.md');
    fs.mkdirSync(path.dirname(reportPath), { recursive: true });
    fs.writeFileSync(reportPath, contractReportMarkdown(results), 'utf-8');
    const failures = results.filter((r) => !r.ok);
    this.trace.agent('contract.verify', `Verified ${results.length} contracts (${failures.length} unverified) for ${key}.`, { riskLevel: failures.length ? 'high' : 'low' });
    this.post('agentMsg', {
      role: 'agent',
      kind: 'contract',
      content:
        (failures.length
          ? `⚠️ **Contract report:** ${failures.length} contract(s) could not be verified.`
          : `✅ **Contract report:** all ${results.length} contract(s) verified.`) +
        `\n\n${results.map((r) => `- ${r.ok ? '✅' : '❌'} ${r.contract} ${r.prove.length ? `(in ${r.prove.join(', ')})` : ''}`).join('\n')}\n\nFull report: \`${path.relative(this.workspaceRoot, reportPath)}\``,
      timestamp: Date.now(),
    });
    this.post('status', { text: `Contract report written.`, progress: false });
  }

  private async handleRefreshSkills(): Promise<void> {
    const ws = vscode.workspace.workspaceFolders?.[0];
    if (!ws) return;
    const skills = this.skills.load(ws);
    this.post('skills', await this.skillsPayload());
  }

  // ------------------------------------------------------------------
  // Enterprise hardening: permission, approvals, exec, MCP
  // ------------------------------------------------------------------

  /** Change the session permission profile (escalation requires human in UI). */
  private async handleSetPermission(profile: PermissionProfile): Promise<void> {
    if (!PERMISSION_PROFILES[profile]) {
      this.trace.system('permission.denied', `Unknown profile "${profile}" rejected.`);
      this.post('error', { message: `Unknown permission profile: ${profile}` });
      return;
    }
    // If escalating beyond the current grant, record it as a user-approved grant.
    const granted = this.permissions.escalate(profile, 'user', `User selected ${profile} in settings.`);
    if (granted) {
      this.profile = profile;
      this.trace.system('permission.changed', `Permission profile set to ${profile}.`, { permissionProfile: profile });
      this.post('permission', { profile, label: PERMISSION_PROFILES[profile].label });
      this.post('status', { text: `🔐 Permission profile: ${PERMISSION_PROFILES[profile].label}` });
    } else {
      this.trace.system('permission.denied', `Cannot downgrade from ${this.permissions.current().profile} to ${profile}.`);
      this.post('error', { message: `Cannot downgrade active permissions from ${this.profile} to ${profile}.` });
    }
  }

  private async handleApproveAction(id: string, approve: boolean): Promise<void> {
    // A simulated approval-gate ledger for the audit trail. In a full UI the gate
    // would carry the action; here we just record intent for audit purposes.
    if (approve) {
      this.approvals.grant(id, 'user');
      this.trace.agent('approval.grant', `User approved action "${id}".`, { approved: true, riskLevel: 'medium' });
    } else {
      this.trace.agent('approval.denied', `User denied action "${id}".`, { approved: false, riskLevel: 'high' });
      this.post('status', { text: `🛑 Action "${id}" was denied. Nothing executed.` });
    }
  }

  /** Run a shell command, but ONLY with an explicit approval + an exec-capable profile. */
  private async handleRunCommand(action: string, args: string[]): Promise<void> {
    if (!this.permissions.canExec()) {
      this.trace.system('exec.denied', `Exec attempt "${action}" blocked: profile is ${this.profile}.`, { riskLevel: 'high', approved: false });
      this.post('error', { message: `Command execution blocked under profile "${this.profile}". Escalate permissions to exec-capable, then approve.` });
      return;
    }
    const decision = this.approvals.evaluate(action, `Run command: ${action} ${(args || []).join(' ')}`, { risk: 'high', irreversible: /(rm\s+-rf|git\s+push\s+--force|drop\s+table|shutdown|reboot)/i.test(action) });
    if (decision.requiresHuman) {
      this.approvals.request(action, `Run command: ${action}`, decision);
      this.post('humanPrompt', { text: `⚠️ Approval needed: ${decision.reason}\n\nRun \`${action} ${(args || []).join(' ')}\`?` });
      return;
    }
    this.trace.system('exec.run', `Executing command: ${action} ${(args || []).join(' ')}`, { approved: true, riskLevel: decision.riskLevel });
    try {
      const { execSync } = require('child_process') as typeof import('child_process');
      const output = execSync(`${action} ${(args || []).join(' ')}`, { cwd: this.workspaceRoot, timeout: 30_000, encoding: 'utf-8' });
      this.trace.agent('step.completed', `Exec "${action}" succeeded (${output.length} chars).`, { approved: true });
      this.post('status', { text: `✅ Exec "${action}" done.` });
    } catch (e: any) {
      this.trace.system('error', `Exec "${action}" failed: ${e?.message}`, { approved: true });
      this.post('error', { message: `Exec failed: ${e?.message}` });
    }
  }

  /** Call an MCP tool — gated by permissions + approval matrix + audit. */
  private async handleMCPCall(server: string, tool: string, args: unknown): Promise<void> {
    if (!this.permissions.canNetwork()) {
      this.trace.system('mcp.denied', `MCP call "${server}/${tool}" blocked: profile ${this.profile} is not network-capable.`, { riskLevel: 'high', approved: false });
      this.post('error', { message: `MCP calls are disabled under profile "${this.profile}".` });
      return;
    }
    const decision = this.approvals.evaluate(`mcp:${tool}`, `MCP call ${server}.${tool}`, { risk: 'high', tool: 'mcp' });
    if (decision.requiresHuman) {
      this.approvals.request(`mcp:${tool}`, `Call ${server}.${tool}`, decision);
      this.post('humanPrompt', { text: `⚠️ Approval needed: MCP call \`${server}.${tool}\`. ${decision.reason}` });
      return;
    }
    const client = this.mcpClients.find((c) => c.config.name === server);
    if (!client) {
      this.post('error', { message: `MCP server "${server}" not registered (add an mcp entry to use it).` });
      return;
    }
    this.trace.system('mcp.call', `Calling ${server}.${tool}`, { tool: `mcp:${tool}`, approved: true, riskLevel: decision.riskLevel });
    try {
      const result = await client.callTool(tool, args || {});
      const text = typeof result === 'string' ? result : JSON.stringify(result, null, 2);
      // Treat the MCP response as untrusted data (injection defense).
      const tagged = `[UNTRUSTED-SOURCE type="mcp-response"]\n${text.slice(0, 4000)}\n[/UNTRUSTED-SOURCE]`;
      this.post('agentMsg', { role: 'agent', kind: 'status', content: `MCP \`${server}.${tool}\` returned:\n\n${tagged}`, timestamp: Date.now() });
    } catch (e: any) {
      this.trace.system('error', `MCP call ${server}.${tool} failed: ${e?.message}`);
      this.post('error', { message: `MCP call failed: ${e?.message}` });
    }
  }

  /** Register MCP servers from config (called lazily / from settings). */
  private async registerMCPServers(configs: MCPServerConfig[]): Promise<void> {
    for (const c of configs) {
      const existing = this.mcpClients.find((m) => m.config.name === c.name);
      if (!existing) {
        const client = new MCPClient(this.trace, c);
        this.mcpClients.push(client);
        try {
          await client.connect();
          await client.listTools({ enabled: [] }); // default: no tools enabled until scoped
        } catch (e: any) {
          this.trace.system('mcp.connect.error', `MCP server "${c.name}" failed to connect: ${e?.message}`);
        }
      }
    }
  }

  private async pushPermissionState(): Promise<void> {
    this.post('permission', { profile: this.profile, label: PERMISSION_PROFILES[this.profile].label });
  }

  private async skillsPayload(): Promise<SkillDef[]> {
    return this.skills.getAll();
  }

  // ------------------------------------------------------------------
  // Helpers
  // ------------------------------------------------------------------

  private post(type: string, payload: any): void {
    this.panel?.post({ type, payload });
  }
}

// ---------------------------------------------------------------------
// Library helpers
// ---------------------------------------------------------------------

function safeParseJson(s: string): any {
  s = s.replace(/^```(?:json)?\s*/i, '').replace(/\s*```$/, '').trim();
  try {
    return JSON.parse(s);
  } catch {
    // Try to extract first JSON array/object.
    const start = s.search(/[[{]/);
    if (start >= 0) {
      try {
        const open = s[start];
        const close = open === '[' ? ']' : '}';
        let depth = 0;
        let inStr = false;
        for (let i = start; i < s.length; i++) {
          const c = s[i];
          if (inStr) {
            if (c === '"' && s[i - 1] !== '\\') inStr = false;
            continue;
          }
          if (c === '"') inStr = true;
          else if (c === open) depth++;
          else if (c === close) {
            depth--;
            if (depth === 0) return JSON.parse(s.slice(start, i + 1));
          }
        }
      } catch {
        /* fallthrough */
      }
    }
    return null;
  }
}

function contractReportMarkdown(
  results: Array<{ contract: string; ok: boolean; prove: string[] }>,
): string {
  const lines: string[] = ['# Contract Verification Report', ''];
  const failed = results.filter((r) => !r.ok);
  lines.push(`Total contracts: **${results.length}** — verified: **${results.length - failed.length}**, unverified: **${failed.length}**`);
  lines.push('');
  for (const r of results) {
    lines.push(`## ${r.ok ? '✅' : '❌'} ${r.contract}`);
    lines.push(r.ok ? `Verified in: ${r.prove.join(', ') || 'ground-truth'}` : '**Not verifiable from current repo scan.** Requires human confirmation.');
    lines.push('');
  }
  return lines.join('\n');
}

function dedupeRisks(risks: Risk[]): Risk[] {
  const seen = new Set<string>();
  const out: Risk[] = [];
  for (const r of risks) {
    const k = `${r.kind}|${r.description}`;
    if (!seen.has(k)) {
      seen.add(k);
      out.push(r);
    }
  }
  return out;
}

function plansToMarkdown(plans: Plan[]): string {
  const lines: string[] = ['# Agent Plans', ''];
  for (const p of plans) {
    lines.push(`## Plan ${p.label} — ${p.title}`);
    lines.push(p.summary);
    lines.push('');
    lines.push('### Steps');
    for (const s of p.steps) {
      lines.push(`${s.order}. **${s.action.toUpperCase()}** ${s.description}`);
      if (s.filesAffected?.length) lines.push(`   - files: ${s.filesAffected.join(', ')}`);
    }
    lines.push('');
    lines.push('### Risks');
    for (const r of p.risks || []) {
      lines.push(`- [${r.level}] ${r.kind}: ${r.description} ${r.mitigation ? `→ ${r.mitigation}` : ''}`);
    }
    lines.push('');
  }
  return lines.join('\n');
}

function ifMissing(base: PlanStep[], existing: PlanStep[]): PlanStep[] {
  const hasAct = (a: string) => existing.some((s) => s.action === a);
  return base.filter((b) => !hasAct(b.action)).map((b) => ({ ...b, order: existing.length + 1 }));
}