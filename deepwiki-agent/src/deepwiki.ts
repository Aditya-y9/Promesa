import * as vscode from 'vscode';
import * as fs from 'fs';
import * as path from 'path';
import * as crypto from 'crypto';
import { Ambiguity, ContractInfo, DeepWikiResult, KnowledgeEntry } from './types';
import { resolveWithin } from './guardrail';

/**
 * DeepWiki knowledge system.
 *
 * This is the agent's reference architecture:
 * - Scans `<workspace>/deepwiki` for md/md files.
 * - Builds an index of topics, definitions and contracts.
 * - Deliberately surfaces *conflicts/ambiguities* rather than guessing.
 * - Provides deterministic lookups so the agent can ground claims.
 */
export class DeepWikiService {
  private cache = new Map<string, string>(); // relpath -> mtime:size
  private index: KnowledgeEntry[] = [];
  private initialized = false;

  constructor(private context: vscode.ExtensionContext) {}

  baseDir(ws: vscode.WorkspaceFolder): string {
    return path.join(ws.uri.fsPath, 'deepwiki');
  }

  ensureBaseDir(ws: vscode.WorkspaceFolder): string {
    const dir = this.baseDir(ws);
    if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
    return dir;
  }

  /** Create a README with a trust & ground-truth contract. */
  initializeBase(ws: vscode.WorkspaceFolder): void {
    const dir = this.ensureBaseDir(ws);
    const readme = path.join(dir, 'README.md');
    if (!fs.existsSync(readme)) {
      fs.writeFileSync(
        readme,
        `# DeepWiki — Ground Truth Knowledge Base

This folder is the **single source of truth** for this codebase used by the DeepWiki Agent.

## Rules for writing here

1. **No assumptions** — everything here must be verifiable from the codebase.
2. **Definitions** — use \`## Definitions\` sections with \`### term\` blocks.
3. **Contracts** — use \`## Contracts\` sections with explicit \`Upstream:\`, \`Downstream:\` and \`Validate:\` lines.
4. **Dependencies** — use \`## Dependencies\` sections.
5. If the same term is defined more than once across files, it is an **ambiguity** and the agent must ask the human — never guess.
6. Keep entries deterministic and small — bullets not prose walls.

## Conventions
- \`## Definitions\` → each term is a \`### term-name\` heading.
- \`## Contracts\` → contract blocks with markdown bullets.
- \`## Dependencies\` → one bullet per dependency with \`ref:\` pointers.
- \`## Risks\` → optional; the agent enriches this after analysis.
`,
        'utf-8',
      );
    }
  }

  /** Rescan the deepwiki folder, detecting conflicts. */
  scan(ws: vscode.WorkspaceFolder): KnowledgeEntry[] {
    const dir = this.ensureBaseDir(ws);
    if (!fs.existsSync(dir)) return [];
    const entries = this.walk(dir);
    this.index = entries;
    this.initialized = true;
    return entries;
  }

  private walk(dir: string): KnowledgeEntry[] {
    const out: KnowledgeEntry[] = [];
    let files: string[] = [];
    try {
      files = fs.readdirSync(dir);
    } catch {
      return out;
    }
    for (const f of files) {
      const full = path.join(dir, f);
      let stat;
      try {
        stat = fs.statSync(full);
      } catch {
        continue;
      }
      if (stat.isDirectory()) {
        out.push(...this.walk(full));
      } else if (f.endsWith('.md')) {
        const content = fs.readFileSync(full, 'utf-8');
        const rel = path.relative(dir, full).split(path.sep).join('/');
        out.push(this.parseFile(content, rel, f));
      }
    }
    return out;
  }

