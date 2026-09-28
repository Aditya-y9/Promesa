import * as vscode from 'vscode';
import * as fs from 'fs';
import * as path from 'path';
import { Policy, PolicyRule } from './types';

/**
 * Company Policy service.
 * Reads `<workspace>/company` folder containing YAML policy files.
 * A simple YAML parser (no deps) handles the common cases:
 *   rules:
 *     - id: SEC-1
 *       severity: HIGH
 *       text: Never commit secrets.
 */
export class CompanyPolicyService {
  private policies: Policy[] = [];

  baseDir(ws: vscode.WorkspaceFolder): string {
    return path.join(ws.uri.fsPath, 'company');
  }

  ensureBaseDir(ws: vscode.WorkspaceFolder): string {
    const dir = this.baseDir(ws);
    if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
    return dir;
  }

  initializeBase(ws: vscode.WorkspaceFolder): void {
    const dir = this.ensureBaseDir(ws);
    const f = path.join(dir, 'policy.yaml');
    if (!fs.existsSync(f)) {
      fs.writeFileSync(
        f,
        `# Company policy
# The agent MUST honour these before making any plan.
#
rules:
  - id: POLICY-1
    severity: HIGH
    text: Never modify files outside the workspace.
  - id: POLICY-2
    severity: HIGH
    text: Never commit secrets or API keys into source.
  - id: POLICY-3
    severity: MEDIUM
    text: All new endpoints must come with contract tests.
`,
        'utf-8',
      );
    }
    void this.load(ws);
  }

  /** Load policies from a folder. */
  load(ws: vscode.WorkspaceFolder): Policy[] {
    const dir = this.ensureBaseDir(ws);
    const policies: Policy[] = [];
    try {
      const files = fs.readdirSync(dir).filter((f) => f.endsWith('.yaml') || f.endsWith('.yml'));
      for (const f of files) {
        const content = fs.readFileSync(path.join(dir, f), 'utf-8');
        policies.push(this.parse(content, f, path.join(dir, f)));
      }
    } catch (e) {
      // no policies folder yet — treat as empty
    }
    this.policies = policies;
    return policies;
  }

  private parse(content: string, pathStr: string, absPath: string): Policy {
    const rules: PolicyRule[] = [];
    const lines = content.split('\n');
    let curText = '';
    let curId = '';
    let curSeverity = 'MEDIUM';
    let inList = false;
    for (const ln of lines) {
      const b = ln.trim();
      const idMatcher = ln.match(/^\s*-\s*id:\s*(.+)/);
      if (idMatcher) {
        if (inList && curText) {
          rules.push({ id: curId || 'RULE-' + rules.length, text: curText, severity: curSeverity });
        }
        curId = idMatcher[1].trim();
        curText = '';
        curSeverity = 'MEDIUM';
        inList = true;
      } else {
        const textMatcher = ln.match(/^\s*text:\s*(.+)/);
        if (textMatcher) curText = textMatcher[1].trim();
        const sevMatcher = ln.match(/^\s*severity:\s*(.+)/);
        if (sevMatcher) curSeverity = sevMatcher[1].trim();
      }
    }
    if (inList && curText) {
      rules.push({ id: curId || 'RULE-' + rules.length, text: curText, severity: curSeverity });
    }
    return {
      path: absPath,
      name: pathStr,
      content,
      rules,
    };
  }

  /** Return rules sorted by severity to make guardrails deterministic. */
  orderedRules(): PolicyRule[] {
    const rank = (s: string) => (s.toUpperCase().startsWith('CRIT') ? 0 : s.toUpperCase().startsWith('HIGH') ? 1 : s.toUpperCase().startsWith('MED') ? 2 : 3);
    return [...this.policies.flatMap((p) => p.rules)].sort((a, b) => rank(a.severity) - rank(b.severity));
  }

  /** Deterministic violation check on a plan text. Returns rule IDs that match. */
  audit(planText: string, contents?: string[]): PolicyRule[] {
    const violations: PolicyRule[] = [];
    for (const r of this.orderedRules()) {
      const keywords = r.text.split(/\s+/).filter((t) => t.length > 3);
      for (const k of keywords) {
        if (planText && planText.toLowerCase().includes(k.toLowerCase())) {
          violations.push(r);
          break;
        }
      }
    }
    return [...new Set(violations)];
  }
}