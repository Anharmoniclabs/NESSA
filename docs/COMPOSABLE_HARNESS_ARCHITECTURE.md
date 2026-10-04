# Composable Local Agent Harness Architecture

This document includes both implemented layers and future design targets. See the
[2026-10-04 wiring audit](HARNESS_WIRING_AUDIT.md) for the current capability map
and executable conformance evidence.

## Product equation

```text
AI model + agentic harness = local AI agent
```

The model supplies language/code intelligence. The harness supplies durable context, tools,
skills, planning, observation, verification, process management, recovery and evidence.

The harness must remain useful when the model changes.

## Core rule

Use one parent agent loop. Skills are focused micro-harness recipes inside that loop; they do
not create another autonomous shell or nested agent by default.

```text
task
  -> context pack
  -> plan
  -> choose skill/tool
  -> action
  -> observation
  -> checkpoint
  -> continuous verification
  -> reviewer (optional)
  -> update state/context
  -> repeat
  -> final verification
  -> patch + evidence
```

## 1. Model layer

Adapters normalize local model backends behind one interface:

- OpenAI-compatible HTTP (Ollama, vLLM, llama.cpp)
- native tool calls or JSON-in-text fallback
- token accounting and context budgets
- primary coder model
- optional independent reviewer model

The reviewer cannot execute tools and cannot mark a task complete.

## 2. Context and memory

Context is layered instead of treating the model context window as memory.

### Persistent project instructions

Load predictable repository instruction files:

1. root `AGENTS.md`
2. root `agent.md` / `AGENT.md`
3. root `.agent/instructions.md`
4. nearest nested `AGENTS.md`/agent file for the target path

Broad instructions load before narrow instructions.

### Session state

Persist:

- task
- plan
- active skills
- tool observations
- edits
- checks
- reviewer notes
- checkpoints
- remaining budgets
- next intended step

### Long-term lessons

Repository-scoped reviewed lessons remain separate from model weights.

### Context compaction

Compaction should create a durable state digest rather than only deleting old tool output:

- current objective
- accepted plan
- changed files
- unresolved failures
- last meaningful observations
- verification state
- active skill state
- next action

The digest is persisted so a resumed process can rebuild context without replaying an entire chat.

### Search-backed recall

Do not inject all memory. Retrieve only the instructions, files, episodes and prior lessons
relevant to the current task/state.

## 3. Tools

Internal tools remain small and explicit:

- repository list/search/outline/read
- exact edit/write/undo
- diff
- registered checks
- bounded one-shot commands
- managed development processes
- document extraction
- skill discovery/activation
- instruction lookup

Later adapters expose external tools through MCP without changing the parent loop.

## 4. Skills as micro-harnesses

A skill is:

```text
instructions
+ required harness tools
+ required local adapters/commands
+ verification contract
+ optional setup/cleanup recipe
```

A skill is not:

```text
a new autonomous agent
a hidden shell session
a second planner with independent completion authority
```

Repository skill layout:

```text
.agent/
  skills/
    <skill-name>/
      SKILL.md
      skill.json
```

Example metadata:

```json
{
  "description": "Observe and debug the local web application",
  "tools": ["dev_start", "dev_status", "dev_logs", "run_check"],
  "commands": ["node", "chrome"],
  "checks": ["startup", "browser reproduction", "tests"]
}
```

Initial skills:

- observe-local-app
- browser-debug
- architecture-check

Future skills:

- package-quality
- database-migration
- performance-profile
- accessibility-audit
- API-contract-check
- release-preflight
- dependency-upgrade
- regression-bisect

## 5. Local development harness

Long-lived development processes should not be launched with ad-hoc `cmd &` shell strings.

Provide managed process primitives:

```text
dev_start(name, argv, cwd)
dev_status(name?)
dev_logs(name)
dev_stop(name)
```

Properties:

- argv execution, no shell interpolation
- named processes
- captured logs
- explicit working directory
- process status
- cleanup when a run ends
- later: durable daemon/reconnect across parent-process restarts
- later: port/health probes and service dependencies

