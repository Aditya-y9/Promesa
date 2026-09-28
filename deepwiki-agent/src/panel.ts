import * as vscode from 'vscode';
import * as path from 'path';

/**
 * The Webview panel exposing the coding-agent GUI:
 *   - Settings tab (Gemini API key, Jira credentials)
 *   - Tickets tab (Jira tickets assigned to me)
 *   - Plan / flowcharts tab
 *   - Chat tab (hmm right, a human chat)
 *   - Skills tab
 */
export class AgentPanel {
  public static current: AgentPanel | undefined;
  private readonly panel: vscode.WebviewPanel;
  private disposables: vscode.Disposable[] = [];

  constructor(private context: vscode.ExtensionContext) {
    this.panel = vscode.window.createWebviewPanel(
      'deepwiki.agent',
      'DeepWiki Agent',
      vscode.ViewColumn.One,
      {
        enableScripts: true,
        retainContextWhenHidden: true,
        localResourceRoots: [vscode.Uri.file(path.join(this.context.extensionPath, 'media'))],
      },
    );
    this.panel.webview.html = this.getHtml();
    this.panel.onDidDispose(() => this.dispose(), null, this.disposables);
  }

  static show(context: vscode.ExtensionContext): AgentPanel {
    if (AgentPanel.current) {
      AgentPanel.current.panel.reveal(vscode.ViewColumn.One);
      return AgentPanel.current;
    }
    const p = new AgentPanel(context);
    return p;
  }

  dispose(): void {
    AgentPanel.current = undefined;
    while (this.disposables.length) {
      this.disposables.pop()?.dispose();
    }
  }

  post(msg: unknown): void {
    try {
      this.panel.webview.postMessage(msg);
    } catch {
      /* panel disposed */
    }
  }

  /** Let the orchestrator drive this panel's messaging. */
  registerMessageHandler(handler: (e: any) => void): void {
    this.panel.webview.onDidReceiveMessage(handler, undefined, this.disposables);
  }

