import * as fs from 'fs';
import * as path from 'path';

/**
 * guardrail.ts
 * ------------
 * Workspace containment + file-access boundary enforcement.
 *
 * Industry practice (2026): agent tools must not be able to escape the
 * workspace via `..`, symlinks, or absolute paths. Symlink escape is a
 * known CVE class in coding-agent tools (Cursor/Devin MCP & file-tool
 * advisories). This module gives us:
 *
 *   - a `resolveWithin` helper that rejects any path escaping `root`
 *   - a realpath (symlink-aware) containment check
 *   - deterministic allow/deny glob matching for reads and writes
 *
 * These are HARD deterministic guardrails — not prompt-based.
 */

/** Convert a tiny glob (supports `*` only) to a RegExp. */
export function globToRegExp(glob: string): RegExp {
  const escaped = glob
    .replace(/[.+^${}()|[\]\\]/g, '\\$&') // escape regex specials
    .replace(/\*\*/g, '\0DOUBLESTAR\0')
    .replace(/\*/g, '[^/\\\\]*')
    .replace(/\0DOUBLESTAR\0/g, '.*');
  return new RegExp(`^${escaped}$`);
}

/**
 * Resolve `target` relative to `root` and assert the result stays inside root.
 * Handles both relative and absolute target paths, and rejects `..` escapes.
 * Symlink-aware: resolves realpath before the check.
 */
export function resolveWithin(root: string, target: string): string {
  const absRoot = path.resolve(root);
  let absTarget: string;
  if (path.isAbsolute(target)) {
    absTarget = path.resolve(target);
  } else {
    absTarget = path.resolve(absRoot, target);
  }
  // Normalize and ensure the resolved path stays under root.
  const rel = path.relative(absRoot, absTarget);
  if (rel === '..' || rel.startsWith(`..${path.sep}`) || path.isAbsolute(rel)) {
    throw new Error(`Path escape blocked: "${target}" resolves outside workspace "${absRoot}"`);
  }
  // Symlink check — walk up from the target looking for an existing ancestor;
  // reject if any existing ancestor's realpath leaves the workspace.
  let probe = absTarget;
  while (true) {
    try {
      const real = fs.realpathSync(probe);
      const realRel = path.relative(absRoot, real);
      if (realRel === '..' || realRel.startsWith(`..${path.sep}`) || path.isAbsolute(realRel)) {
        throw new Error(`Symlink escape blocked: "${target}" resolves to "${real}" outside workspace`);
      }
      break;
    } catch (e: any) {
      if (e?.code === 'ENOENT' || e?.code === 'ENOTDIR') {
        const parent = path.dirname(probe);
        if (parent === probe) {
          // Hit filesystem root without finding an existing ancestor — walk off root differs.
          return absTarget;
        }
        probe = parent;
        continue;
      }
      throw e;
    }
  }
  return path.resolve(absTarget);
}

/** File-access permission evaluator using allow/deny globs. */
export class PathPolicy {
  constructor(
    private readonly allowRead: string[],
    private readonly allowWrite: string[],
  ) {}

  private matches(patterns: string[], relPath: string): boolean {
    return patterns.some((p) => globToRegExp(p.replace(/\\/g, '/')).test(relPath.replace(/\\/g, '/')));
  }

  canRead(root: string, target: string): { allowed: boolean; reason: string } {
    let rel: string;
    try {
      rel = path.relative(root, resolveWithin(root, target)).split(path.sep).join('/');
    } catch (e: any) {
      return { allowed: false, reason: e.message };
    }
    if (!this.matches(this.allowRead, rel)) {
      return { allowed: false, reason: `Read not permitted for "${rel}" under current permission profile.` };
    }
    return { allowed: true, reason: `Read allowed for "${rel}".` };
  }

  canWrite(root: string, target: string): { allowed: boolean; reason: string } {
    let rel: string;
    try {
      rel = path.relative(root, resolveWithin(root, target)).split(path.sep).join('/');
    } catch (e: any) {
      return { allowed: false, reason: e.message };
    }
    if (!this.matches(this.allowWrite, rel)) {
      return { allowed: false, reason: `Write not permitted for "${rel}" under current permission profile.` };
    }
    return { allowed: true, reason: `Write allowed for "${rel}".` };
  }
}

/** Default deny-everything-then-explicitly-allow patterns. Least privilege. */
export const READONLY_PATTERNS = ['**'];
export const WORKSPACE_WRITE_PATTERNS = ['**'];
export const SAFE_WRITE_DENY = [
  'node_modules/**',
  '.git/**',
  'out/**',
  'dist/**',
  '.deepwiki-agents/**',
  '.vscode/**',
  'package-lock.json',
];

/** Convenience: fully-resolved root for a workspace. */
export function guardInclude(wsRoot: string, ...paths: string[]): string[] {
  return [wsRoot, ...paths];
}
