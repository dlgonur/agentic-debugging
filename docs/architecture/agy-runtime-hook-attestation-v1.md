# AGY Runtime Hook Attestation v1

This document records the bounded AGY 1.1.13 runtime repair for the Local
Application decision-only adapter. It is an evidence record and adapter
contract, not a claim of model or provider performance.

## Evidence interpretation

Three owner-authorized real canaries were observed without accepting a
directive or result:

1. The first AGY init advertised `ask_permission`; the adapter rejected it.
2. After narrowly allowing that advertisement, the second init advertised
   `ask_question`; the adapter rejected it.
3. With the documented decision-only custom agent and deny-all `PreToolUse`
   hook, the third init advertised a broad AGY harness inventory, including
   `browser_click_element`, `browser_get_dom`, `browser_subagent`,
   `call_mcp_tool`, `define_subagent`, `execute_browser_javascript`,
   `generate_image`, `grep_search`, `invoke_subagent`, `list_dir`,
   `list_permissions`, `manage_subagents`, `manage_task`, `read_resource`,
   `read_url_content`, `run_command`, `search_web`, `send_message`,
   `view_file`, and `write_to_file`, among many others. The adapter failed
   closed before accepting any result.

Real evidence shows that `init.tools` cannot be treated as the effective
permission state of the selected custom agent. This is an evidence-based
interpretation of observed AGY 1.1.13 behavior; it does not assert that AGY
documentation defines `init.tools` as an authorization field. Quota or backend
generation consumption cannot be proven from these outputs.

## `init.tools` contract

`init.tools` is harness-advertised inventory telemetry, not an authorization
boundary. Missing `tools` and an empty array are valid. If present, the field
must be an array of bounded, non-empty strings: at most 256 items, at most 128
characters per name, and at most 16 KiB for the compact UTF-8 encoded array.
Names are not checked against safe-name, intrinsic, or category allowlists.
The adapter does not persist provider-private inventory content.

Actual tool, MCP, browser, question, permission, task, image, collaboration,
unknown/future-tool, or subagent activity remains forbidden. Stream parsing
rejects such activity independently of the advertised inventory.

## Request-local hook contract

Each request receives a fresh empty workspace and HOME, a fresh local nonce,
and a marker path inside the request isolation root. The generated hooks
configuration contains:

- `PreInvocation`: one direct command handler that reads bounded JSON input,
  requires documented non-negative bounded integer `invocationNum` and
  `initialNumSteps` fields, validates bounded camelCase metadata when present,
  writes only `{"schema":"agy-preinvocation-attestation-v1","nonce":"<exact request nonce>"}`
  using exclusive file creation, and returns `{"injectSteps":[]}`;
- `PreToolUse` with matcher `"*"`: one command handler that returns the exact
  deny response `{"decision":"deny","reason":"Local Application decision-only model: tool execution disabled"}`.

The marker is checked absent before AGY launch. After a successful AGY
process, the adapter performs a bounded read and requires the exact schema and
request nonce before parsing and accepting the terminal directive. Missing,
malformed, oversized, stale, or wrong-nonce markers fail closed. This proves
that the request's generated hook configuration was discovered and executed
before the model invocation; it does not prove that `PreToolUse` was triggered
by a real tool attempt.

The selected temporary agent remains pinned as
`local-application-decision` and declares `tools: []`, `mainAgent: true`,
`subagent: false`, `commandExecutionPolicy: off`, `mcpServers: []`,
`skills: []`, and `plugins: []`. Local Application remains the only executor
of tools, PDB, patching, and verification.

## Preserved bounds

The adapter continues to use model `gemini-3.7-flash-medium`, zero adapter
retries and fallback, one adapter-owned `--print` process per request, no
persistent conversation, a 20-second process timeout, a 64 KiB stream/output
bound, a 25 logical-call cap, a 25,000-byte public request cap, and a 30,000
character Windows command-line cap. Process-tree cancellation ownership is
unchanged.

The next single real canary is required to prove that real AGY executes the
generated `PreInvocation` hook and reaches a valid directive. This repair did
not run real inference or real `agy --print`.

## 2026-08-17 route status

AGY is no longer the primary Local Application runtime route. It was
abandoned as the primary route after its final bounded canary failed to
provide the required structured terminal output. AGY remains
historical/optional; the accepted security work recorded in this document is
preserved unchanged. The accepted real remote decision-model route is the
Ollama Cloud configured-command adapter
(`docs/architecture/ollama-cloud-command-adapter-v1.md`), whose first real
product proof completed successfully on 2026-08-17 (session
`sess-20260817-103258-3d1193`, verifier RESOLVED; see
`docs/project-tracker.md`). The "next single real canary" requirement above
is superseded as a current proof requirement for the Local Application
real-provider route; it remains the historical record of the AGY route's
final state.
