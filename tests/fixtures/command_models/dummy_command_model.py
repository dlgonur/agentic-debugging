"""Durable test fixture: a scripted JSON-lines command model (Task 8).

This is a legitimate durable test fixture for the configured command-model
execution path -- it is NOT a product model implementation.  It exercises
the real subprocess/protocol path of the accepted JSON-lines command
transport (one bounded JSON request on stdin, one JSON directive object on
stdout) and can drive the real controller to completion by mirroring the
accepted deterministic demonstration model's directive sequence.

Modes (first argv argument):

- ``valid`` (default): stateful scripted model that drives the controller
  through Reproduce -> Understand -> (RuntimeEvidence) -> Patch -> Validate
  -> Done using a sidecar state file (``--state-dir``) and task data
  (``--data`` JSON with symbol/file/hypothesis/patch-file/expressions);
- ``malformed``: emits invalid JSON;
- ``malformed_secret``: emits invalid JSON containing a credential-shaped
  literal (the application must never persist or render it);
- ``noise``: emits extra output lines before the JSON response (the
  response is then not a single JSON document);
- ``empty``: emits nothing (unexpected EOF / empty output);
- ``invalid_directive``: valid JSON with an unknown directive kind;
- ``illegal_action``: valid JSON with an action that is not legal in the
  current controller state (the controller must reject it, never execute
  it);
- ``stderr``: writes a line to stderr, then behaves like ``valid``;
- ``secret_on_stderr``: writes a credential-shaped line to stderr (the
  application must never persist or render it);
- ``fail``: exits non-zero without a response;
- ``flood_stdout`` / ``flood_stderr``: writes megabytes of noise (bounded
  output handling);
- ``slow``: sleeps ``--delay`` seconds, then behaves like ``valid``
  (request-timeout tests);
- ``sleep_forever``: never responds and never exits;
- ``spawn_child``: spawns a descendant ``sleep`` process (which itself
  spawns a grandchild), records both pids in ``--child-pid-file`` /
  ``--grandchild-pid-file``, then behaves like ``valid`` (process-tree
  tests);
- ``hang_on_stdin``: never reads stdin and never exits (stdin write
  timeout).

The fixture never reads or writes anything outside its own args; child
processes use the same interpreter explicitly (``sys.executable -c ...``),
never a shell string.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

MODE = sys.argv[1] if len(sys.argv) > 1 else "valid"


def _arg(name: str, default: str = "") -> str:
    for index, item in enumerate(sys.argv):
        if item == name and index + 1 < len(sys.argv):
            return sys.argv[index + 1]
    return default


def _read_request() -> dict:
    line = sys.stdin.buffer.readline()
    if not line:
        return {}
    return json.loads(line.decode("utf-8"))


def _emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def _read_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as stream:
        return json.load(stream)


def _load_data() -> dict:
    data_path = _arg("--data")
    if not data_path:
        return {}
    return _read_json(data_path)


def _load_phase(state_dir: str) -> str:
    path = Path(state_dir) / "phase.json"
    if not path.is_file():
        return "reproduce"
    return _read_json(str(path)).get("phase", "reproduce")


def _save_phase(state_dir: str, phase: str) -> None:
    path = Path(state_dir) / "phase.json"
    path.write_text(json.dumps({"phase": phase}), encoding="utf-8")


def _last_observation(request: dict) -> dict:
    controller = request.get("controller") or {}
    observation = controller.get("last_observation")
    return observation if isinstance(observation, dict) else {}


def _observation_ok(request: dict) -> bool:
    observation = _last_observation(request)
    status = observation.get("status")
    return status == "ok"


def _pause_generation(request: dict) -> int:
    payload = _last_observation(request).get("payload")
    generation = payload.get("pause_generation") if isinstance(payload, dict) else None
    return generation if type(generation) is int and generation > 0 else 1


def _legal_targets(request: dict) -> set:
    controller = request.get("controller") or {}
    targets = controller.get("legal_transition_targets")
    return set(targets) if isinstance(targets, list) else set()


def _respond_valid(request: dict, data: dict, state_dir: str) -> None:
    phase = _load_phase(state_dir)
    state = (request.get("controller") or {}).get("state")
    if state == "Reproduce":
        if phase == "reproduce":
            _save_phase(state_dir, "reproduce-check")
            _emit({"kind": "action", "name": "run_reproduction", "arguments": {"phase": "baseline"}})
            return
        _save_phase(state_dir, "understand-locate")
        _emit({"kind": "transition", "target_state": "Understand", "reason": "failure reproduced"})
        return
    if state == "Understand":
        if phase == "understand-locate":
            _save_phase(state_dir, "understand-window")
            _emit({
                "kind": "action",
                "name": "find_function",
                "arguments": {"name": data.get("symbol", "recent_window"), "path": data.get("file", "recent_window.py")},
            })
            return
        if phase == "understand-window":
            if not _observation_ok(request):
                _emit({"kind": "transition", "target_state": "Failed", "reason": "symbol not located"})
                return
            _save_phase(state_dir, "understand-hypothesis")
            _emit({
                "kind": "action",
                "name": "get_source_window",
                "arguments": {"path": data.get("file", "recent_window.py"), "line": 1},
            })
            return
        if phase == "understand-hypothesis":
            _save_phase(state_dir, "understand-declare")
            _emit({
                "kind": "add_hypothesis",
                "hypothesis_id": data.get("hypothesis_id", "h1"),
                "statement": data.get("statement", "candidate defect"),
                "confidence": "low",
                "evidence_refs": ["observation:get_source_window"],
                "requires_runtime_evidence": True,
            })
            return
        if phase == "understand-declare":
            _save_phase(state_dir, "understand-gate")
            _emit({
                "kind": "action",
                "name": "express_root_cause_hypothesis",
                "arguments": {
                    "hypothesis_id": data.get("hypothesis_id", "h1"),
                    "statement": data.get("statement", "candidate defect"),
                    "target_file": data.get("file", "recent_window.py"),
                    "target_symbol": data.get("symbol", "recent_window"),
                    "confidence": "low",
                },
            })
            return
        if phase == "understand-gate":
            if "RuntimeEvidence" in _legal_targets(request):
                _save_phase(state_dir, "runtime-start")
                _emit({"kind": "transition", "target_state": "RuntimeEvidence", "reason": "runtime evidence allowed"})
            else:
                _save_phase(state_dir, "patch-apply")
                _emit({"kind": "transition", "target_state": "Patch", "reason": "runtime evidence withheld"})
            return
        if phase == "understand-revise":
            _save_phase(state_dir, "understand-runtime-collected")
            _emit({
                "kind": "revise_hypothesis",
                "hypothesis_id": data.get("hypothesis_id", "h1"),
                "statement": data.get("statement", "candidate defect"),
                "confidence": "low",
                "evidence_refs": ["observation:get_source_window", "observation:get_stack_summary"],
                "requires_runtime_evidence": False,
            })
            return
        if phase == "understand-runtime-collected":
            _save_phase(state_dir, "patch-apply")
            _emit({"kind": "transition", "target_state": "Patch", "reason": "runtime evidence collected"})
            return
    if state == "RuntimeEvidence":
        if phase == "runtime-start":
            _save_phase(state_dir, "runtime-stack")
            _emit({"kind": "action", "name": "start_pdb_session", "arguments": {}})
            return
        if phase == "runtime-stack":
            _save_phase(state_dir, "runtime-locals")
            _emit({"kind": "action", "name": "get_stack_summary", "arguments": {}})
            return
        if phase == "runtime-locals":
            _save_phase(state_dir, "runtime-eval")
            _emit({
                "kind": "action",
                "name": "get_frame_locals",
                "arguments": {"frame_id": 0, "pause_generation": _pause_generation(request)},
            })
            return
        if phase == "runtime-eval":
            _save_phase(state_dir, "runtime-exit")
            expressions = data.get("expressions") or []
            _emit({
                "kind": "action",
                "name": "safe_eval_expression",
                "arguments": {
                    "frame_id": 0,
                    "pause_generation": _pause_generation(request),
                    "expression": expressions[0] if expressions else "len(days)",
                },
            })
            return
        if phase == "runtime-exit":
            _save_phase(state_dir, "understand-revise")
            _emit({"kind": "transition", "target_state": "Understand", "reason": "runtime evidence collected"})
            return
    if state == "Patch":
        if phase == "patch-apply":
            patch_file = data.get("patch_file", "")
            patch_text = Path(patch_file).read_text(encoding="utf-8") if patch_file else ""
            _save_phase(state_dir, "patch-syntax")
            _emit({"kind": "action", "name": "apply_patch", "arguments": {"patch": patch_text}})
            return
        if phase == "patch-syntax":
            _save_phase(state_dir, "patch-validate")
            _emit({"kind": "action", "name": "syntax_check", "arguments": {}})
            return
        if phase == "patch-validate":
            _save_phase(state_dir, "validate-reproduce")
            _emit({"kind": "transition", "target_state": "Validate", "reason": "patch applied and syntax checked"})
            return
    if state == "Validate":
        if phase == "validate-reproduce":
            _save_phase(state_dir, "validate-regression")
            _emit({"kind": "action", "name": "run_reproduction", "arguments": {"phase": "post_patch"}})
            return
        if phase == "validate-regression":
            _save_phase(state_dir, "validate-classify")
            _emit({"kind": "action", "name": "run_regression_tests", "arguments": {}})
            return
        if phase == "validate-classify":
            _save_phase(state_dir, "validate-finish")
            _emit({"kind": "action", "name": "classify_outcome", "arguments": {}})
            return
        if phase == "validate-finish":
            payload = _last_observation(request).get("payload")
            outcome = payload.get("outcome") if isinstance(payload, dict) else None
            if outcome == "RESOLVED":
                _emit({"kind": "transition", "target_state": "Done", "reason": "candidate resolved"})
            else:
                _emit({"kind": "transition", "target_state": "Failed", "reason": f"outcome {outcome}"})
            return
    # Unknown phase/state combination: fail closed with a legal transition.
    _emit({"kind": "transition", "target_state": "Failed", "reason": "fixture state mismatch"})


def _spawn_descendant() -> None:
    """Spawn a child that itself spawns a grandchild (tree-kill depth).

    The child sleeps forever and records the grandchild's pid; the
    grandchild is detached from the child's stdout/stderr so it cannot be
    reached by closing the child's pipes.  Both pids are recorded for the
    process-tree assertions.
    """
    grandchild_pid_file = _arg("--grandchild-pid-file")
    grandchild_code = "import time; time.sleep(3600)"
    child_code = (
        "import subprocess, sys, time; "
        f"p = subprocess.Popen([sys.executable, '-c', {grandchild_code!r}], "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, shell=False); "
        f"open({str(grandchild_pid_file)!r}, 'w').write(str(p.pid)); "
        "time.sleep(3600)"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", child_code],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False,
    )
    pid_file = _arg("--child-pid-file")
    if pid_file:
        Path(pid_file).write_text(str(child.pid), encoding="utf-8")


def main() -> int:
    if MODE == "malformed":
        sys.stdout.write("{not-json}\n")
        sys.stdout.flush()
        return 0
    if MODE == "malformed_secret":
        sys.stdout.write('{"secret": "sk-live-malformed-9c41f2", "kind":')
        sys.stdout.flush()
        return 0
    if MODE == "noise":
        sys.stdout.write("log line before the response\n")
        sys.stdout.flush()
        return 0
    if MODE == "empty":
        return 0
    if MODE == "invalid_directive":
        _emit({"kind": "not_a_real_directive_kind", "payload": 1})
        return 0
    if MODE == "illegal_action":
        # apply_patch is never legal in the initial Reproduce state; the
        # controller must reject it instead of executing it.
        _emit(
            {
                "kind": "action",
                "name": "apply_patch",
                "arguments": {"patch": "--- a/x\n+++ b/x\n"},
            }
        )
        return 0
    if MODE == "fail":
        return 3
    if MODE == "flood_stdout":
        sys.stdout.write("x" * (20 * 1024 * 1024))
        sys.stdout.flush()
        return 0
    if MODE == "flood_stderr":
        sys.stderr.write("y" * (20 * 1024 * 1024))
        sys.stderr.flush()
        return 0
    if MODE == "sleep_forever":
        time.sleep(3600)
        return 0
    if MODE == "hang_on_stdin":
        pid_file = _arg("--pid-file")
        if pid_file:
            Path(pid_file).write_text(str(os.getpid()), encoding="utf-8")
        time.sleep(3600)
        return 0
    if MODE == "slow":
        time.sleep(float(_arg("--delay", "5")))
    if MODE == "stderr" or MODE == "secret_on_stderr":
        text = "secret token: sk-abc123def456" if MODE == "secret_on_stderr" else "diagnostic line"
        sys.stderr.write(text + "\n")
        sys.stderr.flush()
    if MODE == "spawn_child":
        _spawn_descendant()
        delay = _arg("--delay")
        if delay:
            time.sleep(float(delay))
    if MODE in ("valid", "stderr", "secret_on_stderr", "slow", "spawn_child"):
        request = _read_request()
        state_dir = _arg("--state-dir")
        if not state_dir:
            raise SystemExit("dummy command model requires --state-dir")
        Path(state_dir).mkdir(parents=True, exist_ok=True)
        _respond_valid(request, _load_data(), state_dir)
        return 0
    raise SystemExit(f"unknown dummy command model mode: {MODE}")


if __name__ == "__main__":
    sys.exit(main())
