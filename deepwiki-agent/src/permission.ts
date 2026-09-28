import * as path from 'path';
import { PermissionProfile, PermissionProfileDef, PermissionGrant } from './types';

/**
 * permission.ts
 * -------------
 * Least-privilege session permission profiles.
 *
 * Industry practice (2026): never give an agent broad standing permissions.
 * Use per-session, ephemeral grants scoped to the exact workspace; default to
 * deny and escalate only with human approval (Anthropic session-scoped keys,
 * GitHub Copilot per-session operator grants, OWASP least-privilege).
 */

export const PERMISSION_PROFILES: Record<PermissionProfile, PermissionProfileDef> = {
  readonly: {
    name: 'readonly',
    label: '🔒 Read-only research',
    allowRead: ['**'],
    allowWrite: [],
    allowExec: false,
    allowNetwork: false,
    allowWebSearch: false,
  },
  'workspace-write': {
    name: 'workspace-write',
    label: '✏️ Workspace write',
    allowRead: ['**'],
    allowWrite: ['**'],
    allowExec: false,
    allowNetwork: false,
    allowWebSearch: false,
  },
  'workspace-exec': {
    name: 'workspace-exec',
    label: '⚙️ Workspace + exec',
    allowRead: ['**'],
    allowWrite: ['**'],
    allowExec: true,
    allowNetwork: false,
    allowWebSearch: false,
  },
  elevated: {
    name: 'elevated',
    label: '🚀 Elevated',
    allowRead: ['**'],
    allowWrite: ['**'],
    allowExec: true,
    allowNetwork: true,
    allowWebSearch: true,
  },
};

/** Escalation order — a higher index implies strictly more capability. */
const ORDER: PermissionProfile[] = ['readonly', 'workspace-write', 'workspace-exec', 'elevated'];
const RANK = (p: PermissionProfile) => ORDER.indexOf(p);

/**
 * Security-token-style session grant: ephemeral, scoped, expiring.
 * Mirror of the "session-scoped key" pattern used by production agents.
 */
export class PermissionManager {
  private activeProfile: PermissionProfile;

  constructor(
    private workspaceRoot: string,
    private defaultProfile: PermissionProfile = 'readonly',
  ) {
    this.activeProfile = defaultProfile;
  }

  current(): PermissionGrant {
    return {
      profile: this.activeProfile,
      approvedAt: Date.now(),
      approvedBy: 'policy',
      scope: this.workspaceRoot,
      expiresAt: Date.now() + 60 * 60 * 1000,
      reason: 'active session profile',
    };
  }

  /** Issue (or honor) a session-scoped grant. Always ephemeral. */
  issue(profile: PermissionProfile, approvedBy: PermissionGrant['approvedBy'], reason: string, ttlMs = 60 * 60 * 1000): PermissionGrant {
    this.activeProfile = profile;
    return this.current();
  }

  /** Grant a higher profile if the requester holds a lower one (or equal). */
  escalate(profile: PermissionProfile, approvedBy: PermissionGrant['approvedBy'], reason: string): PermissionGrant | undefined {
    if (RANK(profile) < RANK(this.activeProfile)) {
      return undefined; // cannot downgrade an active grant
    }
    return this.issue(profile, approvedBy, reason);
  }

  def(): PermissionProfileDef {
    return PERMISSION_PROFILES[this.current().profile];
  }

  canRead(target: string): boolean {
    return this.def().allowRead.some((p) => pathMatches(this.workspaceRoot, target, p));
  }

  canWrite(target: string): boolean {
    return this.def().allowWrite.some((p) => pathMatches(this.workspaceRoot, target, p));
  }

  canExec(): boolean {
    return this.def().allowExec;
  }

  canNetwork(): boolean {
    return this.def().allowNetwork;
  }

  canWebSearch(): boolean {
    return this.def().allowWebSearch;
  }
}

/** Minimal path-matching for `**` globs (kept local + consistent with guardrail.ts globToRegExp). */
function pathMatches(root: string, target: string, pattern: string): boolean {
  const rel = path.relative(root, target).split(path.sep).join('/');
  const regex = new RegExp(
    '^' + pattern.split('/').map((seg) => (seg === '**' ? '.*' : seg.replace(/[.+^${}()|[\]\\]/g, '\\$&').replace(/\*/g, '[^/]*'))).join('/') + '$',
  );
  return regex.test(rel);
}
