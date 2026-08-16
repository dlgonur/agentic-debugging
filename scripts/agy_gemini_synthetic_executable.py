#!/usr/bin/env python3
"""Deterministic synthetic AGY CLI (test-only; never contacts a model).

Mirrors the AGY 1.1.13 surfaces used by the Gemini command adapter:

* ``--version``
* ``models``
* ``--print`` with ``--model``, ``--mode``, ``--sandbox``,
  ``--disable-slash-commands``, ``--output-format stream-json``,
  ``--json-schema``, ``--print-timeout``

The synthetic recovers the canonical public request from the inline prompt
(between ``=== BEGIN PUBLIC REQUEST ===`` / ``=== END PUBLIC REQUEST ===``)
and emits protocol stream-json events selected by ``synthetic_scenario``.

Network-incapable, deterministic, no external I/O besides local markers.
"""

from __future__ import annotations

import atexit
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

SYNTHETIC_VERSION = "1.1.13"
SYNTHETIC_MODELS = (
    "gemini-3.7-flash-high",
    "gemini-3.7-flash-medium",
    "gemini-3.7-flash-low",
)
PUBLIC_REQUEST_START = "=== BEGIN PUBLIC REQUEST ==="
PUBLIC_REQUEST_END = "=== END PUBLIC REQUEST ==="

DIRECTIVE_STOP = {"kind": "transition", "target_state": "Failed", "reason": "synthetic-success"}
DIRECTIVE_ACTION_BASELINE = {
    "kind": "action",
    "name": "run_reproduction",
    "arguments": {"phase": "baseline"},
}
DIRECTIVE_TRANSITION_UNDERSTAND = {
    "kind": "transition",
    "target_state": "Understand",
    "reason": "baseline failure reproduced",
}
DIRECTIVE_ADD_HYPOTHESIS = {
    "kind": "add_hypothesis",
    "hypothesis_id": "h-1",
    "statement": "synthetic root-cause hypothesis",
    "confidence": "medium",
    "evidence_refs": [],
    "requires_runtime_evidence": False,
}


def _argv_value(argv: list[str], flag: str) -> str | None:
    for index, item in enumerate(argv):
        if item == flag and index + 1 < len(argv):
            return argv[index + 1]
    return None


def _has_flag(argv: list[str], flag: str) -> bool:
    return flag in argv


def _request_from_message(message: str | None) -> dict[str, Any]:
    if not isinstance(message, str) or not message:
        return {}
    start = message.find(PUBLIC_REQUEST_START)
    if start < 0:
        return {}
    start += len(PUBLIC_REQUEST_START)
    end = message.find(PUBLIC_REQUEST_END, start)
    if end < 0:
        return {}
    payload = message[start:end].strip()
    if not payload:
        return {}
    try:
        value = json.loads(payload)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _state_legal_directive(request: dict[str, Any]) -> dict[str, Any]:
    controller = request.get("controller")
    state = controller.get("state") if isinstance(controller, dict) else None
    contracts = request.get("action_contracts")
    if state == "Reproduce" and isinstance(contracts, dict) and "run_reproduction" in contracts:
        return DIRECTIVE_ACTION_BASELINE
    if state == "Understand":
        return DIRECTIVE_ADD_HYPOTHESIS
    legal = controller.get("legal_transition_targets") if isinstance(controller, dict) else None
    if isinstance(legal, list) and "Understand" in legal:
        return DIRECTIVE_TRANSITION_UNDERSTAND
    if isinstance(legal, list) and legal:
        return {
            "kind": "transition",
            "target_state": legal[0],
            "reason": "synthetic-success",
        }
    return DIRECTIVE_STOP