This becomes the base for local application servers, workers, telemetry collectors and browser
debug adapters.

## 6. Browser/computer-use development harness

Use Chrome DevTools for agents as a dedicated development adapter rather than teaching the model
to scrape HTML blindly.

Target capabilities:

- attach to a development Chrome instance
- console inspection
- DOM inspection
- network requests/failures
- performance traces
- accessibility checks
- screenshots when necessary
- WebMCP/page-exposed debugging tools when present

Prefer the MCP adapter for rich browser inspection. Keep the CLI for targeted shell automation.

A browser debugging skill should:

1. start/confirm the local app
2. attach a development browser
3. reproduce the issue
4. record console/network/DOM/performance evidence
5. map the observation back to source
6. apply the fix
7. repeat the same observation
8. run repository tests

Do not attach the development agent to a personal authenticated browser profile.

## 7. MCP tool adapters

MCP is an adapter layer, not the agent loop.

```text
parent agent
  -> tool registry
      -> internal tool
      -> local CLI adapter
      -> MCP adapter
```

Implement against the current stateless MCP request model:

- explicit tool discovery
- deterministic tool names/order
- explicit state handles as tool arguments when a service needs durable state
- request/response timeouts
- structured tool output
- cache tool catalogs where the protocol allows it
- no hidden agent state inside the transport

The first MCP adapter should be Chrome DevTools because it directly improves coding/debugging.

## 8. Continuous verification

Verification is a loop concern, not a finish-only phase.

### After every edit

- save a patch checkpoint
- syntax/parse validation
- return failures immediately to the model

### Every few edits

- targeted/full registered tests depending on cost
- structural checks when configured

### At meaningful milestones

- application reproduction
- browser evidence
- architecture/conformance rules
- optional reviewer pass

### At finish

- rerun authoritative checks independently
- compare with baseline
- reject completion if required verification fails

## 9. Independent reviewer model

The reviewer receives:

- task
- current patch
- current verification summary
- selected architectural/project instructions

The reviewer returns concerns only.

It cannot:

- edit files
- execute commands
- approve a plan
- mark work complete

Reviewer cadence is configurable (for example every two successful edits and once before finalization).

## 10. Snapshots and recovery

Current lightweight checkpoint:

```text
evidence/checkpoints/edit-####-step-####.diff
```

Next durable recovery layer:

```text
session.json
events.jsonl
context-digest.json
tool-receipts.jsonl
checkpoints/
dev/processes.json
patch.diff
```

Resume algorithm:

1. load session + last checkpoint
2. rebuild workspace
3. replay only durable state transitions, not model prose
4. reconstruct compact context digest
5. reconcile managed processes
6. continue from the next unfinished phase

A crash must not silently restart the task from step zero.

## 11. Observability

Instrument the harness with OpenTelemetry-compatible traces.

Trace hierarchy:

```text
agent.run
  plan
  model.invoke
  skill.activate
  tool.execute
  edit
  checkpoint
  verification
  reviewer.invoke
  repair
  finalize
```

Record at minimum:

- session/task id
- model/backend
- model latency
- input/output token counts where available
- tool name + duration + status
- active skill
- edit/checkpoint number
- check status
- reviewer latency
- loop/repair count

Prompt/tool content capture must be configurable because it can contain repository secrets.

Initial local observability workflow:

- harness emits OTLP
- local collector/dashboard is started through the managed dev-process layer
- an observability skill tells the main agent how to start/query/stop it

## 12. Structural and architectural verification

Syntax is insufficient.

Add repository-defined conformance layers:

### JavaScript/TypeScript workspaces

- ESLint flat config
- custom ESLint rules
- module/dependency boundary rules
- package-level rules across pnpm workspace packages

### Polyglot structural rules

- ast-grep rules for AST-shape requirements
- dependency-cruiser for dependency graph contracts
- repository-specific executable checks

