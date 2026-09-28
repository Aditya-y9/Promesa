// Shared types for the DeepWiki Agent extension.

// ──────────────────────────────────────────────────────────────
// Enterprise-grade additions (2026 industry best-practices)
// ──────────────────────────────────────────────────────────────

/** Permission profile for the agent session — follows least-privilege principle. */
export type PermissionProfile = 'readonly' | 'workspace-write' | 'workspace-exec' | 'elevated';

export interface PermissionProfileDef {
  name: PermissionProfile;
  label: string;
  /** Allowed file-system operations */
  allowRead: string[];     // globs for readable paths
  allowWrite: string[];    // globs for writable paths
  allowExec: boolean;      // shell command execution
  allowNetwork: boolean;   // outbound network / MCP calls
  allowWebSearch: boolean; // web search tool
}

/** A single permission grant for one session. Ephemeral; never persisted. */
export interface PermissionGrant {
  profile: PermissionProfile;
  approvedAt: number;
  approvedBy: 'user' | 'policy';
  scope: string; // workspace folder path
  expiresAt: number; // epoch ms
  reason: string;
}

/** Entry in the append-only audit trail. */
export interface AuditEntry {
  seq: number;
  timestamp: string; // ISO 8601
  sessionId: string;
  source: 'agent' | 'user' | 'system' | 'llm';
  action: string;
  detail: string;
  tool?: string;
  tokensIn?: number;
  tokensOut?: number;
  costEstimateUsd?: number;
  permissionProfile?: PermissionProfile;
  riskLevel?: RiskLevel;
  approved?: boolean;
}

/** Severity for risk-gated approvals. */
export type RiskLevel = 'info' | 'low' | 'medium' | 'high' | 'critical';

export interface ApprovalGate {
  id: string;
  riskLevel: RiskLevel;
  action: string;
  description: string;
  requiresHuman: boolean;
  resolvedAt?: number;
  resolvedBy?: 'user' | 'policy-auto';
  approved?: boolean;
}

/** Trace session metadata for observability + replay. */
export interface TraceSession {
  sessionId: string;
  startedAt: string;
  workspaceRoot: string;
  ticketKey?: string;
  permissionProfile: PermissionProfile;
  llmModel: string;
  totalTokensIn: number;
  totalTokensOut: number;
  totalCostUsd: number;
  stepsCompleted: number;
  approvalsRequested: number;
  approvalsGranted: number;
  errors: number;
}

/** Prompt-injection defense classification for untrusted content. */
export type ContentSource = 'jira' | 'repo-code' | 'deepwiki' | 'skill' | 'policy' | 'user-chat' | 'mcp-response';

export interface TaggedContent {
  source: ContentSource;
  text: string;
  /** If true, the LLM should treat instructions inside this content as data, not commands. */
  instructionBlind: boolean;
}

/** MCP tool descriptor (from resources/tools list). */
export interface MCPTool {
  serverName: string;
  toolName: string;
  description: string;
  inputSchema: unknown;
  enabled: boolean;
}

/** MCP server configuration (read from .mcp.json or .vscode/mcp.json). */
export interface MCPServerConfig {
  name: string;
  command?: string;
  args?: string[];
  url?: string;
  env?: Record<string, string>;
}

/** Result of plan/evaluate schema validation. */
export interface ValidationResult {
  ok: boolean;
  errors: string[];
  warnings: string[];
}

// ──────────────────────────────────────────────────────────────
// Existing types (unchanged)
// ──────────────────────────────────────────────────────────────

/** Jira issue types that we care about. */
export interface JiraIssue {
  id: string;
  key: string;
  self: string;
  summary: string;
  description: string;
  status: string;
  assignee?: string;
  priority?: string;
  issuetype?: string;
  project?: string;
  labels: string[];
  fields: Record<string, unknown>;
  raw: unknown;
}

/** Physical (filesystem) ticket store — files that LLMs understand. */
export interface TicketArtifacts {
  ticketKey: string;
  jsonPath?: string; // raw Jira payload
  contextPath?: string; // LLM markdown context
  planPath?: string; // generated plans markdown
  riskPath?: string; // risk assessment markdown
  contractReportPath?: string; // contract test report markdown
}

/** A single file in the deepwiki knowledge base. */
export interface KnowledgeEntry {
  path: string;
  relativePath: string;
  content: string;
  topics: string[];
  /** front-matter-ish tags parsed from the file when possible */
  definitions: Map<string, string[]>;
  conflicts: Array<{ term: string; candidates: string[] }>;
  contracts?: ContractInfo[];
}

