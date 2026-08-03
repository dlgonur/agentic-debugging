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
* ``models opencode --verbose --pure`` — emits catalog entries for the
  synthetic runtime model identities used by the adapter fixtures (nonzero
  catalog prices in OpenCode Go mode).
* ``debug config --pure`` — echoes the wrapper's isolation configuration
  (``OPENCODE_CONFIG``) plus the empty agent/mode/command keys the wrapper
  expects.
* ``run <message> --pure --format json --model <id> --variant <v>
  --dir <root> --file <request-file>`` — reads the request file written by
  the wrapper from its stdin request and emits protocol-1.3-style JSON event
  lines (a text part carrying the directive plus a ``step_finish`` event
  with tokens/cost) selected by the request's ``synthetic_scenario`` field
  (default ``valid``).

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
* ``malformed-always`` — every attempt emits malformed text.
* ``timeout`` — sleeps far beyond the wrapper call timeout.
* ``timeout-with-child`` — sleeps and spawns a child process that keeps
  writing a marker file; the transport's process-group cleanup must
  terminate the tree.
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

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

#: Catalog entries for the synthetic runtime model identities used by the
#: adapter fixtures.  Catalog prices are NONZERO in OpenCode Go mode.
SYNTHETIC_MODEL_IDS = (
    "test-deepseek-v4-flash",
    "synthetic-deepseek-v4-flash",
)
SYNTHETIC_VERSION = "1.0.0"
SYNTHETIC_PROVIDER = "opencode-go"

DIRECTIVE_STOP = {"kind": "stop", "reason": "synthetic-success"}


def _catalog() -> str:
    entries = [
        {
            "id": model_id,
            "providerID": SYNTHETIC_PROVIDER,
            "status": "active",
            "cost": {"input": 0.5, "output": 1.5, "cache": {"read": 0.25, "write": 0.25}},
            "variants": {"max": {"reasoningEffort": "max"}},
        }
        for model_id in SYNTHETIC_MODEL_IDS
    ]
    return "\n".join(json.dumps(entry, ensure_ascii=False) for entry in entries) + "\n"


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


def _request_from_file(path: str | None) -> dict[str, Any]:
    if path is None or not Path(path).is_file():
        return {}
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
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


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])

    # Network-incapable guard: refuse to run in an interpreter that already
    # imported network modules.  The synthetic executable itself never
    # imports them.
    network_modules = [name for name in sys.modules if name.split(".")[0] in {"socket", "http", "urllib", "requests", "aiohttp", "httpx"}]
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
        return _run_behavior(_request_from_file(_argv_value(args, "--file")))
    sys.stderr.write(f"synthetic opencode: unsupported command {args[0]!r}\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