def _emit(event: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _emit_init(argv: list[str], *, tools: list[str] | None = None) -> None:
    _emit({
        "event": "init",
        "init": {
            "cwd": str(Path.cwd()),
            "tools": ["ask_permission"] if tools is None else list(tools),
            "permission_mode": "request-review",
            "model": _argv_value(argv, "--model") or "",
            "agent": _argv_value(argv, "--agent") or "",
        },
    })


def _emit_user_input() -> None:
    _emit({
        "event": "step_update",
        "step_update": {
            "step_index": 0,
            "state": "DONE",
            "step_type": "user_input",
        },
    })


def _emit_reasoning() -> None:
    _emit({
        "event": "step_update",
        "step_update": {
            "step_index": 1,
            "state": "DONE",
            "step_type": "reasoning",
            "text_delta": "inspecting the supplied public request only",
        },
    })


def _emit_result(directive: dict[str, Any], *, usage: dict[str, Any] | None = None) -> None:
    payload: dict[str, Any] = {
        "status": "SUCCESS",
        "response": json.dumps(directive, ensure_ascii=False),
        "structured_output": directive,
        "num_turns": 1,
    }
    if usage is not None:
        payload["usage"] = usage
    _emit({"event": "result", "result": payload})


def _default_usage() -> dict[str, Any]:
    return {
        "input_tokens": 11,
        "output_tokens": 5,
        "thinking_tokens": 2,
        "cache_read_tokens": 0,
        "total_tokens": 18,
    }


def _pre_tool_use_hook_config() -> Path:
    home = os.environ.get("USERPROFILE") or os.environ.get("HOME") or ""
    return Path(home) / ".gemini" / "config" / "hooks.json"


def _consult_pre_tool_use_hook(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Run the isolated deny hook exactly as AGY would before a tool call."""
    config_path = _pre_tool_use_hook_config()
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {
            "hook_observed": False,
            "decision": None,
            "reason": "missing or invalid hooks.json",
        }
    if not isinstance(config, dict):
        return {"hook_observed": False, "decision": None, "reason": "hooks.json is not an object"}
    hook_commands: list[str] = []
    for definition in config.values():
        if not isinstance(definition, dict):
            continue
        entries = definition.get("PreToolUse")
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            matcher = entry.get("matcher")
            if not isinstance(matcher, str):
                continue
            try:
                matches = matcher in ("", "*") or re.fullmatch(matcher, tool_name) is not None
            except re.error:
                matches = False
            if not matches:
                continue
            handlers = entry.get("hooks")
            if not isinstance(handlers, list):
                continue
            for handler in handlers:
                if isinstance(handler, dict) and isinstance(handler.get("command"), str):
                    hook_commands.append(handler["command"])
    if not hook_commands:
        return {"hook_observed": False, "decision": None, "reason": "no matching PreToolUse hook"}
    payload = {
        "toolCall": {"name": tool_name, "args": arguments},
        "stepIdx": 2,
    }
    for command in hook_commands:
        try:
            completed = subprocess.run(
                command,
                input=json.dumps(payload, ensure_ascii=False),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=True,
                timeout=2.0,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return {"hook_observed": False, "decision": None, "reason": type(exc).__name__}
        if completed.returncode != 0:
            return {"hook_observed": False, "decision": None, "reason": "hook returned nonzero"}
        try:
            response = json.loads((completed.stdout or "").strip())
        except json.JSONDecodeError:
            return {"hook_observed": False, "decision": None, "reason": "hook output is not JSON"}
        if not isinstance(response, dict):
            return {"hook_observed": False, "decision": None, "reason": "hook output is not an object"}
        return {
            "hook_observed": True,
            "decision": response.get("decision"),
            "reason": response.get("reason"),
        }
    return {"hook_observed": False, "decision": None, "reason": "hook was not run"}


def _run_pre_tool_attempt(argv: list[str], request: dict[str, Any], *, tool_name: str, subagent: bool = False) -> int:
    arguments = {"synthetic": True}
    hook = _consult_pre_tool_use_hook(tool_name, arguments)
    evidence_root = Path.cwd().parent.parent
    sentinel = evidence_root / f"agy-synthetic-tool-side-effect-{os.getpid()}.sentinel"
    evidence_path = evidence_root / f"agy-synthetic-pretool-use-{os.getpid()}.json"
    evidence: dict[str, Any] = {
        "hook_observed": hook["hook_observed"],
        "decision": hook["decision"],
        "reason": hook["reason"],
        "tool_name": tool_name,
        "subagent": subagent,
        "sentinel": str(sentinel),
        "sentinel_exists_before": sentinel.exists(),
        "side_effect_attempted": False,
    }
    if hook["decision"] != "deny":
        sentinel.write_text("synthetic tool side effect\n", encoding="utf-8")
        evidence["side_effect_attempted"] = True
    evidence["sentinel_exists_after_hook"] = sentinel.exists()
    evidence_path.write_text(json.dumps(evidence, sort_keys=True) + "\n", encoding="utf-8")

    advertised = [tool_name] if tool_name in {"ask_permission", "ask_question", "list_permissions"} else []
    _emit_init(argv, tools=advertised)
    _emit_user_input()
    if subagent:
        _emit({
            "event": "step_update",
            "step_update": {
                "step_index": 2,
                "state": "DONE",
                "step_type": "agent_response",
                "subagent_info": {"subagents": [{"type_name": tool_name}]},
            },
        })
    else:
        _emit({
            "event": "step_update",
            "step_update": {
                "step_index": 2,
                "state": "DONE",
                "step_type": "tool",
                "tool_name": tool_name,
                "tool_info": {"name": tool_name, "parameters": arguments},
            },
        })
    _emit_result(_state_legal_directive(request), usage=_default_usage())
    return 0


def _record_invocation(argv: list[str], request: dict[str, Any]) -> None:
    record = {
        "argv": list(argv),
        "cwd": str(Path.cwd()),
        "pid": os.getpid(),
        "ppid": os.getppid(),
        "has_continue": _has_flag(argv, "--continue") or _has_flag(argv, "-c"),
        "has_conversation": _has_flag(argv, "--conversation"),
        "has_dangerously_skip": _has_flag(argv, "--dangerously-skip-permissions"),
        "has_add_dir": _has_flag(argv, "--add-dir"),
        "has_print": _has_flag(argv, "--print") or _has_flag(argv, "-p") or _has_flag(argv, "--prompt"),
        "mode": _argv_value(argv, "--mode"),
        "model": _argv_value(argv, "--model"),
        "output_format": _argv_value(argv, "--output-format"),
        "json_schema": _argv_value(argv, "--json-schema"),
        "print_timeout": _argv_value(argv, "--print-timeout"),
        "agent": _argv_value(argv, "--agent"),
        "sandbox": _has_flag(argv, "--sandbox"),
        "disable_slash_commands": _has_flag(argv, "--disable-slash-commands"),
        "prompt": _argv_value(argv, "--print") or _argv_value(argv, "-p") or _argv_value(argv, "--prompt"),
        "home": os.environ.get("USERPROFILE") or os.environ.get("HOME"),
        "scenario": request.get("synthetic_scenario"),
    }
    try:
        Path.cwd().joinpath("agy-last-invocation.json").write_text(
            json.dumps(record, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
    except OSError:
        pass
    marker_root = os.environ.get("USERPROFILE") or os.environ.get("HOME")
    if marker_root:
        try:
            counter_path = Path(marker_root) / "agy-print-count.json"
            count = 0
            if counter_path.is_file():
                try:
                    count = int(json.loads(counter_path.read_text(encoding="utf-8")).get("count", 0))
                except (OSError, ValueError, json.JSONDecodeError, TypeError):
                    count = 0
            counter_path.write_text(json.dumps({"count": count + 1}), encoding="utf-8")
        except OSError:
            pass


def _models_catalog(request: dict[str, Any] | None = None) -> str:
    scenario = request.get("synthetic_scenario") if isinstance(request, dict) else None
    models = list(SYNTHETIC_MODELS)
    if scenario == "unavailable-model":
        models = [item for item in models if item != "gemini-3.7-flash-medium"]
    lines = ["Fetching available models..."]
    labels = {
        "gemini-3.7-flash-high": "Gemini 3.7 Flash (High)",
        "gemini-3.7-flash-medium": "Gemini 3.7 Flash (Medium)",
        "gemini-3.7-flash-low": "Gemini 3.7 Flash (Low)",
    }
    for model_id in models:
        lines.append(f"{model_id}{labels.get(model_id, model_id)}")
    return "\n".join(lines) + "\n"


def _spawn_child(seconds: float) -> int:
    marker_root = os.environ.get("USERPROFILE") or os.environ.get("HOME") or tempfile.gettempdir()
    marker = Path(marker_root) / f"agy-gemini-synthetic-child-{os.getpid()}.json"
    code = (
        "import json,sys,time,os,pathlib\n"
        "marker, seconds = sys.argv[1], float(sys.argv[2])\n"
        "end = time.monotonic() + seconds\n"
        "count = 0\n"
        "while time.monotonic() < end:\n"
        "    count += 1\n"
        "    pathlib.Path(marker).write_text(json.dumps({'child_pid': os.getpid(), 'tick': count}), encoding='utf-8')\n"
        "    time.sleep(0.15)\n"
    )
    pid = subprocess.Popen(
        [sys.executable, "-c", code, str(marker), str(seconds)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    ).pid
    try:
        (Path.cwd() / f"agy-gemini-synthetic-child-{pid}.json").write_text(
            json.dumps({"child_pid": pid}), encoding="utf-8"
        )
    except OSError:
        pass
    return pid


def _spawn_external_cancel_tree() -> int:
    marker = Path.cwd() / f"agy-gemini-synthetic-tree-{os.getpid()}.json"
    adapter_pid = os.getppid()
    agy_pid = os.getpid()
    child_code = (
        "import json,sys,time,os,pathlib,subprocess\n"
        "marker = sys.argv[1]\n"
        "code = 'import time,sys; end = time.monotonic() + float(sys.argv[1]); '\n"
        "code += 'while time.monotonic() < end: time.sleep(0.15)'\n"
        "grandchild = subprocess.Popen([sys.executable, '-c', code, '150'],\n"
        "                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        f"pathlib.Path(marker).write_text(json.dumps({{\n"
        f"    'adapter_pid': {adapter_pid},\n"
        f"    'agy_pid': {agy_pid},\n"
        f"    'child_pid': os.getpid(),\n"
        f"    'grandchild_pid': grandchild.pid,\n"
        f"}}), encoding='utf-8')\n"
        "end = time.monotonic() + 150.0\n"
        "while time.monotonic() < end:\n"
        "    time.sleep(0.15)\n"
    )
    return subprocess.Popen(
        [sys.executable, "-c", child_code, str(marker)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    ).pid


def _run_print(argv: list[str]) -> int:
    prompt = _argv_value(argv, "--print") or _argv_value(argv, "-p") or _argv_value(argv, "--prompt")
    request = _request_from_message(prompt)
    _record_invocation(argv, request)
    scenario = request.get("synthetic_scenario")
    scenario = scenario if isinstance(scenario, str) else "valid"

    if scenario == "startup-failure":
        sys.stderr.write("synthetic agy: startup failure injected\n")
        return 1
    if scenario == "timeout":
        time.sleep(30.0)
        return 0
    if scenario == "timeout-with-child":
        _spawn_child(35.0)
        time.sleep(30.0)
        return 0
    if scenario == "external-cancel-tree":
        _spawn_external_cancel_tree()
        time.sleep(120.0)
        return 0
    if scenario == "nonzero-exit":
        sys.stderr.write("synthetic agy diagnostic on stderr\n")
        return 7
    if scenario == "credential-output":
        sys.stderr.write("api_key=super-secret-synthetic-value\n")
        _emit_init(argv)
        _emit_user_input()
        _emit_reasoning()
        _emit_result(_state_legal_directive(request), usage=_default_usage())
        return 0
    if scenario == "oversized":
        sys.stdout.write("x" * 8_000_000 + "\n")
        sys.stdout.flush()
        return 0
    if scenario == "malformed-ndjson":
        _emit_init(argv)
        sys.stdout.write("{not-valid-json\n")
        sys.stdout.flush()
        _emit_result(_state_legal_directive(request), usage=_default_usage())
        return 0
    if scenario == "missing-result":
        _emit_init(argv)
        _emit_user_input()
        _emit_reasoning()
        return 0
    if scenario == "duplicate-result":
        _emit_init(argv)
        _emit_user_input()
        directive = _state_legal_directive(request)
        _emit_result(directive, usage=_default_usage())
        _emit_result(directive, usage=_default_usage())
        return 0
    if scenario == "tool-event":
        _emit_init(argv)
        _emit_user_input()
        _emit({
            "event": "step_update",
            "step_update": {
                "step_index": 2,
                "state": "DONE",
                "step_type": "tool",
                "tool_name": "run_command",
                "tool_info": {
                    "name": "run_command",
                    "parameters": {"CommandLine": "dir"},
                    "output": "synthetic-tool-output\n",
                },
            },
        })
        _emit_result(_state_legal_directive(request), usage=_default_usage())
        return 0
    if scenario == "ask-permission-event":
        _emit_init(argv, tools=["ask_permission"])
        _emit_user_input()
        _emit({
            "event": "step_update",
            "step_update": {
                "step_index": 2,
                "state": "DONE",
                "step_type": "agent_response",
                "tool_info": {
                    "name": "ask_permission",
                    "parameters": {"permission": "command(*)"},
                },
            },
        })
        _emit_result(_state_legal_directive(request), usage=_default_usage())
        return 0
    if scenario in {
        "pretool-hook-tool-attempt",
        "tool-attempt",
        "ask-permission-attempt",
        "ask-question-attempt",
        "list-permissions-attempt",
        "run-command-attempt",
        "view-file-attempt",
        "web-attempt",
        "mcp-attempt",
        "generate-image-attempt",
        "unknown-tool-attempt",
    }:
        default_names = {
            "ask-permission-attempt": "ask_permission",
            "ask-question-attempt": "ask_question",
            "list-permissions-attempt": "list_permissions",
            "run-command-attempt": "run_command",
            "view-file-attempt": "view_file",
            "web-attempt": "search_web",
            "mcp-attempt": "mcp_call",
            "generate-image-attempt": "generate_image",
            "unknown-tool-attempt": "unknown_future_tool",
        }
        tool_name = request.get("synthetic_tool_name")
        if not isinstance(tool_name, str) or not tool_name:
            tool_name = default_names.get(scenario, "run_command")
        return _run_pre_tool_attempt(argv, request, tool_name=tool_name)
    if scenario in {"pretool-hook-subagent-attempt", "subagent-attempt"}:
        return _run_pre_tool_attempt(
            argv,
            request,
            tool_name="invoke_subagent",
            subagent=True,
        )
    if scenario == "subagent-event":
        _emit_init(argv)
        _emit_user_input()
        _emit({
            "event": "step_update",
            "step_update": {
                "step_index": 2,
                "state": "DONE",
                "step_type": "agent_response",
                "subagent_info": {
                    "subagents": [{
                        "type_name": "explore",
                        "role": "explore",
                        "conversation_id": "synthetic-subagent",
                    }]
                },
            },
        })
        _emit_result(_state_legal_directive(request), usage=_default_usage())
        return 0
    if scenario == "wrong-directive-schema":
        _emit_init(argv)
        _emit_user_input()
        _emit_result({"action": "run_reproduction", "params": {"phase": "baseline"}})
        return 0
    if scenario == "illegal-action":
        _emit_init(argv)
        _emit_user_input()
        _emit_result({
            "kind": "action",
            "name": "apply_patch",
            "arguments": {"patch": "--- a\n+++ b\n"},
        })
        return 0
    if scenario == "illegal-transition":
        _emit_init(argv)
        _emit_user_input()
        _emit_result({
            "kind": "transition",
            "target_state": "Done",
            "reason": "illegal jump",
        })
        return 0
    if scenario == "legal-transition":
        _emit_init(argv)
        _emit_user_input()
        _emit_reasoning()
        _emit_result(DIRECTIVE_TRANSITION_UNDERSTAND, usage=_default_usage())
        return 0
    if scenario == "legal-action":
        _emit_init(argv)
        _emit_user_input()
        _emit_reasoning()
        _emit_result(DIRECTIVE_ACTION_BASELINE, usage=_default_usage())
        return 0
    if scenario == "hypothesis-directive":
        _emit_init(argv)
        _emit_user_input()
        _emit_reasoning()
        _emit_result(DIRECTIVE_ADD_HYPOTHESIS, usage=_default_usage())
        return 0
    if scenario == "init-run-command":
        _emit_init(argv, tools=["run_command"])
        _emit_user_input()
        _emit_result(_state_legal_directive(request), usage=_default_usage())
        return 0
    if scenario == "init-file-tool":
        _emit_init(argv, tools=["view_file"])
        _emit_user_input()
        _emit_result(_state_legal_directive(request), usage=_default_usage())
        return 0
    if scenario == "init-web-tool":
        _emit_init(argv, tools=["read_url"])
        _emit_user_input()
        _emit_result(_state_legal_directive(request), usage=_default_usage())
        return 0
    if scenario == "init-empty-tools":
        _emit_init(argv, tools=[])
        _emit_user_input()
        _emit_result(_state_legal_directive(request), usage=_default_usage())
        return 0
    if scenario == "init-ask-permission-only":
        _emit_init(argv, tools=["ask_permission"])
        _emit_user_input()
        _emit_result(_state_legal_directive(request), usage=_default_usage())
        return 0
    if scenario == "init-ask-question-only":
        _emit_init(argv, tools=["ask_question"])
        _emit_user_input()
        _emit_result(_state_legal_directive(request), usage=_default_usage())
        return 0
    if scenario == "init-list-permissions-only":
        _emit_init(argv, tools=["list_permissions"])
        _emit_user_input()
        _emit_result(_state_legal_directive(request), usage=_default_usage())
        return 0
    if scenario == "init-audited-intrinsic-inventory":
        _emit_init(argv, tools=["ask_permission", "ask_question", "list_permissions"])
        _emit_user_input()
        _emit_result(_state_legal_directive(request), usage=_default_usage())
        return 0
    if scenario == "init-unknown-tool":
        _emit_init(argv, tools=["unknown_future_tool"])
        _emit_user_input()
        _emit_result(_state_legal_directive(request), usage=_default_usage())
        return 0
    if scenario == "init-ask-permission-plus-run-command":
        _emit_init(argv, tools=["ask_permission", "run_command"])
        _emit_user_input()
        _emit_result(_state_legal_directive(request), usage=_default_usage())
        return 0
    if scenario == "init-multiple-unapproved-tools":
        _emit_init(argv, tools=["run_command", "view_file", "unknown_future_tool"])
        _emit_user_input()
        _emit_result(_state_legal_directive(request), usage=_default_usage())
        return 0
    if scenario == "unknown-event":
        _emit_init(argv)
        _emit({"event": "mcp_call", "mcp_call": {"server": "web", "tool": "search"}})
        _emit_result(_state_legal_directive(request), usage=_default_usage())
        return 0
    if scenario == "valid-no-usage":
        _emit_init(argv)
        _emit_user_input()
        _emit_reasoning()
        _emit_result(_state_legal_directive(request))
        return 0

    _emit_init(argv)
    _emit_user_input()
    _emit_reasoning()
    _emit_result(_state_legal_directive(request), usage=_default_usage())
    return 0


def _is_network_module(name: str) -> bool:
    top = name.split(".")[0]
    if top in {"socket", "http", "requests", "aiohttp", "httpx"}:
        return True
    if top == "urllib":
        return name not in ("urllib", "urllib.parse")
    return False


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    network_modules = [name for name in sys.modules if _is_network_module(name)]
    if network_modules:
        sys.stderr.write(
            f"synthetic agy refuses to run with network modules loaded: {sorted(network_modules)}\n"
        )
        return 3
    if not args:
        sys.stderr.write("synthetic agy: missing argv\n")
        return 2
    if args[0] == "--version":
        sys.stdout.write(SYNTHETIC_VERSION + "\n")
        return 0
    if args[0] == "models":
        sys.stdout.write(_models_catalog())
        return 0
    if args[0] in {"agent", "agents"}:
        home = os.environ.get("USERPROFILE") or os.environ.get("HOME") or ""
        agent_md = (
            Path(home) / ".gemini" / "config" / "agents"
            / "local-application-decision" / "agent.md"
        )
        if agent_md.is_file():
            sys.stdout.write("local-application-decision\n")
        return 0
    if _has_flag(args, "--print") or _has_flag(args, "-p") or _has_flag(args, "--prompt"):
        return _run_print(args)
    sys.stderr.write(f"synthetic agy: unsupported command {args[0]!r}\n")
    return 2


# ---- fake native executable fixture (test-only) ------------------------------

_FORWARDER_SOURCE = r"""
using System;
using System.Diagnostics;
using System.IO;
using System.Text;
public static class AgyForwarder
{
    private static readonly string Python = {{PYTHON}};
    private static readonly string Script = {{SCRIPT}};

    private static string Quote(string arg)
    {
        var sb = new StringBuilder();
        sb.Append('"');
        for (int i = 0; i < arg.Length; i++)
        {
            char c = arg[i];
            if (c == '\\')
            {
                int run = 0;
                while (i < arg.Length && arg[i] == '\\') { run++; i++; }
                i--;
                bool followedByQuote = i + 1 < arg.Length && arg[i + 1] == '"';
                sb.Append('\\', followedByQuote ? run * 2 : run);
            }
            else if (c == '"')
            {
                sb.Append('\\');
                sb.Append('"');
            }
            else
            {
                sb.Append(c);
            }
        }
        sb.Append('"');
        return sb.ToString();
    }

    public static int Main(string[] args)
    {
        string outFile = Path.GetTempFileName();
        string errFile = Path.GetTempFileName();
        var psi = new ProcessStartInfo();
        psi.FileName = Python;
        psi.UseShellExecute = false;
        psi.RedirectStandardOutput = true;
        psi.RedirectStandardError = true;
        var tail = new StringBuilder();
        tail.Append(Quote(Script));
        foreach (string arg in args) tail.Append(' ').Append(Quote(arg));
        psi.Arguments = tail.ToString();
        int exitCode = 1;
        using (var p = Process.Start(psi))
        {
            string stdout = p.StandardOutput.ReadToEnd();
            string stderr = p.StandardError.ReadToEnd();
            p.WaitForExit();
            exitCode = p.ExitCode;
            try { File.WriteAllText(outFile, stdout); } catch (Exception) { }
            try { File.WriteAllText(errFile, stderr); } catch (Exception) { }
        }
        try { Console.Out.Write(File.ReadAllText(outFile)); } catch (Exception) { }
        try { Console.Error.Write(File.ReadAllText(errFile)); } catch (Exception) { }
        try { File.Delete(outFile); } catch (Exception) { }
        try { File.Delete(errFile); } catch (Exception) { }
        return exitCode;
    }
}
"""

_FORWARDER_CACHE: dict[tuple[str, str], Path] = {}


def _cs_string_literal(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _bounded(value: str, limit: int = 512) -> str:
    value = (value or "").strip()
    return value if len(value) <= limit else value[:limit] + " <truncated>"


def _compile_forwarder(interpreter: str, target_script: str) -> Path:
    key = (interpreter, target_script)
    cached = _FORWARDER_CACHE.get(key)
    if cached is not None and cached.is_file():
        return cached
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if not powershell:
        raise RuntimeError("PowerShell is required to build the fake native AGY executable fixture")
    work = Path(tempfile.mkdtemp(prefix=f"agy-gemini-forwarder-{os.getpid()}-"))
    atexit.register(shutil.rmtree, work, ignore_errors=True)
    source_path = work / "forwarder.cs"
    output = work / "agy.exe"
    source = _FORWARDER_SOURCE.replace("{{PYTHON}}", _cs_string_literal(interpreter)).replace(
        "{{SCRIPT}}", _cs_string_literal(target_script)
    )
    source_path.write_text(source, encoding="utf-8")
    command = (
        f"Add-Type -TypeDefinition (Get-Content -Raw '{source_path}') "
        f"-OutputAssembly '{output}' -OutputType ConsoleApplication"
    )
    completed = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        check=False,
    )
    if completed.returncode != 0 or not output.is_file():
        raise RuntimeError(
            f"fake native AGY executable build failed (rc {completed.returncode}): "
            f"{_bounded(completed.stderr) or _bounded(completed.stdout)}"
        )
    _FORWARDER_CACHE[key] = output
    return output


def build_fake_agy_executable(fake_bin: str | Path, *, target_script: str | Path) -> Path:
    """Create a deterministic fake native ``agy.exe`` in ``fake_bin``."""
    target = Path(fake_bin)
    target.mkdir(parents=True, exist_ok=True)
    compiled = _compile_forwarder(sys.executable, str(Path(target_script).resolve()))
    native = target / "agy.exe"
    if not native.is_file() or native.read_bytes() != compiled.read_bytes():
        shutil.copy2(compiled, native)
    return native


if __name__ == "__main__":
    raise SystemExit(main())
