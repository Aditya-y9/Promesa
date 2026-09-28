import * as vscode from 'vscode';
import * as path from 'path';
import * as fs from 'fs';
import { AgentPanel } from './panel';
import { SettingsService } from './settings';
import { JiraService } from './jira';
import { AgentOrchestrator } from './agent';
import { SkillsService } from './skills';

let orchestrator: AgentOrchestrator | undefined;

/**
 * Activate the extension:
 *  - Registers "DeepWiki Agent: Open Dashboard".
 *  - Scaffolds `deepwiki/`, `company/`, `skills/` in the first workspace folder.
 *  - Opens the agent panel (coding-agent GUI).
 */
export function activate(context: vscode.ExtensionContext): void {
  const ws = vscode.workspace.workspaceFolders?.[0];
  if (ws) {
    void scaffoldFolders(ws);
  }

  const openDashboard = vscode.commands.registerCommand('deepwiki.openDashboard', async () => {
    const panel = AgentPanel.show(context);
    orchestrator = new AgentOrchestrator(context);
    orchestrator.attachPanel(panel);
  });

  // Skill discovery command.
  const addSkill = vscode.commands.registerCommand('deepwiki.addSkype', async () => {
    const ws0 = vscode.workspace.workspaceFolders?.[0];
    if (!ws0) return;
    const skillsDir = path.join(ws0.uri.fsPath, 'skills');
    fs.mkdirSync(skillsDir, { recursive: true });
    const name = await vscode.window.showInputBox({ prompt: 'Skill name (used as file name)', value: 'my-skill.md' });
    if (!name) return;
    const file = path.join(skillsDir, name.endsWith('.md') ? name : `${name}.md`);
    if (!fs.existsSync(file)) {
      fs.writeFileSync(
        file,
        `---\nid: ${name}\nname: ${name}\ndescription: Describe your skill.\nentryPoint: ${name}\n---\n\nSkill content here.\n`,
        'utf-8',
      );
    }
    if (orchestrator) {
      orchestrator.refreshSkillsFromDisk();
    }
    const doc = await vscode.workspace.openTextDocument(file);
    await vscode.window.showTextDocument(doc);
  });

  const showTrace = vscode.commands.registerCommand('deepwiki.showTrace', async () => {
    const panel = AgentPanel.show(context);
    if (orchestrator) {
      panel.post({ type: 'status', payload: { text: '📋 Audit trail loaded. Open the Guardrails tab.' } });
      await orchestrator.onMessage({ type: 'getTrace' });
    }
  });

  context.subscriptions.push(openDashboard, addSkill, showTrace);

  // Auto-open dashboard on activation so the user can enter the LLM key right away.
  vscode.commands.executeCommand('deepwiki.openDashboard');
}

/** Create `deepwiki/README.md`, `company/policy.yaml`, `skills` if missing. */
async function scaffoldFolders(ws: vscode.WorkspaceFolder): Promise<void> {
  const { DeepWikiService, researchForTicket } = await import('./deepwiki');
  const d = new DeepWikiService({ extensionPath: '' } as any);
  d.initializeBase(ws);
  const { CompanyPolicyService } = await import('./policy');
  new CompanyPolicyService().initializeBase(ws);
  new SkillsService().ensureBaseDir(ws);
}

export function deactivate(): void {
  orchestrator = undefined;
}