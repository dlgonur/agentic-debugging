#!/usr/bin/env python3
"""Deterministic synthetic OpenCode CLI (test-only; runs behind the real protocol wrapper).

This module is the fake ``opencode.cmd`` invoked by the accepted protocol
wrapper (:mod:`opencode_protocol_transport.py`) inside the OpenCode Go
execution adapter's tests and self-test mode.  It replaces only the OpenCode
CLI: the real wrapper still performs protocol conversion, isolation,
directive extraction, usage parsing, redaction, and evidence handling, and
the wrapper's stdin contract (one JSON protocol request line) is exercised
end-to-end.

Contract (mirrors the real ``opencode`` CLI surface used by the wrapper):

* ``--version`` — emits the synthetic OpenCode version.
* ``models opencode-go --verbose --pure`` — emits catalog entries for the
  synthetic runtime model identities used by the adapter fixtures (nonzero
  catalog prices in OpenCode Go mode).
* ``debug config --pure`` — echoes the wrapper's isolation configuration
  (``OPENCODE_CONFIG``) plus the empty agent/mode/command keys the wrapper
  expects.
* ``run <message> --pure --format json --model <id> --variant <v>
  --dir <root>`` — recovers the canonical public request from the inline
  message (between the exact ``=== BEGIN PUBLIC REQUEST ===`` /
  ``=== END PUBLIC REQUEST ===`` delimiters the real protocol wrapper writes)
  and emits protocol-1.3-style JSON event lines (a text part carrying the
  directive plus a ``step_finish`` event with tokens/cost) selected by the
  request's ``synthetic_scenario`` field (default ``valid``).

Properties (enforced by the scenario table):

* test-only: never contacts a network endpoint, model provider, catalog,
  account, or entitlement service; imports only stdlib modules and never
  opens a socket;
* deterministic: every scenario depends only on the request file content and
  the argv, never on wall-clock time or external state;
* network-incapable: refuses to run in an interpreter that already loaded
  network modules.

Scenarios (``synthetic_scenario`` in the request file):

* ``valid`` — directive plus finite tokens/cost (0.0042).
* ``valid-no-usage`` — directive only; no usage or cost at all.
* ``valid-usage`` — directive plus explicit finite tokens/cost (0.0042).
* ``cost-zero`` — directive plus an explicitly reported zero cost.
* ``malformed-then-valid`` — first attempt (request carries
  ``directive_feedback: null``) emits malformed text; later attempts emit a
  valid directive.
* ``state-legal`` — emits the legal protocol directive for the request's
  ``controller.state``: an action in ``Reproduce``, an add_hypothesis
  directive in ``Understand``, a revise_hypothesis directive in
  ``RuntimeEvidence``, and a stop directive otherwise.
* ``copied-request-plus-valid`` — copies the entire embedded request JSON
  into the output and appends one valid directive, exercising the wrapper's
  schema-aware extraction (the copied request object must fail directive
  validation, never heuristic key stripping).
* ``tool-call-text`` — emits DSML tool-call text that tries to read the
  request file, plus one valid directive; the wrapper must reject the tool
  call text and accept the single valid directive.
* ``malformed-always`` — every attempt emits malformed text.
* ``timeout`` — sleeps far beyond the wrapper call timeout.
* ``timeout-with-child`` — sleeps and spawns a child process that keeps
  writing a marker file; the transport's process-group cleanup must
  terminate the tree.
* ``external-cancel-tree`` — spawns a child which spawns a grandchild, writes
  a deterministic readiness marker (adapter pid, opencode pid, child pid,
  grandchild pid) into the current working directory, then sleeps far beyond
  any request budget; the OUTER transport's external cancellation must
  terminate the adapter AND the whole detached tree.
* ``auth-env-probe`` — emits a boolean-only probe event reporting whether the
  ``OPENCODE_AUTH_CONTENT`` environment value was injected and matches the
  request's ``synthetic_marker`` field, then emits one valid directive; the
  adapter passes the probe event through untouched (it carries no secret
  bytes) while the marker itself must never appear in any adapter-owned
  artifact.
* ``oversized`` — floods stdout far beyond the wrapper's bounded capture.
* ``nonzero-exit`` — writes a bounded stderr diagnostic and exits 7.
* ``startup-failure`` — exits 1 before reading anything.
* ``identity-mismatch`` — valid directive but telemetry declares a different
  runtime model identity (passed through the wrapper's telemetry).
* ``route-drift`` — telemetry declares billing route ``ZEN``.
* ``free-tier-drift`` — telemetry declares billing route ``FREE_TIER``.
* ``model-substitution-drift`` — telemetry declares a model-substitution
  marker.
* ``nonfinite-usage`` — usage carries a literal ``NaN`` value that the
  adapter must reject.
* ``credential-output`` — stderr carries a credential-shaped line that the
  wrapper's evidence must redact.

Usage (as spawned by the wrapper through the ``opencode.cmd`` shim):

    python opencode_go_synthetic_executable.py <opencode argv...>
"""
from __future__ import annotations

