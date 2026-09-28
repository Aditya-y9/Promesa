import * as vscode from 'vscode';
import { JiraIssue, JiraConfig, TicketArtifacts } from './types';
import { SettingsService } from './settings';
import * as fs from 'fs';
import * as path from 'path';

/**
 * Jira REST API v3 client.
 * Uses basic auth (email + API token) for on-prem / cloud Jira.
 */
export class JiraService {
  private config: JiraConfig | undefined;

  constructor(private settings: SettingsService) {}

  private async ensureConfig(): Promise<JiraConfig> {
    if (!this.config) {
      this.config = await this.settings.getJiraConfig();
    }
    if (!this.config?.baseUrl) {
      throw new Error('Jira not configured. Provide credentials in the DeepWiki Agent settings tab.');
    }
    return this.config;
  }

  private base64Auth(cfg: JiraConfig): string {
    return Buffer.from(`${cfg.username}:${cfg.apiToken}`).toString('base64');
  }

  private async request<T>(endpoint: string, params?: URLSearchParams): Promise<T> {
    const cfg = await this.ensureConfig();
    const ws = vscode.workspace.workspaceFolders?.[0];
    const proxy = vscode.workspace.getConfiguration('http.proxy', ws?.uri);
    const proxyUrl = proxy.get<string>('proxy') || process.env.HTTPS_PROXY || process.env.HTTP_PROXY;

    const url = new URL(`${cfg.baseUrl.replace(/\/$/, '')}/rest/agile/1.0${endpoint}`);
    if (params) {
      url.search = params.toString();
    }

    const resp = await fetch(url.toString(), {
      method: 'GET',
      headers: {
        Authorization: `Basic ${this.base64Auth(cfg)}`,
        Accept: 'application/json',
      },
      // Proxy support — Node 18+ fetch doesn't use env automatically
      ...(proxyUrl ? { proxy: proxyUrl } : {}),
    });

    if (!resp.ok) {
      const body = await resp.text().catch(() => '');
      throw new Error(`Jira API error ${resp.status}: ${resp.statusText}. ${body}`);
    }
    return resp.json() as Promise<T>;
  }

  /** Fetch issues assigned to the configured user. */
  async fetchMyIssues(assignee?: string): Promise<JiraIssue[]> {
    const cfg = await this.ensureConfig();
    const user = assignee || cfg.username;
    const jql =
      cfg.jql || `assignee = "${user}" AND resolution = unresolved ORDER BY priority DESC, updated DESC`;

    const params = new URLSearchParams({
      jql,
      maxResults: '50',
      fields: 'summary,description,status,assignee,priority,issuetype,project,labels,comment,updated,created',
    });

    const result = (await this.request<{ issues: unknown[] }>('/search', params)) as {
      issues: any[];
    };

    return result.issues.map((issue: any) => {
      const f = issue.fields || {};
      return {
        id: issue.id,
        key: issue.key,
        self: issue.self,
        summary: f.summary || '(no summary)',
        description: f.description || '',
        status: f.status?.name || 'Unknown',
        assignee: f.assignee?.displayName || f.assignee?.emailAddress || 'Unassigned',
        priority: f.priority?.name || 'None',
        issuetype: f.issuetype?.name || 'Task',
        project: f.project?.name || '',
        labels: f.labels || [],
        fields: f,
        raw: issue,
      } as JiraIssue;
    });
  }

  /** Fetch a single issue in full detail. */
  async fetchIssueDetail(key: string): Promise<JiraIssue> {
    const result = (await this.request<any>(`/issue/${key}`, new URLSearchParams({
      fields: '*all',
    }))) as any;

    const f = result.fields || {};
    return {
      id: result.id,
      key: result.key,
      self: result.self,
      summary: f.summary || '',
      description: f.description || '',
      status: f.status?.name || 'Unknown',
      assignee: f.assignee?.displayName || f.assignee?.emailAddress || 'Unassigned',
      priority: f.priority?.name || 'None',
      issuetype: f.issuetype?.name || 'Task',
      project: f.project?.name || '',
      labels: f.labels || [],
      fields: f,
      raw: result,
    };
  }