Examples:

- UI package cannot import server internals
- tools cannot directly mutate orchestrator state
- every external adapter must implement timeout/error normalization
- each workspace package must expose an approved public entrypoint
- no circular dependency across architecture layers
- every tool must declare a kind and schema
- every skill must declare its verification contract

These are "whole-codebase" tests: they assert architecture rather than only valid syntax.

## 13. Local configuration

Add a future `agentharness.toml`:

```toml
[context]
max_chars = 12000

[verification]
continuous = ["syntax"]
full_every_edits = 3
finish = ["syntax", "tests", "architecture"]

[reviewer]
enabled = true
every_edits = 2

[dev.app]
argv = ["pnpm", "dev"]
cwd = "apps/web"
health_url = "http://127.0.0.1:3000/health"

[checks]
tests = "pnpm test"
lint = "pnpm lint"
architecture = "pnpm architecture:check"

[mcp.chrome-devtools]
transport = "stdio"
argv = ["npx", "-y", "chrome-devtools-mcp@latest"]
```

The model may select among configured operations; it should not have to rediscover every local
command on every run.

## 14. Build sequence

### Phase A - composable harness foundation

- [x] persistent project instruction loader
- [x] micro-harness skill registry
- [x] built-in local-app/browser/architecture skill recipes
- [x] managed dev process start/status/logs/stop
- [x] per-edit patch checkpoints
- [x] automatic cheap verification after edits
- [x] periodic full-test hook
- [x] optional independent reviewer interface
- [x] tests for the new layers

### Phase B - durable session/context engine

- event-sourced session state
- restart-safe checkpoint manifest
- context digest
- exact resume
- progress leases/budgets
- durable dev-process reconciliation

### Phase C - MCP adapter bus

- MCP client interface
- stateless HTTP adapter
- stdio adapter
- tool discovery/cache
- tool output normalization
- Chrome DevTools adapter

### Phase D - developer observability

- OpenTelemetry spans/metrics
- local collector/dashboard skill
- trace viewer links in run evidence
- model/tool/check timing summaries

### Phase E - structural conformance

- `agentharness.toml`
- custom check registry
- ESLint/pnpm adapter
- ast-grep adapter
- dependency graph adapter
- architecture test examples

### Phase F - browser/computer-use loop

- app health discovery
- development Chrome lifecycle
- Chrome DevTools MCP tools
- browser reproduction/evidence objects
- post-fix replay

### Phase G - memory and retrieval

- durable episodic memory
- evidence-linked lessons
- semantic/file search
- compact state retrieval
- relevance scoring
- explicit memory provenance

### Phase H - local UI

- task entry
- loop timeline
- active skill
- tool calls
- dev processes
- browser state
- tests/checks
- reviewer notes
- context budget
- checkpoints
- pause/resume
- patch review

## 15. Acceptance tests

The integrated agent is not considered finished until these scenarios pass:

1. root AGENTS.md is loaded automatically.
2. nested AGENTS.md applies only to files under its directory.
3. repository skill is discovered without code changes to the harness.
4. skill activation does not spawn a second autonomous agent.
5. local app starts through managed argv execution and logs are captured.
6. dead startup is reported as a failed dev process.
7. an edit produces a checkpoint immediately.
8. syntax failure is returned immediately after the bad edit.
9. periodic tests run during a multi-edit task.
10. final verification independently reruns required checks.
11. reviewer sees patch/evidence but receives no tools.
12. reviewer failure does not crash the coding agent.
13. structural violation fails the architecture check.
14. Chrome adapter can reproduce and re-check a local UI bug.
15. MCP tool disconnect does not corrupt parent-loop state.
16. context compaction preserves task/plan/changes/failures/next step.
17. process restart resumes from the last durable checkpoint.
18. original source remains unchanged until explicit patch application.
19. observability shows model/tool/check spans for a complete run.
20. a new skill can be added by files alone and appears in `agentharness skills`.
