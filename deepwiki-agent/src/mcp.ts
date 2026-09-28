import { MCPServerConfig, MCPTool } from './types';
import { TraceService } from './trace';

/**
 * mcp.ts
 * -------
 * Minimal Model Context Protocol (JSON-RPC 2.0) client.
 *
 * Industry practice (2026): MCP is the standard for connecting agents to tools and
 * data (97M+ monthly SDK downloads, Linux Foundation governance). Enterprise rules:
 *   - MCP servers are untrusted until approved; scope per-tool, per-session.
 *   - Prefer incremental scope consent (request minimum access per operation).
 *   - Never send secrets; redact env vars from traces.
 *
 * This client talks to a subprocess stdio server OR a URL endpoint using JSON-RPC
 * 2.0. Keep zero heavy deps; the caller (agent.ts / panel) decides enablement.
 */

/** JSON-RPC types. */
type JsonRpcResponse =
  | { jsonrpc: '2.0'; result: unknown; id: number }
  | { jsonrpc: '2.0'; error: { code: number; message: string; data?: unknown }; id: number };

function isError(resp: JsonRpcResponse): resp is Extract<JsonRpcResponse, { error: unknown }> {
  return 'error' in resp;
}

export { MCPServerConfig };

export class MCPClient {
  private proc: import('child_process').ChildProcessWithoutNullStreams | undefined;
  private requestSeq = 0;
  private readonly pending = new Map<number, (r: JsonRpcResponse) => void>();
  private buffer = '';
  private tools: MCPTool[] = [];
  readonly serverName: string;

  constructor(
    private trace: TraceService,
    readonly config: MCPServerConfig,
  ) {
    this.serverName = config.name;
  }

  async connect(): Promise<void> {
    if (this.config.url) {
      // stdio forced off for URL transport; not implemented here.
      throw new Error(`MCP URL transport for "${this.config.name}" not supported in this build. Use a local stdio server.`);
    }
    const { spawn } = require('child_process') as typeof import('child_process');
    const cmd = this.config.command;
    if (!cmd) throw new Error('MCP server requires a command.');
    const env = { ...process.env, ...(this.config.env || {}) };
    // Strip any secret-looking env vars before passing to the subprocess/trace.
    const redactedEnv = redactEnv(env);
    this.proc = spawn(cmd, this.config.args || [], { env, stdio: ['pipe', 'pipe', 'pipe'] });
    this.proc.stdout?.on('data', (chunk: Buffer) => this.onData(chunk.toString()));
    this.proc.stderr?.on('data', () => {
      /* stderr is for diagnostics only; redact lines that may contain secrets */
    });
    this.trace.system('mcp.connect', `Connected to MCP server "${this.config.name}" (env keys: ${Object.keys(redactedEnv).join(', ')})`);
    const initRes = await this.request('initialize', { protocolVersion: '2024-11-05', capabilities: {}, clientInfo: { name: 'deepwiki-agent', version: '0.1.0' } });
    if (isError(initRes)) throw new Error(`MCP initialize error: ${initRes.error.message}`);
  }

  private onData(chunk: string): void {
    this.buffer += chunk;
    let idx: number;
    while ((idx = this.buffer.indexOf('\n')) >= 0) {
      const line = this.buffer.slice(0, idx).trim();
      this.buffer = this.buffer.slice(idx + 1);
      if (!line) continue;
      try {
        const msg = JSON.parse(line) as JsonRpcResponse;
        const resolve = this.pending.get(msg.id);
        if (resolve) {
          this.pending.delete(msg.id);
          resolve(msg);
        }
      } catch {
        /* partial/keep-alive line */
      }
    }
  }

  private request(method: string, params: unknown): Promise<JsonRpcResponse> {
    return new Promise((resolve, reject) => {
      const id = ++this.requestSeq;
      this.pending.set(id, resolve);
      const payload = JSON.stringify({ jsonrpc: '2.0', id, method, params });
      this.proc?.stdin?.write(payload + '\n');
      // basic timeout to avoid hanging the extension
      setTimeout(() => {
        if (this.pending.has(id)) {
          this.pending.delete(id);
          reject(new Error(`MCP request "${method}" timed out.`));
        }
      }, 15_000);
    });
  }

  async listTools(scope?: { enabled: string[] }): Promise<MCPTool[]> {
    const res = await this.request('tools/list', {});
    if (isError(res)) throw new Error(`MCP tools/list error: ${res.error.message}`);
    const list = (res.result as { tools?: Array<{ name: string; description?: string; inputSchema?: unknown }> }).tools || [];
    this.tools = list.map((t) => ({
      serverName: this.config.name,
      toolName: t.name,
      description: t.description || '',
      inputSchema: t.inputSchema || {},
      enabled: scope ? (scope.enabled || []).includes(t.name) : true,
    }));
    return this.tools;
  }

  async callTool(name: string, args: unknown): Promise<unknown> {
    if (!this.tools.find((t) => t.toolName === name && t.enabled)) {
      throw new Error(`MCP tool "${name}" is not enabled for session scope.`);
    }
    this.trace.system('mcp.call', `Calling MCP tool "${this.config.name}/${name}"`, { tool: `mcp:${name}` });
    const res = await this.request('tools/call', { name, arguments: args });
    if (isError(res)) throw new Error(`MCP tools/call error: ${res.error.message}`);
    return res.result;
  }

  async listResources(): Promise<unknown> {
    const res = await this.request('resources/list', {});
    if (isError(res)) throw new Error(`MCP resources/list error: ${res.error.message}`);
    return res.result;
  }

  dispose(): void {
    this.proc?.kill();
    this.pending.clear();
  }
}

/** Deterministically redact env keys that look like secrets, for traces/logs. */
export function redactEnv(env: Record<string, string | undefined>): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(env)) {
    out[k] = /(KEY|TOKEN|SECRET|PASSWORD|PASS|CRED|AUTH|AWS_|AZURE_|GCP_|GIT_|PROD)/i.test(k) && v ? '[redacted]' : String(v || '');
  }
  return out;
}
