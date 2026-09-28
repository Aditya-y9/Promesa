import * as vscode from 'vscode';
import { JiraConfig } from './types';

const SECRETS_PREFIX = 'deepwikiAgent.';

/** Secure storage backed by VS Code SecretStorage + workspaceState for non-secret settings. */
export class SettingsService {
  private readonly secrets: vscode.SecretStorage;
  private readonly state: vscode.Memento;

  constructor(context: vscode.ExtensionContext) {
    this.secrets = context.secrets;
    this.state = context.workspaceState;
  }

  async getApiKey(): Promise<string | undefined> {
    return this.secrets.get(`${SECRETS_PREFIX}geminiApiKey`);
  }

  async storeApiKey(key: string): Promise<void> {
    await this.secrets.store(`${SECRETS_PREFIX}geminiApiKey`, key.trim());
  }

  async getJiraConfig(): Promise<JiraConfig | undefined> {
    const jira = await this.secrets.get(`${SECRETS_PREFIX}jira`);
    if (jira) {
      try {
        return JSON.parse(jira) as JiraConfig;
      } catch {
        return undefined;
      }
    }
    return this.state.get<JiraConfig>('deepwiki.jira') as JiraConfig | undefined;
  }

  async storeJiraConfig(config: JiraConfig): Promise<void> {
    await this.secrets.store(`${SECRETS_PREFIX}jira`, JSON.stringify(config));
    await this.state.update('deepwiki.jira', config);
  }

  jiraConfigured(): Promise<boolean> {
    return this.getJiraConfig().then((c) => !!c && !!c.baseUrl && !!c.apiToken);
  }
}
