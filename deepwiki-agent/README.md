# 🧠 DeepWiki Agent — Risk-Aware Coding Agent for VS Code

A supercharged "Claude Code–style" coding agent built as a VS Code extension. It connects to **Jira**, plans with **Google Gemini**, grounds every decision in a **`deepwiki/` knowledge base** (falling back to the actual repo only when needed), enforces **hard deterministic guardrails** (it never assumes), and is **risk-aware**: it analyzes upstream/downstream impacts and verifies contracts before and after a change.

> **Enterprise hardening (2026):** this build adds production guardrails — least-privilege permission profiles, prompt-injection defense, an append-only audit trail, a risk-graded approval matrix, MCP client with per-session tool scoping, and schema-validated plan output. See **[`ENTERPRISE.md`](./ENTERPRISE.md)** for the full operating playbook, and `tests/` for the CI gate.

---

## ✨ Features

| Feature | Where |
| --- | --- |
| 🔑 Enter a **Gemini API key** (+ Jira credentials) in the GUI | `Settings` tab |
| 🎫 Show **tickets assigned to you** via the Jira REST API | `Tickets` tab |
| 📥 Select a ticket → agent fetches **all data** and persists it as **LLM-readable files** (`json` + `markdown`) | `.deepwiki-agents/tickets/<KEY>/` |
| 📚 Ground truth from **`deepwiki/`** knowledge files | `deepwiki/` folder |
| 🔍 Falls back to **actual repo code** only when deepwiki is empty (tagged `[source: repo-code]`, never assumed) | auto |
| 🛑 **Hard guardrails**: ambiguous terms (multiple definitions in deepwiki) → agent **stops and asks the human** via an interactive window. It does not guess. | automatic |
| 🏛️ **Company policies** from `company/*.yaml` checked before every plan | `company/` folder |
| 📊 **3 best plans with Mermaid flowcharts** (Plan A / B / C), presented for human selection | `Plan` tab |
| 💬 Chat with the agent to draft a **new plan (D)** combining ideas | `Agent Chat` tab |
| ⚠️ **Risk assessment** — upstream/downstream impact read from deepwiki `Contract:` blocks | stored in `risk-assessment.md` |
| 🧪 **Contract tests** — verifies every deepwiki contract still resolves in repo code, writes a report, flags failures | `contract-test-report.md` |
| 🛠️ **Skills / tools** — add skill files in `skills/` (front-matter + content); enable/disable in GUI | `skills/` folder + `Skills` tab |
| 🔐 **Permission profiles** — read-only / workspace-write / workspace-exec / elevated. Default 🔒 read-only; escalate per session | `Guardrails` tab |
| 🛡️ **Prompt-injection defense** — repo code, Jira, skills & MCP responses are fenced as untrusted data | automatic (every LLM call) |
| 📋 **Audit trail** — append-only JSONL + MD per run; token/cost/approval/exec all recorded | `.deepwiki-agents/traces/` |
| ✅ **Schema-validated plans** — Zod validation so malformed LLM output never reaches execution | auto |

---

## 🚀 Getting started

1. **Open** the extension in VS Code (`F5` → Extension Development Host) — or install the `.vsix`.
2. The **DeepWiki Agent** dashboard opens automatically.
3. Go to **Settings** → enter:
   - **Google Gemini API key** (`AIza...`) — stored in VS Code `SecretStorage`.
   - **Jira base URL** (e.g. `https://company.atlassian.net`), **email/username**, **API token** (or password for server).
   - Save.
4. Go to **Tickets** → **Refresh assigned tickets** → pick a ticket.
5. Click **Start agent on selected ticket** in the **Plan** tab.
6. The agent:
   - reads `deepwiki/` ground truth,
   - **blocks** on any ambiguity and asks you to resolve it inline,
   - produces **3 plans with flowcharts**,
   - asks you to **choose A / B / C** or chat to make a **custom plan**.
7. Click **Run contract tests** to verify internal & external contracts still hold.

---

## 📁 Folder conventions (created automatically on first run)

```
your-project/
├─ deepwiki/            ← GROUND TRUTH knowledge base (markdown)
│  ├─ README.md         ← authoring rules
│  └─ my-service.md     ← definitions / contracts / dependencies
├─ company/            ← company policy YAMLs
│  └─ policy.yaml      ← rules with id / severity / text
├─ skills/            ← agent skills (front-matter md files)
└─ .deepwiki-agents/  ← per-ticket artifacts the agent writes
   └─ tickets/<KEY/
│  ├─ jira-payload.json        ← full Jira issue (all fields)
│  ├─ ticket-context.md        ← LLM-readable ticket markdown
│  ├─ plan.md                 ← drafted plans
│  ├─ risk-assessment.md       ← upstream/downstream risks
│  └─ contract-test-report.md ← contract verification report
```

### `deepwiki/` entry format

```markdown
## Definitions
### auth-service
The service that issues JWT access tokens.

## Contracts
### Contract: AuthService.login
- Upstream: UserProfileService / getProfile
- Downstream: ApiGateway / authenticate
- Validate: `npm test auth`
```

> ⚠️ If `### term` appears with **different** definitions across files → **ambiguity** → the agent asks you to resolve before proceeding. **It will not guess.**

### `company/policy.yaml` format

```yaml
rules:
  - id: SEC-1
    severity: HIGH
    text: Never commit secrets or API keys.
```

### `skills/` format

```markdown
---
id: my-skill
name: My Skill
description: What it does.
entryPoint: ./my-skill.md
---

Skill body / instructions the agent will heed.
```

---

## 🛡️ Guardrails (deterministic, not probabilistic)

1. **No-assumption rule** — the agent may only state facts traceable to `deepwiki/` (or repo code, explicitly tagged).
2. **Ambiguity gate** — conflicting definitions halt execution and open an interactive human prompt. Nothing proceeds until resolved.
3. **Policy audit** — `company/*.yaml` rules are checked against plan text before plans are presented.
4. **Risk-first ordering** — upstream / downstream contract impact is assessed from deepwiki before any step runs.
5. **Contract verification** — every `Contract:` in deepwiki is searched in the repo; failures are surfaced and written to `contract-test-report.md`.
6. **Human-in-the-loop** — the agent never edits files on its own; you approve each step. (Plans are proposals.)

---

## 🔧 Commands

| Command | Id |
| --- | --- |
| Open Dashboard | `deepwiki.openDashboard` |
| Add a Skill | `deepwiki.addSkype` (opens `skills/<name>.md`) |

## ⚙️ Settings

- `deepwiki.companyFolder` — relative path to company policy YAMLs (default `company`).
- `deepwiki.deepwikiFolder` — relative path to deepwiki knowledge (default `deepwiki`).

## 🔐 Security

- The Gemini key and Jira token are stored in **VS Code `SecretStorage`**.
- JQL defaults to `assignee = currentUser() AND resolution = unresolved`.
- Jira uses Basic auth against the REST v3/agile endpoint.

## 🏗️ Tech

- TypeScript, VS Code Webview API.
- `@google/generative-ai` (Gemini 1.5 Pro, `temperature 0`).
- Jira REST API (basic auth via fetch).
- Mermaid flowchart rendering for plan visualization.

## 🧪 Development

```bash
npm install
npm run compile   # tsc -p ./
# F5 → Extension Development Host
```

## 📌 Roadmap

- [ ] Optional LangChain tool-calling loop once `deepwiki` confidence is high.
- [ ] Auto-apply approved plan steps as editable diffs.
- [ ] Multi-workspace Jira org support.
- [ ] Export plan/risk reports to Jira comments.