  private parseFile(content: string, rel: string, fname: string): KnowledgeEntry {
    const lines = content.split('\n');
    const definitions = new Map<string, string[]>();
    const contracts: ContractInfo[] = [];
    let curDef = '';
    for (const ln of lines) {
      // Definitions
      const defMatch = ln.match(/^###\s+(.+)/);
      if (defMatch) {
        const term = defMatch[1].trim().toLowerCase();
        if (!definitions.has(term)) definitions.set(term, []);
        curDef = term;
      } else if (curDef && ln.trim()) {
        definitions.get(curDef)?.push(ln.trim());
      }
      // Contracts
      const conMatch = ln.match(/^###\s+Contract:(.+)/i);
      if (conMatch) {
        contracts.push({
          name: conMatch[1].trim(),
          direction: 'internal',
          upstream: [],
          downstream: [],
        });
      }
    }
    // pick up contract bullets
    if (contracts.length > 0) {
      // Try to contract-scope upstream/downstream within contract blocks.
      const blocks = content.split(/^###\s+Contract:/m);
      for (let i = 1; i < blocks.length; i++) {
        const b = blocks[i];
        contracts[i - 1] = {
          ...contracts[i - 1],
          name: contracts[i - 1].name || b.split('\n')[0].trim(),
          upstream: [...b.matchAll(/Upstream:\s*(.+)/g)].map((m) => m[1]),
          downstream: [...b.matchAll(/Downstream:\s*(.+)/g)].map((m) => m[1]),
          verification: [...b.matchAll(/Validate:\s*(.+)/g)].map((m) => m[1])[0],
        };
      }
    }

    // detect per-file conflicts (a term with 2 definitions lines)
    const conflicts: Array<{ term: string; candidates: string[] }> = [];
    for (const [term, linesArr] of definitions) {
      if (linesArr.length > 1) conflicts.push({ term, candidates: linesArr });
    }

    return {
      path: content,
      relativePath: rel,
      content,
      topics: this.extractTopics(content),
      definitions,
      conflicts,
      contracts,
    };
  }

  private extractTopics(content: string): string[] {
    const topics = new Set<string>();
    for (const m of content.matchAll(/^##\s+(.+)/gm)) {
      topics.add(m[1].trim());
    }
    return [...topics];
  }

  /** Deterministic keyword-based relevance search. Returns entries w/ overlapping tokens. */
  search(query: string, limit = 8): KnowledgeEntry[] {
    const tokens = query.toLowerCase().split(/[^a-z0-9_]+/).filter((t) => t.length > 2);
    const scored = this.index.map((e) => {
      const text = e.content.toLowerCase();
      let score = 0;
      for (const t of tokens) {
        if (text.includes(t)) score++;
      }
      return { e, score };
    });
    // Give deterministic tie-break by path length.
    return scored
      .filter((s) => s.score > 0)
      .sort((a, b) => b.score - a.score || a.e.relativePath.length - b.e.relativePath.length)
      .slice(0, limit)
      .map((s) => s.e);
  }

  /** Collect global conflicts across all files (same term defined in 2+ files). */
  collectiveConflicts(): Ambiguity[] {
    const termMap = new Map<string, { def: string; sources: string[] }>();
    for (const e of this.index) {
      for (const [term, defs] of e.definitions.entries()) {
        for (const d of defs) {
          if (!termMap.has(term)) termMap.set(term, { def: d, sources: [] });
          const t = termMap.get(term)!;
          if (!t.sources.includes(e.relativePath)) {
            // If same term in multiple files but identical definition → not a conflict.
            if (t.def !== d) t.sources.push(e.relativePath);
          }
        }
      }
    }
    const amb: Ambiguity[] = [];
    for (const [term, t] of termMap) {
      if (t.sources.length > 1) {
        amb.push({
          id: `amb-${crypto.createHash('sha1').update(term).digest('hex').slice(0, 8)}`,
          topic: term,
          question: `The term "${term}" has multiple definitions in deepwiki. Which is the ground truth for this task?`,
          candidates: [t.def, ...t.sources.map((s) => `(see ${s})`)],
          sources: t.sources,
          resolved: undefined,
        });
      }
    }
    return amb;
  }

  /** Assert-based validation: no ambiguous terminology in the final plan. */
  validPlanReferences(): { ok: boolean; errors: string[] } {
    const errors: string[] = [];
    for (const e of this.index) {
      for (const c of e.conflicts) {
        errors.push(`File ${e.relativePath}: term "${c.term}" has ${c.candidates.length} definitions.`);
      }
    }
    return { ok: errors.length === 0, errors };
  }

  /** Gather all upstream/downstream dependencies referenced anywhere. */
  allContracts(): ContractInfo[] {
    const out: ContractInfo[] = [];
    for (const e of this.index) {
      if (e.contracts) out.push(...e.contracts);
    }
    return out;
  }
}

/** Build a deterministic research snapshot for a ticket. */
export async function researchForTicket(
  ws: vscode.WorkspaceFolder,
  deepwiki: DeepWikiService,
  ticketKey: string,
): Promise<DeepWikiResult> {
  const entries = deepwiki.scan(ws);
  const relevant = deepwiki.search(`${ticketKey} ${ticketKey.split('-')[1] || ''}`);
  const unresolvedAdjacencies = deepwiki.collectiveConflicts();
  const unresolved = unresolvedAdjacencies.filter((a) => !a.resolved);

  // If deepwiki found nothing relevant, fall back to actual repo code.
  // This is explicitly tagged as "from repo code (not deepwiki)", never assumed.
  if (relevant.length === 0 && entries.length === 0) {
    const repoFallback = await repoCodeGroundSearch(ws, ticketKey);
    const summary = buildMarkdownSummary(entries, repoFallback, ticketKey + ' (from repo — no deepwiki)');
    return {
      entries: repoFallback,
      relevant: repoFallback,
      unresolvedAmbiguities: unresolved,
      summaryMarkdown: summary,
    };
  }

  const summary = buildMarkdownSummary(entries, relevant, ticketKey);
  return { entries, relevant, unresolvedAmbiguities: unresolved, summaryMarkdown: summary };
}

/**
 * When deepwiki has no entries, fall back to the actual repo code.
 * This is a deterministic keyword-based scan — it never infers intent.
 * Every entry is tagged with [source: repo-code] so the agent can't assume it's deepwiki ground truth.
 */
async function repoCodeGroundSearch(ws: vscode.WorkspaceFolder, query: string): Promise<KnowledgeEntry[]> {
  const tokens = query.toLowerCase().split(/[^a-z0-9_]+/).filter(t => t.length > 2);
  const root = ws.uri.fsPath;
  const out: KnowledgeEntry[] = [];

  const walk = (dir: string) => {
    let ents;
    try {
      // Guardrail: never follow a symlink that escapes the workspace.
      resolveWithin(root, dir);
      ents = fs.readdirSync(dir, { withFileTypes: true });
    } catch {
      return;
    }
    for (const e of ents) {
      if (e.name === 'node_modules' || e.name === '.git' || e.name === 'out' || e.name === 'dist' || e.name === '.deepwiki-agents') continue;
      const full = path.join(dir, e.name);
      if (e.isDirectory()) walk(full);
      else if (/(\.ts|\.js|\.md|\.py|\.go|\.java|\.rs|\.yaml|\.json|\.xml)$/i.test(e.name)) {
        try {
          resolveWithin(root, full);
          const content = fs.readFileSync(full, 'utf-8');
          const contentToks = content.toLowerCase();
          const matchCount = tokens.filter(t => contentToks.includes(t)).length;
          if (matchCount > 0) {
            const rel = path.relative(root, full).split(path.sep).join('/');
            const header = `[source: repo-code — not deepwiki] ${rel} (matched ${matchCount}/${tokens.length} query tokens)`;
            const entry = {
              path: content,
              relativePath: rel,
              content: `# ${header}\n\nExcerpt (first 3000 chars):\n${content.slice(0, 3000)}`,
              topics: [...new Set(tokens.filter(t => contentToks.includes(t)))],
              definitions: new Map<string, string[]>(),
              conflicts: [],
            } as KnowledgeEntry;
            out.push(entry);
            if (out.length >= 6) return;
          }
        } catch { /* skip unreadable */ }
      }
    }
  };

  walk(root);
  return out;
}

function buildMarkdownSummary(entries: KnowledgeEntry[], relevant: KnowledgeEntry[], ticketKey: string): string {
  const lines: string[] = [];
  lines.push(`# DeepWiki Ground-Truth Summary for ${ticketKey}`);
  lines.push('');
  lines.push(`> All of the following is **verifiable** and must be cited. The agent must NOT invent facts.`);
  lines.push('');
  lines.push(`## Relevant knowledge entries`);
  for (const e of relevant) {
    lines.push(`### ${e.relativePath}`);
    lines.push('');
    lines.push(e.content.slice(0, 4000));
    lines.push('');
  }
  lines.push(`## All knowledge files`);
  for (const e of entries) {
    lines.push(`- \`${e.relativePath}\` (${e.topics.join(', ')})`);
  }
  return lines.join('\n');
}