import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

#: Catalog entries for the synthetic runtime model identities used by the
#: adapter fixtures.  Catalog prices are NONZERO in OpenCode Go mode.
#: Exposed as a module constant so the adapter self-test and the test
#: fixtures can compute the exact deterministic catalog-entry fingerprint
#: the real wrapper independently recomputes during its OpenCode Go
#: preflight.
SYNTHETIC_MODEL_IDS = (
    "test-deepseek-v4-flash",
    "synthetic-deepseek-v4-flash",
    "deepseek-v4-pro",
    "test-deepseek-v4-pro",
)
SYNTHETIC_VERSION = "1.0.0"
SYNTHETIC_PROVIDER = "opencode-go"
SYNTHETIC_CATALOG_ENTRIES = [
    {
        "id": model_id,
        "providerID": SYNTHETIC_PROVIDER,
        "status": "active",
        "cost": {"input": 0.5, "output": 1.5, "cache": {"read": 0.25, "write": 0.25}},
        "variants": {"max": {"reasoningEffort": "max"}},
    }
    for model_id in SYNTHETIC_MODEL_IDS
]

#: The exact inline-request delimiters the real protocol wrapper writes into
#: the OpenCode user message (:data:`opencode_protocol_transport.PUBLIC_REQUEST_START`
#: / ``..._END``); the synthetic CLI recovers the request from the message,
#: never from a file, mirroring the real model-facing contract.
PUBLIC_REQUEST_START = "=== BEGIN PUBLIC REQUEST ==="
PUBLIC_REQUEST_END = "=== END PUBLIC REQUEST ==="

DIRECTIVE_STOP = {"kind": "stop", "reason": "synthetic-success"}

DIRECTIVE_ACTION_BASELINE = {"kind": "action", "name": "run_reproduction", "arguments": {"phase": "baseline"}}
DIRECTIVE_ADD_HYPOTHESIS = {
    "kind": "add_hypothesis",
    "hypothesis_id": "h-1",
    "statement": "synthetic root-cause hypothesis",
    "confidence": "medium",
    "evidence_refs": [],
    "requires_runtime_evidence": False,
}
DIRECTIVE_REVISE_HYPOTHESIS = {
    "kind": "revise_hypothesis",
    "hypothesis_id": "h-1",
    "statement": "synthetic revised root-cause hypothesis",
    "confidence": "high",
    "evidence_refs": ["obs-1"],
    "requires_runtime_evidence": True,
}


def _catalog() -> str:
    return "\n".join(json.dumps(entry, ensure_ascii=False) for entry in SYNTHETIC_CATALOG_ENTRIES) + "\n"


def _effective_config() -> str:
    config_path = os.environ.get("OPENCODE_CONFIG")
    if config_path and Path(config_path).is_file():
        config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    else:
        config = {}
    config = dict(config)
    config.update({"agent": {}, "mode": {}, "command": {}})
    return json.dumps(config, ensure_ascii=False, sort_keys=True)


def _argv_value(argv: list[str], flag: str) -> str | None:
    for index, item in enumerate(argv):
        if item == flag and index + 1 < len(argv):
            return argv[index + 1]
    return None