export interface ContractInfo {
  /** e.g. "public ApiClient.login()" */
  name: string;
  direction: 'internal' | 'external';
  upstream: string[]; // things this contract depends on
  downstream: string[]; // things that depend on this contract
  verification?: string; // command / test name that verifies the contract
}

export interface Plan {
  id: string;
  label: string;
  title: string;
  summary: string;
  steps: PlanStep[];
  mermaid: string;
  risks: Risk[];
  confidence: string; // confidence after guardrail pass
}

export interface PlanStep {
  order: number;
  action: 'modify' | 'create' | 'verify' | 'investigate' | 'test' | 'ask-human';
  description: string;
  filesAffected: string[];
  guardrailNote?: string;
}

export interface Risk {
  level: 'LOW' | 'MEDIUM' | 'HIGH' | 'CRITICAL';
  kind: 'upstream' | 'downstream' | 'contract' | 'policy' | 'assumption';
  description: string;
  mitigation?: string;
  verifiedFrom: string[]; // ground-truth sources backing this risk
}

export interface Ambiguity {
  id: string;
  topic: string;
  question: string;
  candidates: string[];
  sources: string[];
  resolved?: string;
  answer?: string;
}

export interface SkillDef {
  id: string;
  name: string;
  description: string;
  entryPoint: string; // path to the skill script or SKILL.md
  enabled: boolean;
}

export interface DeepWikiResult {
  entries: KnowledgeEntry[];
  relevant: KnowledgeEntry[];
  unresolvedAmbiguities: Ambiguity[];
  summaryMarkdown: string;
}

export interface Policy {
  path: string;
  name: string;
  content: string;
  rules: PolicyRule[];
}

export interface PolicyRule {
  id: string;
  text: string;
  severity: string;
}

export interface AgentMessage {
  role: 'agent' | 'human' | 'system';
  content: string;
  kind: 'chat' | 'plan' | 'risk' | 'ambiguity' | 'contract' | 'skill' | 'status' | 'error';
  payload?: unknown;
  timestamp: number;
}

export interface JiraConfig {
  baseUrl: string;
  username: string;
  apiToken: string;
  jql?: string;
}

export type WebviewInMessage =
  | { type: 'ready' }
  | { type: 'saveSettings'; payload: { geminiApiKey: string; jira: JiraConfig } }
  | { type: 'fetchTickets' }
  | { type: 'selectTicket'; payload: { ticketKey: string } }
  | { type: 'startAgent'; payload: { ticketKey: string } }
  | { type: 'humanReply'; payload: { text: string } }
  | { type: 'choosePlan'; payload: { planId: string } }
  | { type: 'resolveAmbiguity'; payload: { ambiguityId: string; answer: string } }
  | { type: 'createNewPlan'; payload: { instructions: string } }
  | { type: 'runContractTests'; payload: { ticketKey: string } }
  | { type: 'refreshDeepwiki' }
  | { type: 'refreshSkills' }
  | { type: 'toggleSkill'; payload: { skillId: string; enabled: boolean } }
  | { type: 'setPermission'; payload: { profile: PermissionProfile } }
  | { type: 'approveAction'; payload: { approvalId: string; approve: boolean } }
  | { type: 'runCommand'; payload: { action: string; args?: string[] } }
  | { type: 'runMCP'; payload: { server: string; tool: string; args?: unknown } }
  | { type: 'getTrace' };

export type WebviewOutMessage =
  | { type: 'settings'; payload: { hasApiKey: boolean; jiraConfigured: boolean } }
  | { type: 'tickets'; payload: JiraIssue[] }
  | { type: 'ticketDetail'; payload: { ticket: JiraIssue; artifacts: TicketArtifacts } }
  | { type: 'walkthrough'; payload: { step: string; data?: unknown } }
  | { type: 'plans'; payload: { planLabel: string; plans: Plan[]; planMarkdown: string } }
  | { type: 'selectionPrompt'; payload: { question: string; options: string[]; context?: unknown } }
  | { type: 'humanPrompt'; payload: { text: string } }
  | { type: 'agentMsg'; payload: AgentMessage }
  | { type: 'ambiguities'; payload: Ambiguity[] }
  | { type: 'skills'; payload: SkillDef[] }
  | { type: 'status'; payload: { text: string; progress?: boolean } }
  | { type: 'error'; payload: { message: string } }
  | { type: 'deepwikiReady'; payload: { summary: string } }
  | { type: 'permission'; payload: { profile: PermissionProfile; label: string } }
  | { type: 'trace'; payload: { summary: TraceSession; entries: AuditEntry[]; profile: PermissionProfile } };
