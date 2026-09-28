import * as vscode from 'vscode';
import * as fs from 'fs';
import * as path from 'path';
import { SkillDef } from './types';

/**
 * Skills/Tools extension system.
 *
 * Skills are `.md` or `.js` files in `<workspace>/skills/` with metadata front-matter.
 * The agent can load + evaluate them as additional context or runtime tools.
 *
 * A skill file contains:
 * ```markdown
 * ---
 * id: my-skill
 * name: My Skill
 * description: What this skill does.
 * entryPoint: ./my-skill.js
 * ---
 * ```
 */
export class SkillsService {
  private skills: SkillDef[] = [];

  baseDir(ws: vscode.WorkspaceFolder): string {
    return path.join(ws.uri.fsPath, 'skills');
  }

  ensureBaseDir(ws: vscode.WorkspaceFolder): string {
    const dir = this.baseDir(ws);
    if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
    return dir;
  }

  load(ws: vscode.WorkspaceFolder): SkillDef[] {
    const dir = this.ensureBaseDir(ws);
    try {
      const files = fs.readdirSync(dir).filter((f) => f.endsWith('.md') || f.endsWith('.js'));
      this.skills = files.map((f) => {
        const content = fs.readFileSync(path.join(dir, f), 'utf-8');
        const id = this.extractMeta(content, 'id') || f.replace(/\.[^.]+$/, '');
        const name = this.extractMeta(content, 'name') || id;
        const description = this.extractMeta(content, 'description') || '';
        const ep = this.extractMeta(content, 'entryPoint') || '';
        return {
          id,
          name,
          description,
          entryPoint: ep ? path.resolve(dir, ep) : path.join(dir, f),
          enabled: true,
        };
      });
    } catch (e) {
      this.skills = [];
    }
    return this.skills;
  }

  private extractMeta(content: string, field: string): string | undefined {
    const m = content.match(new RegExp(`^${field}:\\s*(.+)`, 'm'));
    return m ? m[1].trim() : undefined;
  }

  getAll(): SkillDef[] {
    return this.skills;
  }

  getEnabled(): SkillDef[] {
    return this.skills.filter((s) => s.enabled);
  }

  setEnabled(id: string, v: boolean): void {
    const s = this.skills.find((sk) => sk.id === id);
    if (s) s.enabled = v;
  }

  /** Build a system prompt segment embedding skill context. */
  contextPrompt(): string {
    const en = this.getEnabled();
    if (!en.length) return '';
    return (
      '## Available skills\n' +
      en
        .map((s) => {
          const content = fs.existsSync(s.entryPoint)
            ? fs.readFileSync(s.entryPoint, 'utf-8').slice(0, 1000)
            : '';
          return `### ${s.name}\n${s.description}\n\`\`\`\n${content}\n\`\`\``;
        })
        .join('\n\n')
    );
  }
}