def _emit_text(text: str) -> None:
    sys.stdout.write(json.dumps({"type": "text", "part": {"text": text}}, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _emit_step_finish(extra: dict[str, Any]) -> None:
    part: dict[str, Any] = {}
    if "tokens" in extra:
        part["tokens"] = extra["tokens"]
    if "cost" in extra:
        part["cost"] = extra["cost"]
    for key in ("observed_model", "observed_billing_route", "observed_model_substitution"):
        if key in extra:
            part[key] = extra[key]
    sys.stdout.write(json.dumps({"type": "step_finish", "part": part}, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _state_legal_directive(request: dict[str, Any]) -> dict[str, Any]:
    """The legal protocol directive for the request's current controller
    state; used by the ``state-legal`` synthetic scenario."""
    controller = request.get("controller")
    state = controller.get("state") if isinstance(controller, dict) else None
    contracts = request.get("action_contracts")
    if state == "Reproduce" and isinstance(contracts, dict) and "run_reproduction" in contracts:
        return DIRECTIVE_ACTION_BASELINE
    if state == "Understand":
        return DIRECTIVE_ADD_HYPOTHESIS
    if state == "RuntimeEvidence":
        return DIRECTIVE_REVISE_HYPOTHESIS
    return DIRECTIVE_STOP


def _run_behavior(request: dict[str, Any]) -> int:
    scenario = request.get("synthetic_scenario")
    scenario = scenario if isinstance(scenario, str) else "valid"
    if scenario == "startup-failure":
        sys.stderr.write("synthetic opencode: startup failure injected\n")
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
    if scenario == "auth-env-probe":
        marker = request.get("synthetic_marker")
        marker = marker if isinstance(marker, str) else ""
        auth_content = os.environ.get("OPENCODE_AUTH_CONTENT", "")
        sys.stdout.write(
            json.dumps({
                "type": "auth_env_probe",
                "part": {
                    "auth_env_present": "OPENCODE_AUTH_CONTENT" in os.environ,
                    "auth_env_matches_marker": bool(marker) and marker in auth_content,
                },
            }, ensure_ascii=False)
            + "\n"
        )
        sys.stdout.flush()
        _emit_text(json.dumps(_state_legal_directive(request), ensure_ascii=False))
        _emit_step_finish({"tokens": {"input": 11, "output": 5}, "cost": 0.0042})
        return 0
    if scenario == "oversized":
        sys.stdout.write("x" * 8_000_000 + "\n")
        sys.stdout.flush()
        return 0
    if scenario == "nonzero-exit":
        sys.stderr.write("synthetic opencode diagnostic on stderr\n")
        return 7
    if scenario == "credential-output":
        sys.stderr.write("api_key=super-secret-synthetic-value\n")
        _emit_text(json.dumps(DIRECTIVE_STOP, ensure_ascii=False))
        return 0
    if scenario == "malformed-always":
        _emit_text("this is not a directive")
        return 0
    if scenario == "malformed-then-valid":
        if request.get("directive_feedback") is None:
            _emit_text("malformed first transport attempt")
            return 0
        _emit_text(json.dumps(DIRECTIVE_STOP, ensure_ascii=False))
        _emit_step_finish({"tokens": {"input": 11, "output": 5}, "cost": 0.00123})
        return 0
    if scenario == "valid-no-usage":
        _emit_text(json.dumps(DIRECTIVE_STOP, ensure_ascii=False))
        return 0
    if scenario == "cost-zero":
        _emit_text(json.dumps(DIRECTIVE_STOP, ensure_ascii=False))
        _emit_step_finish({"tokens": {"input": 11, "output": 5}, "cost": 0.0})
        return 0
    if scenario == "state-legal":
        _emit_text(json.dumps(_state_legal_directive(request), ensure_ascii=False))
        _emit_step_finish({"tokens": {"input": 11, "output": 5}, "cost": 0.0042})
        return 0
    if scenario == "copied-request-plus-valid":
        _emit_text(json.dumps(request, ensure_ascii=False) + "\n" + json.dumps(_state_legal_directive(request), ensure_ascii=False))
        _emit_step_finish({"tokens": {"input": 11, "output": 5}, "cost": 0.0042})
        return 0
    if scenario == "tool-call-text":
        _emit_text(
            'Let me read the request file.\n<||DSML||tool_calls>\n'
            '<||DSML||invoke name="Bash">\n'
            '<||DSML||parameter name="command" string="true">type public-request.json</||DSML||parameter>\n'
            "</||DSML||invoke>\n</||DSML||tool_calls>\n"
            + json.dumps(_state_legal_directive(request), ensure_ascii=False)
        )
        _emit_step_finish({"tokens": {"input": 11, "output": 5}, "cost": 0.0042})
        return 0
    if scenario == "identity-mismatch":
        _emit_text(json.dumps(DIRECTIVE_STOP, ensure_ascii=False))
        _emit_step_finish({"tokens": {"input": 11, "output": 5}, "cost": 0.0, "observed_model": "opencode-go/some-other-model"})
        return 0
    if scenario == "route-drift":
        _emit_text(json.dumps(DIRECTIVE_STOP, ensure_ascii=False))
        _emit_step_finish({"tokens": {"input": 11, "output": 5}, "cost": 0.0, "observed_billing_route": "ZEN"})
        return 0
    if scenario == "free-tier-drift":
        _emit_text(json.dumps(DIRECTIVE_STOP, ensure_ascii=False))
        _emit_step_finish({"tokens": {"input": 11, "output": 5}, "cost": 0.0, "observed_billing_route": "FREE_TIER"})
        return 0
    if scenario == "model-substitution-drift":
        _emit_text(json.dumps(DIRECTIVE_STOP, ensure_ascii=False))
        _emit_step_finish({"tokens": {"input": 11, "output": 5}, "cost": 0.0, "observed_model_substitution": True})
        return 0
    if scenario == "nonfinite-usage":
        _emit_text(json.dumps(DIRECTIVE_STOP, ensure_ascii=False))
        sys.stdout.write(
            json.dumps({"type": "step_finish", "part": {"tokens": {"input": 11, "output": 5}, "cost": 0.00123}}, ensure_ascii=False)
            .replace('"input": 11', '"input": NaN')
            + "\n"
        )
        sys.stdout.flush()
        return 0
    _emit_text(json.dumps(DIRECTIVE_STOP, ensure_ascii=False))
    _emit_step_finish({"tokens": {"input": 11, "output": 5}, "cost": 0.0042})
    return 0


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
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _spawn_child(seconds: float) -> int:
    marker_root = os.environ.get("USERPROFILE") or os.environ.get("HOME") or tempfile.gettempdir()
    marker = Path(marker_root) / f"opencode-go-synthetic-child-{os.getpid()}.json"
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
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    ).pid
    # Also record the child identity in a marker inside the fake's own
    # working directory (the wrapper isolation root) so the test can
    # discover it deterministically.
    try:
        (Path.cwd() / f"opencode-go-synthetic-child-{pid}.json").write_text(
            json.dumps({"child_pid": pid}), encoding="utf-8"
        )
    except OSError:
        pass
    return pid


def _spawn_external_cancel_tree() -> int:
    """Spawn a child which spawns a grandchild, then block far beyond any
    request budget.

    The readiness marker (written once by the child into the current working
    directory) carries the adapter pid (the synthetic process's parent), the
    synthetic opencode pid, the child pid, and the grandchild pid, so the
    outer-transport cancellation test can wait for an explicit readiness/PID
    marker and then assert adapter/child/grandchild are all dead.  All three
    descendants stay inside the synthetic opencode's process group on POSIX
    (the child never calls setsid), so the adapter's external-cancellation
    handler and the transport's tree termination both cover them.
    """
    marker = Path.cwd() / f"opencode-go-synthetic-tree-{os.getpid()}.json"
    adapter_pid = os.getppid()
    opencode_pid = os.getpid()
    child_code = (
        "import json,sys,time,os,pathlib,subprocess\n"
        "marker = sys.argv[1]\n"
        "code = 'import time,sys; end = time.monotonic() + float(sys.argv[1]); '\n"
        "code += 'while time.monotonic() < end: time.sleep(0.15)'\n"
        "grandchild = subprocess.Popen([sys.executable, '-c', code, '150'],\n"
        "                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        f"pathlib.Path(marker).write_text(json.dumps({{\n"
        f"    'adapter_pid': {adapter_pid},\n"
        f"    'opencode_pid': {opencode_pid},\n"
        f"    'child_pid': os.getpid(),\n"
        f"    'grandchild_pid': grandchild.pid,\n"
        f"}}), encoding='utf-8')\n"
        "end = time.monotonic() + 150.0\n"
        "while time.monotonic() < end:\n"
        "    time.sleep(0.15)\n"
    )
    pid = subprocess.Popen(
        [sys.executable, "-c", child_code, str(marker)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    ).pid
    return pid


def _is_network_module(name: str) -> bool:
    """Whether a loaded module name represents a real network capability.

    ``socket``, ``http``, ``requests``, ``aiohttp``, and ``httpx`` always
    are.  The ``urllib`` package namespace and its passive ``urllib.parse``
    parser are not (they cannot open a connection and are present at
    interpreter startup on some platform builds); every other ``urllib.*``
    submodule is a network capability.
    """
    top = name.split(".")[0]
    if top in {"socket", "http", "requests", "aiohttp", "httpx"}:
        return True
    if top == "urllib":
        return name not in ("urllib", "urllib.parse")
    return False


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])

    # Network-incapable guard: refuse to run in an interpreter that already
    # imported network modules.  The synthetic executable itself never
    # imports them.  ``urllib``/``urllib.parse`` are passive: the bare
    # package namespace and the parser are present at interpreter startup on
    # some platform builds (``pathlib`` imports ``urllib.parse`` on
    # Python 3.10) and can never open a connection, so they are not network
    # capabilities; ``urllib.request`` and the other submodules are.
    network_modules = [
        name
        for name in sys.modules
        if _is_network_module(name)
    ]
    if network_modules:
        sys.stderr.write(f"synthetic opencode refuses to run with network modules loaded: {sorted(network_modules)}\n")
        return 3

    if not args:
        sys.stderr.write("synthetic opencode: missing argv\n")
        return 2
    if args[0] == "--version":
        sys.stdout.write(SYNTHETIC_VERSION + "\n")
        return 0
    if args[0] == "models":
        sys.stdout.write(_catalog())
        return 0
    if args[0] == "debug":
        sys.stdout.write(_effective_config())
        return 0
    if args[0] == "run":
        return _run_behavior(_request_from_message(_argv_value(args, "run")))
    sys.stderr.write(f"synthetic opencode: unsupported command {args[0]!r}\n")
    return 2


# ---- fake native executable fixture (test-only) ------------------------------
#
# The real protocol wrapper resolves and invokes the native ``opencode.exe``
# directly for ``opencode run`` (bypassing the cmd.exe batch shim).  Test
# fixtures and the adapter self-test therefore need a deterministic fake
# native ``opencode.exe``: a tiny compiled console forwarder that invokes
# ``<interpreter> <target_script> <args...>`` with no shell and propagates
# stdout/stderr and the exit code.  The forwarder is compiled once per
# (interpreter, target_script) pair per session via PowerShell Add-Type and
# copied into each fake launcher directory, so the real wrapper's
# native-executable resolution, version proof, and model-execution invocation
# run against a deterministic fake native executable.

_FORWARDER_SOURCE = r"""
using System;
using System.Diagnostics;
using System.IO;
using System.Text;
public static class OpenCodeForwarder
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


def _compile_forwarder(interpreter: str, target_script: str) -> Path:
    """Compile (once per session per interpreter/script pair) the fake native
    executable forwarder via PowerShell Add-Type; returns the compiled exe."""
    import shutil as _shutil

    key = (interpreter, target_script)
    cached = _FORWARDER_CACHE.get(key)
    if cached is not None and cached.is_file():
        return cached
    # Windows PowerShell 5.1 (``powershell.exe``) supports Add-Type
    # -OutputType ConsoleApplication; prefer it over PowerShell 7 (whose
    # Add-Type -OutputType is unsupported on newer .NET).
    powershell = _shutil.which("powershell") or _shutil.which("pwsh")
    if not powershell:
        raise RuntimeError("PowerShell is required to build the fake native executable fixture")
    # Every cache key needs its own assembly path.  Reusing one per-process
    # output path caused PowerShell Add-Type to retain the first compiled
    # target while later cache keys pointed at that stale executable.  It also
    # allowed a reused OS PID to inherit an abandoned build directory.  A
    # unique owned directory makes the on-disk artifact agree with the
    # in-memory (interpreter, target_script) key.
    work = Path(tempfile.mkdtemp(prefix=f"opencode-go-forwarder-{os.getpid()}-"))
    atexit.register(shutil.rmtree, work, ignore_errors=True)
    source_path = work / "forwarder.cs"
    output = work / "opencode.exe"
    source = _FORWARDER_SOURCE.replace("{{PYTHON}}", _cs_string_literal(interpreter)).replace("{{SCRIPT}}", _cs_string_literal(target_script))
    source_path.write_text(source, encoding="utf-8")
    command = (
        f"Add-Type -TypeDefinition (Get-Content -Raw '{source_path}') "
        f"-OutputAssembly '{output}' -OutputType ConsoleApplication"
    )
    completed = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
        errors="replace", timeout=180, check=False,
    )
    if completed.returncode != 0 or not output.is_file():
        raise RuntimeError(
            f"fake native executable build failed (rc {completed.returncode}): "
            f"{_bounded(completed.stderr) or _bounded(completed.stdout)}"
        )
    _FORWARDER_CACHE[key] = output
    return output


def _bounded(value: str, limit: int = 512) -> str:
    value = (value or "").strip()
    return value if len(value) <= limit else value[:limit] + " <truncated>"


def build_fake_native_executable(fake_bin: str | Path, *, target_script: str | Path) -> Path:
    """Create a deterministic fake native ``opencode.exe`` in ``fake_bin``.

    The compiled forwarder invokes ``<sys.executable> <target_script> <args...>``
    with no shell and propagates stdout/stderr and the exit code, so it serves
    the exact synthetic CLI surface (``--version``, ``models ...``,
    ``debug config --pure``, ``run <message> ...``).  Test-only.
    """
    target = Path(fake_bin)
    target.mkdir(parents=True, exist_ok=True)
    compiled = _compile_forwarder(sys.executable, str(Path(target_script).resolve()))
    native = target / "opencode.exe"
    if not native.is_file() or native.read_bytes() != compiled.read_bytes():
        shutil.copy2(compiled, native)
    return native


if __name__ == "__main__":
    raise SystemExit(main())