  /** Persist ticket to local files for LLM consumption and tracking. */
  async persistTicketArtifacts(ticket: JiraIssue, basePath: string): Promise<TicketArtifacts> {
    const dir = path.join(basePath, 'tickets', ticket.key);
    fs.mkdirSync(dir, { recursive: true });

    // 1) Raw Jira JSON
    const jsonPath = path.join(dir, 'jira-payload.json');
    fs.writeFileSync(jsonPath, JSON.stringify(ticket.raw, null, 2), 'utf-8');

    // 2) LLM-context markdown — structured, unambiguous description
    const contextMd = this.buildTicketContext(ticket);
    const contextPath = path.join(dir, 'ticket-context.md');
    fs.writeFileSync(contextPath, contextMd, 'utf-8');

    // 3) Empty plan file (will be filled by agent)
    const planPath = path.join(dir, 'plan.md');
    if (!fs.existsSync(planPath)) {
      fs.writeFileSync(
        planPath,
        `# Plan for ${ticket.key}\n\n**Status:** Awaiting agent analysis\n\n`,
        'utf-8',
      );
    }

    // 4) Empty risk file
    const riskPath = path.join(dir, 'risk-assessment.md');
    if (!fs.existsSync(riskPath)) {
      fs.writeFileSync(riskPath, `# Risk Assessment for ${ticket.key}\n\n**Status:** Not yet assessed\n\n`, 'utf-8');
    }

    // 5) Contract report placeholder
    const contractReportPath = path.join(dir, 'contract-test-report.md');
    if (!fs.existsSync(contractReportPath)) {
      fs.writeFileSync(
        contractReportPath,
        `# Contract Test Report for ${ticket.key}\n\n**Status:** Not yet tested\n\n`,
        'utf-8',
      );
    }

    return {
      ticketKey: ticket.key,
      jsonPath,
      contextPath,
      planPath,
      riskPath,
      contractReportPath,
    };
  }

  private buildTicketContext(ticket: JiraIssue): string {
    const lines: string[] = [];
    lines.push(`# ${ticket.key}: ${ticket.summary}`);
    lines.push('');
    lines.push(`- **Project:** ${ticket.project}`);
    lines.push(`- **Type:** ${ticket.issuetype}`);
    lines.push(`- **Status:** ${ticket.status}`);
    lines.push(`- **Priority:** ${ticket.priority}`);
    lines.push(`- **Assignee:** ${ticket.assignee}`);
    lines.push('');
    lines.push('## Description');
    lines.push('');
    // Flatten Atlassian Document Format (ADF) or plain text.
    if (typeof ticket.description === 'string') {
      lines.push(ticket.description);
    } else if (ticket.description && typeof ticket.description === 'object') {
      lines.push(flattenADF(ticket.description as any));
    } else {
      lines.push('*(No description provided)*');
    }
    lines.push('');
    lines.push('## Labels');
    lines.push(ticket.labels.length > 0 ? ticket.labels.join(', ') : 'None');
    lines.push('');
    lines.push('## Fields (key/value)');
    for (const [k, v] of Object.entries(ticket.fields)) {
      if (v !== null && v !== undefined) {
        lines.push(`- **${k}:** ${typeof v === 'object' ? JSON.stringify(v).slice(0, 500) : String(v)}`);
      }
    }
    return lines.join('\n');
  }
}

function flattenADF(node: any): string {
  if (!node) return '';
  if (typeof node === 'string') return node;
  if (Array.isArray(node.content)) {
    return node.content.map(flattenADF).join('\n');
  }
  if (node.text) return node.text;
  if (node.type === 'paragraph') return (node.content ?? []).map(flattenADF).join('') + '\n';
  if (node.type === 'bulletList' || node.type === 'orderedList')
    return (node.content ?? []).map(flattenADF).join('\n');
  if (node.type === 'listItem') return '- ' + (node.content ?? []).map(flattenADF).join('') + '\n';
  return '';
}