  private getHtml(): string {
    return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
  * { box-sizing: border-box; margin:0; padding:0; }
  body { font-family: var(--vscode-font-family); color: var(--vscode-foreground); background: var(--vscode-editor-background); }
  .topnav { display:flex; gap:0; border-bottom:1px solid var(--vscode-panel-border); background: var(--vscode-editor-background); padding:0 4px; }
  .topnav button.tab { background:transparent; border:0; border-bottom:2px solid transparent; color: var(--vscode-foreground); padding:10px 14px; cursor:pointer; font-size:13px; }
  .topnav button.tab.active { border-bottom-color: var(--vscode-button-background); font-weight:600; }
  .page { display:none; padding:16px; }
  .page.active { display:block; }
  input, textarea, select { width:100%; padding:8px; background: var(--vscode-input-background); color: var(--vscode-input-foreground); border:1px solid var(--vscode-input-border); border-radius:4px; margin:4px 0 10px; }
  label { font-size:12px; opacity:.8; display:block; margin-top:6px; }
  button { cursor:pointer; padding:8px 16px; background: var(--vscode-button-background); color: var(--vscode-button-foreground); border:0; border-radius:4px; margin:2px; }
  button.secondary { background: var(--vscode-button-secondaryBackground); color: var(--vscode-button-secondaryForeground); }
  h2 { margin-bottom:10px; }
  .msg { border:1px solid var(--vscode-panel-border); border-radius:6px; padding:10px; margin:8px 0; white-space:pre-wrap; }
  .msg.human { background: var(--vscode-editorWidget-background); }
  .ticket { border:1px solid var(--vscode-panel-border); padding:10px; margin:6px 0; border-radius:6px; cursor:pointer; }
  .ticket:hover { background: var(--vscode-list-hoverBackground); }
  .badge { display:inline-block; padding:2px 8px; border-radius:10px; font-size:11px; background: var(--vscode-badge-background); color: var(--vscode-badge-foreground); margin-left:6px; }
  .plan { border:1px solid var(--vscode-editorWidget-border); margin:10px 0; padding:12px; border-radius:8px; }
  .flowchart { background: var(--vscode-sideBar-background); padding:10px; border-radius:6px; font-family: monospace; white-space: pre; overflow-x:auto; font-size:12px; }
  #chatlog { max-height: 50vh; overflow-y:auto; border:1px solid var(--vscode-panel-border); border-radius:6px; padding:8px; margin-bottom:10px; }
  .kbd { background: var(--vscode-editorWidget-background); border-radius:3px; padding:1px 5px; }
</style>
</head>
<body>
<div class="topnav" id="nav">
  <button class="tab active" data-tab="settings">Settings</button>
  <button class="tab" data-tab="tickets">Tickets</button>
  <button class="tab" data-tab="plan">Plan</button>
  <button class="tab" data-tab="chat">Agent Chat</button>
  <button class="tab" data-tab="skills">Skills</button>
  <button class="tab" data-tab="guardrail">Guardrails</button>
</div>

<div class="page active" id="page-settings">
  <h2>LLM &amp; Jira Configuration</h2>
  <label>Google Gemini API Key</label>
  <input type="password" id="geminiKey" placeholder="AIza...">
  <label>Jira base URL</label>
  <input type="text" id="jiraUrl" placeholder="https://yourcompany.atlassian.net">
  <label>Jira email / username</label>
  <input type="text" id="jiraUser" placeholder="you@company.com">
  <label>Jira API Token</label>
  <input type="password" id="jiraToken">
  <label>JQL override (optional)</label>
  <input type="text" id="jiraJql" placeholder="assignee = currentUser()">
  <button onclick="saveSettings()">Save configuration</button>
  <p style="opacity:.7; margin-top:8px; font-size:12px">Keys are stored in VS Code SecretStorage.</p>
</div>

<div class="page" id="page-tickets">
  <h2>Assigned tickets</h2>
  <button onclick="fetchTickets()">🔄 Refresh assigned tickets</button>
  <div id="ticketList"><p style="opacity:.6">No tickets loaded.</p></div>
</div>

<div class="page" id="page-plan">
  <h2>Plans</h2>
  <button onclick="startAgent()">Start agent on selected ticket</button>
  <button class="secondary" onclick="runContractTests()">Run contract tests</button>
  <div id="planArea"><p style="opacity:.6">Select a ticket, then “Start agent”. The agent will research DeepWiki, surface ambiguity, propose 3 plans with flowcharts, and wait for your choice.</p></div>
</div>

<div class="page" id="page-chat">
  <h2>Human ⇄ Agent</h2>
  <div id="chatlog"><p style="opacity:.6">Agent output appears here.</p></div>
  <div style="display:flex; gap:6px">
    <input type="text" id="chatinput" placeholder="Type a message to the agent…">
    <button onclick="sendHuman()">Send</button>
  </div>
</div>

<div class="page" id="page-skills">
  <h2>Skills &amp; Tools</h2>
  <button onclick="refreshSkills()">Refresh skills from <span class="kbd">skills/</span></button>
  <div id="skillList"><p style="opacity:.6">Skills folder not yet loaded.</p></div>
</div>

<div class="page" id="page-guardrail">
  <h2>🔐 Guardrails &amp; Observability</h2>

  <h3 style="margin-top:14px">Permission profile</h3>
  <p style="font-size:12px; opacity:.7">Controls what the agent can do in this session. Default: 🔒 read-only. Escalate only when needed.</p>
  <select id="permSelect" onchange="setPermission()">
    <option value="readonly" selected>🔒 Read-only research</option>
    <option value="workspace-write">✏️ Workspace write</option>
    <option value="workspace-exec">⚙️ Workspace + exec</option>
    <option value="elevated">🚀 Elevated (network + web)</option>
  </select>
  <p id="permLabel" style="font-size:12px; opacity:.7;">Current: 🔒 Read-only research</p>

  <h3 style="margin-top:14px">Session audit trail</h3>
  <button onclick="getTrace()">📋 Refresh trace log</button>
  <div id="traceSummary" style="font-size:12px; opacity:.8; margin:6px 0"></div>
  <div id="traceLog" style="max-height:30vh; overflow-y:auto; border:1px solid var(--vscode-panel-border); border-radius:6px; padding:6px; font-family:monospace; font-size:11px; white-space:pre-wrap;"></div>

  <h3 style="margin-top:14px">MCP servers</h3>
  <p style="font-size:12px; opacity:.7">MCP servers registered from the workspace config. Each tool is disabled by default; enable per-session.</p>
  <div id="mcpList"><p style="opacity:.6">No MCP servers registered. Add entries to your workspace MCP config to register tools.</p></div>
</div>

<script>
  const vscode = acquireVsCodeApi();
  let currentTicket = null;
  let selectedKey = null;

  const tabs = document.querySelectorAll('#nav button');
  tabs.forEach(t => t.addEventListener('click', () => {
    document.querySelectorAll('#nav button').forEach(x => x.classList.remove('active'));
    t.classList.add('active');
    document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
    document.getElementById('page-' + t.dataset.tab).classList.add('active');
  }));

  window.addEventListener('message', e => {
    const msg = e.data;
    switch (msg.type) {
      case 'status': statusFlash(msg.payload?.text); break;
      case 'tickets': renderTickets(msg.payload); break;
      case 'agentMsg': appendChat(msg.payload); break;
      case 'plans': renderPlans(msg.payload); break;
      case 'ambiguities': renderAmbiguities(msg.payload); break;
      case 'skills': renderSkills(msg.payload); break;
      case 'permission': renderPermission(msg.payload); break;
      case 'trace': renderTrace(msg.payload); break;
      case 'selectionPrompt': promptSelection(msg.payload); break;
      case 'humanPrompt': promptHuman(msg.payload); break;
      case 'error': appendChat({ role:'system', content: msg.payload?.message || 'Error', kind:'error' }); break;
      default: console.warn('unhandled', msg.type);
    }
  });

  function saveSettings() {
    vscode.postMessage({ type:'saveSettings', payload: {
      geminiApiKey: document.getElementById('geminiKey').value,
      jira: {
        baseUrl: document.getElementById('jiraUrl').value,
        username: document.getElementById('jiraUser').value,
        apiToken: document.getElementById('jiraToken').value,
        jql: document.getElementById('jiraJql').value || undefined
      }
    }});
  }
  function fetchTickets() { vscode.postMessage({ type:'fetchTickets' }); }
  function startAgent() {
    if (!selectedKey) { appendChat({ role:'system', content:'Select a ticket first.', kind:'status' }); return; }
    vscode.postMessage({ type:'startAgent', payload:{ ticketKey: selectedKey } });
  }
  function runContractTests() {
    vscode.postMessage({ type:'runContractTests', payload:{ ticketKey: selectedKey } });
  }
  function sendHuman() {
    const t = document.getElementById('chatinput').value;
    if (!t.trim()) return;
    vscode.postMessage({ type:'humanReply', payload:{ text: t } });
    appendChat({ role:'human', content: t, kind:'chat' });
    document.getElementById('chatinput').value = '';
  }
  function refreshSkills() { vscode.postMessage({ type:'refreshSkills' }); }
  function setPermission() {
    const v = document.getElementById('permSelect').value;
    vscode.postMessage({ type:'setPermission', payload:{ profile: v } });
  }
  function getTrace() { vscode.postMessage({ type:'getTrace' }); }
  function runMcp(server, tool) { vscode.postMessage({ type:'runMCP', payload:{ server, tool, args:{} } }); }
  function renderPermission(p) {
    if (!p) return;
    document.getElementById('permLabel').textContent = 'Current: ' + (p.label || p.profile);
  }
  function renderTrace(p) {
    if (!p) return;
    const s = p.summary || {};
    document.getElementById('traceSummary').textContent =
      'Session ' + (s.sessionId || '').slice(0,8) + ' · ' + (s.stepsCompleted||0) + ' steps · ' + (s.tokensIn||0) + ' tok in · $' + (s.totalCostUsd||0).toFixed(4) + ' · approvals ' + (s.approvalsGranted||0) + '/' + (s.approvalsRequested||0);
    const el = document.getElementById('traceLog');
    el.textContent = (p.entries || []).map(e => '['+e.timestamp+'] ['+e.source+'] '+e.action+' — '+e.detail).join('\n');
  }
  function renderMcpList(servers) {
    const el = document.getElementById('mcpList');
    if (!servers || !servers.length) return;
    el.innerHTML = '';
    servers.forEach(s => {
      const d = document.createElement('div');
      d.className = 'ticket';
      d.innerHTML = '<b>'+escapeHtml(s.name)+'</b> '+escapeHtml(s.description||'')+
        ' <button onclick="runMcp(\\''+escapeHtml(s.name)+'\\',\\''+escapeHtml(s.toolName)+'\\')">Call '+escapeHtml(s.toolName)+'</button>';
      el.appendChild(d);
    });
  }

  function statusFlash(t){ appendChat({ role:'system', content: t || '' , kind:'status'}); }
  function renderTickets(list) {
    const el = document.getElementById('ticketList');
    if (!list || !list.length) { el.innerHTML = '<p style="opacity:.6">No assigned unresolved tickets.</p>'; return; }
    el.innerHTML = '';
    list.forEach(t => {
      const div = document.createElement('div');
      div.className = 'ticket';
      div.innerHTML = '<b>'+t.key+'</b> '+escapeHtml(t.summary)+' <span class="badge">'+escapeHtml(t.status)+'</span>';
      div.onclick = () => {
        selectedKey = t.key;
        vscode.postMessage({ type:'selectTicket', payload:{ ticketKey: t.key } });
        div.style.outline = '1px solid var(--vscode-button-background)';
      };
      el.appendChild(div);
    });
  }
  function escapeHtml(s){ return String(s||'').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
  function appendChat(m) {
    const log = document.getElementById('chatlog');
    if (log.querySelector('p') && !log.querySelector('.msg')) log.innerHTML='';
    const div = document.createElement('div');
    div.className = 'msg ' + (m.role==='human' ? 'human' : (m.role==='system' ? 'system':'agent'));
    let content = escapeHtml(m.content || '');
    if (m.kind==='plan' && m.payload && m.payload.mermaid) content = '<pre class="flowchart">'+escapeHtml(m.payload.mermaid)+'</pre>';
    div.innerHTML = '<b>'+(m.role==='human'?'You': m.role==='system' ? 'System':'Agent')+':</b> ' + content;
    log.appendChild(div);
    log.scrollTop = log.scrollHeight;
  }
  function renderPlans(p) {
    document.getElementById('planArea').innerHTML = '<h3>'+escapeHtml(p.planLabel)+'</h3>';
    (p.plans || []).forEach(pl => {
      const d = document.createElement('div');
      d.className = 'plan';
      d.innerHTML = '<h4>Plan '+escapeHtml(pl.label)+': '+escapeHtml(pl.title)+'</h4><p>'+escapeHtml(pl.summary)+'</p>'+
        '<pre class="flowchart">'+escapeHtml(pl.mermaid)+'</pre>'+
        '<button onclick="choosePlan(\\''+escapeHtml(pl.id)+'\\')">Choose plan '+escapeHtml(pl.label)+'</button>';
      document.getElementById('planArea').appendChild(d);
    });
    const chat = document.createElement('div'); chat.className = 'plan';
    chat.innerHTML = '<h4>Or… let the agent draft a new plan</h4>'+
      '<input type="text" id="newPlanIdeas" placeholder="e.g. combine test-first with verify-first approach">'+
      '<button onclick="newPlan()">Create new plan</button>';
    document.getElementById('planArea').appendChild(chat);
  }
  function choosePlan(planId){ vscode.postMessage({ type:'choosePlan', payload:{ planId } }); }
  function newPlan(){ const i = document.getElementById('newPlanIdeas'); vscode.postMessage({ type:'createNewPlan', payload:{ instructions: i.value } }); }
  function renderAmbiguities(list){
    if(!list || !list.length) return;
    const d = document.createElement('div');
    d.className = 'plan';
    d.innerHTML = '<h4>⚠️ Ambiguity in ground truth — action required</h4>';
    list.forEach(a => {
      d.innerHTML += '<div><b>'+escapeHtml(a.topic)+'</b><p>'+escapeHtml(a.question)+'</p>'+
        '<input type="text" id="res-'+a.id+'" placeholder="Ground-truth answer">'+
        '<button onclick="resolveAmb(\\''+a.id+'\\')">Resolve</button></div>';
    });
    document.getElementById('chatlog').appendChild(d);
  }
  function resolveAmb(id){ vscode.postMessage({ type:'resolveAmbiguity', payload:{ ambiguityId:id, answer: document.getElementById('res-'+id).value } }); }
  function promptSelection(p){ appendChat({ role:'agent', content: p.question, kind:'chat' }); }
  function promptHuman(p){ appendChat({ role:'agent', content: p.text, kind:'chat' }); }
  function renderSkills(list){
    const el = document.getElementById('skillList');
    if(!list) return;
    el.innerHTML = '';
    list.forEach(s => {
      const d = document.createElement('div');
      d.className = 'ticket';
      d.innerHTML = '<b>'+escapeHtml(s.name)+'</b> '+escapeHtml(s.description)+' <button onclick="toggleSkill(\\''+escapeHtml(s.id)+'\\', '+!s.enabled+')">'+ (s.enabled?'Disable':'Enable') +'</button>';
      el.appendChild(d);
    });
  }
  function toggleSkill(id, enabled){ vscode.postMessage({ type:'toggleSkill', payload:{ skillId:id, enabled } }); refreshSkills(); }

  vscode.postMessage({ type:'ready' });
</script>
</body>
</html>`;
  }
}
