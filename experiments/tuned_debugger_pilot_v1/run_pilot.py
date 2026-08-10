#!/usr/bin/env python3
"""Frozen 5x2 tuned-model interactive-debugger pilot.

The existing live controller owns orchestration and the existing verifier owns
correctness.  This file contributes only:

* frozen contract validation;
* a local Qwen2.5 ``ModelTransport`` implementation (optionally with a frozen
  PEFT adapter, or ``--base-only`` with no adapter weights);
* one 10-case A/B invocation; and
* a review-oriented projection of already-recorded public trajectory evidence.

No tuned adapter is bundled or synthesized here.
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import sys
import time
from pathlib import Path
from typing import Any, Mapping

THIS_FILE = Path(__file__).resolve()
REPO_ROOT = THIS_FILE.parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agentic_debugger.demo.policies import DemoPolicy
from agentic_debugger.evaluation.live import (
    LiveExecutionAuthorization,
    LiveModelConfig,
    LiveRunLimits,
    LiveTransportError,
    run_live_evaluation,
)
from agentic_debugger.evaluation.runner import load_task
from agentic_debugger.runtime.pdb_session import PdbSession

CONTRACT_PATH = THIS_FILE.with_name("experiment_contract.json")
CURATED_ROOT = REPO_ROOT / "agentic_debugger" / "datasets" / "curated"

DEBUGGER_ACTIONS = frozenset(
    {
        "start_pdb_session",
        "get_stack_summary",
        "get_frame",
        "get_frame_locals",
        "safe_eval_expression",
        "inspect_caller_frame",
        "continue_pdb_session",
        "step_pdb_session",
        "next_pdb_session",
        "stop_pdb_session",
    }
)
def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _fixture_tree_sha256(task_dir: Path) -> str:
    digest = hashlib.sha256()
    files = sorted(
        path
        for path in task_dir.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix != ".pyc"
    )
    for path in files:
        relative = path.relative_to(task_dir).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _load_contract() -> dict[str, Any]:
    value = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    if value.get("schema_version") != "tuned-debugger-pilot-v1":
        raise RuntimeError("unsupported debugger pilot contract")
    return value


def _task_ids(contract: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(str(item["task_id"]) for item in contract["tasks"])


def _validate_contract(contract: Mapping[str, Any]) -> dict[str, Any]:
    tasks = list(contract["tasks"])
    conditions = list(contract["conditions"])
    if len(tasks) != 5 or len({item["task_id"] for item in tasks}) != 5:
        raise RuntimeError("pilot must contain exactly five unique tasks")
    if [item["condition_id"] for item in conditions] != [
        "A-static-repair",
        "B-debugger-assisted",
    ]:
        raise RuntimeError("pilot condition identity/order drifted")
    if [item["policy"] for item in conditions] != [
        DemoPolicy.STATIC_BASELINE.value,
        DemoPolicy.PDB_ON_UNCERTAINTY.value,
    ]:
        raise RuntimeError("pilot policy identity/order drifted")

    task_evidence: dict[str, Any] = {}
    for frozen in tasks:
        task_id = frozen["task_id"]
        task_dir = CURATED_ROOT / task_id
        observed_tree = _fixture_tree_sha256(task_dir)
        if observed_tree != frozen["fixture_tree_sha256"]:
            raise RuntimeError(f"fixture identity drifted: {task_id}")
        task = load_task(str(task_dir / "task.json"))
        if task.constraints.max_patch_attempts != contract["budgets"]["task_max_patch_attempts"]:
            raise RuntimeError(f"patch budget drifted: {task_id}")
        if task.constraints.max_test_runs != contract["budgets"]["task_max_test_runs"]:
            raise RuntimeError(f"test budget drifted: {task_id}")
        if task.constraints.max_pdb_observations != contract["budgets"]["task_max_pdb_observations"]:
            raise RuntimeError(f"PDB budget drifted: {task_id}")
        if task.tests.timeout_seconds != frozen["test_timeout_seconds"]:
            raise RuntimeError(f"test timeout drifted: {task_id}")
        if len(task.tests.fail_to_pass) != frozen["f2p"]:
            raise RuntimeError(f"F2P identity drifted: {task_id}")
        if len(task.tests.pass_to_pass) != frozen["p2p"]:
            raise RuntimeError(f"P2P identity drifted: {task_id}")

        # The exact agent-visible mapping is the paired A/B evidence input.
        agent_visible = task.agent_visible_mapping()
        visible_sha = _sha256_bytes(_canonical_json(agent_visible).encode("utf-8"))
        if visible_sha != frozen["agent_visible_mapping_sha256"]:
            raise RuntimeError(f"agent-visible task evidence drifted: {task_id}")
        task_evidence[task_id] = {
            "fixture_tree_sha256": observed_tree,
            "agent_visible_mapping_sha256_A": visible_sha,
            "agent_visible_mapping_sha256_B": visible_sha,
            "agent_visible_mapping_identical_A_B": True,
        }

    budgets = contract["budgets"]
    pdb_request_default = inspect.signature(PdbSession).parameters["request_timeout"].default
    if float(pdb_request_default) != float(budgets["pdb_request_timeout_seconds"]):
        raise RuntimeError("PDB request-timeout default drifted from the frozen pilot")
    if budgets["debugger_accepted_actions_max"] != (
        budgets["debugger_session_starts_max"]
        + budgets["debugger_observation_or_control_actions_max"]
        + budgets["debugger_session_stops_max"]
    ):
        raise RuntimeError("debugger action-budget arithmetic drifted")

    return {
        "contract_sha256": _sha256_bytes(CONTRACT_PATH.read_bytes()),
        "task_evidence": task_evidence,
        "validated_case_count": 10,
    }


def _adapter_identity(adapter_path: Path) -> dict[str, Any]:
    if not adapter_path.is_dir():
        raise RuntimeError("waiting for frozen tuned adapter from Chat B")
    config_path = adapter_path / "adapter_config.json"
    weights_path = adapter_path / "adapter_model.safetensors"
    if not config_path.is_file():
        raise RuntimeError("adapter_config.json is missing from tuned adapter")
    if not weights_path.is_file():
        raise RuntimeError("adapter_model.safetensors is missing from tuned adapter")
    files = []
    combined = hashlib.sha256()
    for path in sorted(item for item in adapter_path.rglob("*") if item.is_file()):
        relative = path.relative_to(adapter_path).as_posix()
        digest = _sha256_bytes(path.read_bytes())
        files.append({"path": relative, "sha256": digest, "size_bytes": path.stat().st_size})
        combined.update(relative.encode("utf-8"))
        combined.update(b"\0")
        combined.update(digest.encode("ascii"))
        combined.update(b"\0")
    return {
        "path": str(adapter_path.resolve()),
        "tree_identity_sha256": combined.hexdigest(),
        "files": files,
    }


class LocalQwenPeftTransport:
    """Local pinned Qwen2.5 base + optional frozen PEFT adapter transport.

    Imports are lazy so ``--validate-only`` has no model/GPU dependency.
    The transport returns the exact envelope already consumed by
    ``LiveModelAdapter``: ``{"directive": ..., "usage": ...}``.

    ``base_only`` selects the RAW control condition: the identical pinned base
    and tokenizer path are used, and no PEFT weights are attached.  All other
    semantics (prompt, serialization, generation, budgets) are shared.
    """

    SYSTEM_PROMPT = (
        "You are the model component of a typed debugging controller. "
        "Return exactly one JSON directive object and no prose, markdown, or "
        "analysis. Obey only the directive_schema, allowed_actions, "
        "legal_transition_targets, and action_contracts in the user payload."
    )

    def __init__(
        self,
        *,
        base_repository: str,
        base_revision: str,
        adapter_path: Path | None,
        max_new_tokens: int,
        max_input_tokens: int,
        base_only: bool = False,
    ) -> None:
        try:
            import torch
            from peft import PeftModel
            from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
        except ImportError as exc:
            raise RuntimeError(
                "real pilot requires torch, transformers, peft and bitsandbytes"
            ) from exc

        if not torch.cuda.is_available():
            raise RuntimeError("real 7B pilot requires a CUDA runtime")

        if base_only == (adapter_path is not None):
            raise RuntimeError("exactly one of base-only or adapter-path must be selected")

        if base_only:
            adapter_config = None
        else:
            adapter_config = json.loads((adapter_path / "adapter_config.json").read_text(encoding="utf-8"))
            declared_base = adapter_config.get("base_model_name_or_path")
            if declared_base not in {None, "", base_repository}:
                raise RuntimeError(
                    "tuned adapter declares a different base model: " + str(declared_base)
                )

        compute_dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
        quantization = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_use_double_quant=True,
            bnb_4bit_compute_dtype=compute_dtype,
        )
        self.tokenizer = AutoTokenizer.from_pretrained(
            base_repository,
            revision=base_revision,
            trust_remote_code=False,
        )
        base = AutoModelForCausalLM.from_pretrained(
            base_repository,
            revision=base_revision,
            trust_remote_code=False,
            device_map="auto",
            quantization_config=quantization,
            torch_dtype=compute_dtype,
        )
        if base_only:
            self.model = base
        else:
            self.model = PeftModel.from_pretrained(
                base,
                str(adapter_path),
                is_trainable=False,
            )
        self.model.eval()
        self.torch = torch
        self.max_new_tokens = max_new_tokens
        self.max_input_tokens = max_input_tokens
        self.device = next(self.model.parameters()).device

    def request(
        self,
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        user_payload = _canonical_json(payload)
        messages = [
            {"role": "system", "content": self.SYSTEM_PROMPT},
            {"role": "user", "content": user_payload},
        ]
        try:
            rendered = self.tokenizer.apply_chat_template(
                messages,
                tokenize=False,
                add_generation_prompt=True,
            )
            inputs = self.tokenizer(
                rendered,
                return_tensors="pt",
                add_special_tokens=False,
            )
        except Exception as exc:
            raise LiveTransportError(
                "local model request tokenization failed",
                kind="request_serialization",
            ) from exc

        prompt_tokens = int(inputs["input_ids"].shape[-1])
        if prompt_tokens > self.max_input_tokens:
            raise LiveTransportError(
                "local model request exceeds frozen input-token bound",
                kind="request_too_large",
            )
        inputs = {key: value.to(self.device) for key, value in inputs.items()}
        started = time.monotonic()
        try:
            with self.torch.inference_mode():
                generated = self.model.generate(
                    **inputs,
                    do_sample=False,
                    max_new_tokens=self.max_new_tokens,
                    max_time=float(timeout_seconds),
                    pad_token_id=(
                        self.tokenizer.eos_token_id
                        if self.tokenizer.pad_token_id is None
                        else self.tokenizer.pad_token_id
                    ),
                )
        except Exception as exc:
            raise LiveTransportError(
                "local model generation failed",
                kind="process_error",
            ) from exc
        if time.monotonic() - started > timeout_seconds + 5.0:
            raise LiveTransportError(
                "local model generation exceeded request timeout",
                kind="request_timeout",
                timed_out=True,
            )

        new_tokens = generated[0, prompt_tokens:]
        completion_tokens = int(new_tokens.shape[-1])
        text = self.tokenizer.decode(new_tokens, skip_special_tokens=True).strip()
        try:
            directive = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LiveTransportError(
                "local model response was not strict JSON",
                kind="invalid_response",
            ) from exc
        if not isinstance(directive, Mapping):
            raise LiveTransportError(
                "local model directive was not a JSON object",
                kind="invalid_response",
            )
        return {
            "directive": dict(directive),
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }


def _parse_events(events_jsonl: str) -> list[dict[str, Any]]:
    result = []
    for line in events_jsonl.splitlines():
        if line.strip():
            result.append(json.loads(line))
    return result


def _case_evidence(case: Mapping[str, Any]) -> dict[str, Any]:
    events = _parse_events(str(case.get("events_jsonl") or ""))
    actions: dict[str, dict[str, Any]] = {}
    observations: dict[str, dict[str, Any]] = {}
    model_action_events = []
    debugger_commands = []
    patch = None
    patch_action_id = None
    declared_root_cause = []

    for event in events:
        if event.get("event_type") == "action":
            action = event.get("payload", {}).get("action", {})
            action_id = action.get("action_id")
            if isinstance(action_id, str):
                actions[action_id] = action
            model_action_events.append(action)
            name = action.get("name")
            if name in DEBUGGER_ACTIONS:
                debugger_commands.append(
                    {
                        "sequence": event.get("sequence"),
                        "action_id": action_id,
                        "name": name,
                        "arguments": action.get("arguments", {}),
                    }
                )
            if name == "apply_patch":
                patch_action_id = action_id
                patch = action.get("arguments", {}).get("patch")
            if name == "express_root_cause_hypothesis":
                declared_root_cause.append(action.get("arguments", {}))
        elif event.get("event_type") == "observation":
            observation = event.get("payload", {}).get("observation", {})
            action_id = observation.get("action_id")
            if isinstance(action_id, str):
                observations[action_id] = observation

    command_results = []
    structured_observations = []
    selected_breakpoints = []
    selected_frames = []
    selected_variable_expressions = []
    for command in debugger_commands:
        observation = observations.get(command["action_id"])
        command_results.append(
            {
                "action_id": command["action_id"],
                "name": command["name"],
                "status": observation.get("status") if observation else "missing",
                "payload": observation.get("payload") if observation else None,
            }
        )
        if observation is not None:
            structured_observations.append(observation)
        arguments = command.get("arguments", {})
        if command["name"] == "start_pdb_session" and "breakpoint_line" in arguments:
            selected_breakpoints.append(arguments["breakpoint_line"])
        if command["name"] in {"get_frame", "get_frame_locals", "safe_eval_expression"}:
            if "frame_id" in arguments:
                selected_frames.append(arguments["frame_id"])
        if command["name"] == "safe_eval_expression" and "expression" in arguments:
            selected_variable_expressions.append(arguments["expression"])

    evidence = case.get("evidence") or {}
    directive_attempts = list(evidence.get("observable_model_directive_attempts") or [])
    accepted_directives = list(evidence.get("observable_model_directives") or [])
    post_debugger_directives = [
        item
        for item in accepted_directives
        if isinstance(item.get("last_observation"), Mapping)
        and item["last_observation"].get("name") in DEBUGGER_ACTIONS
    ]

    emitted_debugger_action_attempts = [
        item
        for item in directive_attempts
        if isinstance(item.get("directive"), Mapping)
        and item["directive"].get("kind") == "action"
        and item["directive"].get("name") in DEBUGGER_ACTIONS
    ]
    accepted_debugger_action_attempts = [
        item for item in emitted_debugger_action_attempts if item.get("accepted") is True
    ]
    successful_debugger_commands = [
        item for item in command_results if item.get("status") == "ok"
    ]

    verifier = case.get("verifier") or {}
    measurements = case.get("measurements") or {}
    return {
        "case_id": case.get("case_id"),
        "task_id": case.get("task_id"),
        "condition": (
            "A-static-repair"
            if case.get("policy") == DemoPolicy.STATIC_BASELINE.value
            else "B-debugger-assisted"
        ),
        "status": case.get("status"),
        "model_action": {
            "accepted_action_events": model_action_events,
            "directive_attempts": directive_attempts,
        },
        "debugger_command": debugger_commands,
        "command_execution_result": command_results,
        "selected_breakpoint": selected_breakpoints,
        "selected_frame": selected_frames,
        "selected_variable_expression": selected_variable_expressions,
        "structured_debugger_observation": structured_observations,
        "observable_root_cause_or_localization": {
            "declared_root_cause_actions": declared_root_cause,
            "observable_model_directives": accepted_directives,
            "verifier_localization": verifier.get("localization"),
        },
        "post_debugger_model_directive": post_debugger_directives,
        "patch": patch,
        "patch_apply": {
            "controller_observation": observations.get(patch_action_id) if patch_action_id else None,
            "verifier": verifier.get("patch_application"),
        },
        "F2P": verifier.get("fail_to_pass"),
        "P2P": verifier.get("pass_to_pass"),
        "RESOLVED": verifier.get("outcome") == "RESOLVED",
        "debugger_turns": len(debugger_commands),
        "runtime": {
            "case_elapsed_duration_ms": measurements.get("case_elapsed_duration_ms"),
            "model_phase_elapsed_duration_ms": measurements.get("model_phase_elapsed_duration_ms"),
            "model_transport_duration_ms": measurements.get("model_transport_duration_ms"),
        },
        "tokens": measurements.get("token_usage"),
        "derived_metrics": {
            "debugger_action_attempts": len(emitted_debugger_action_attempts),
            "debugger_action_accepted": len(accepted_debugger_action_attempts),
            "debugger_action_executed_ok": len(successful_debugger_commands),
            "debugger_command_valid_rate": (
                len(accepted_debugger_action_attempts) / len(emitted_debugger_action_attempts)
                if emitted_debugger_action_attempts
                else None
            ),
            "debugger_command_execution_rate": (
                len(successful_debugger_commands) / len(debugger_commands)
                if debugger_commands
                else None
            ),
            "interpretation_evidence_present": bool(post_debugger_directives),
            "debugger_output_interpretation_correctness": "NOT_ASSESSED",
        },
    }


def _build_evidence(report: Mapping[str, Any], validation: Mapping[str, Any], adapter: Mapping[str, Any] | None) -> dict[str, Any]:
    return {
        "schema_version": "tuned-debugger-pilot-evidence-v1",
        "contract_sha256": validation["contract_sha256"],
        "adapter_identity": adapter,
        "task_pairing": validation["task_evidence"],
        "cases": [_case_evidence(case) for case in report.get("cases", [])],
        "interpretation_scoring_boundary": (
            "Observable post-debugger directives are retained. Correctness remains "
            "NOT_ASSESSED until the existing independent root-cause rubric is applied."
        ),
    }


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _run_identity(
    validation: Mapping[str, Any],
    model_contract: Mapping[str, Any],
    adapter: Mapping[str, Any] | None,
    task_ids: tuple[str, ...],
    conditions: list[str],
    *,
    base_only: bool,
    chat_template: str | None,
) -> dict[str, Any]:
    if base_only:
        return {
            "contract_sha256": validation["contract_sha256"],
            "model_condition": "RAW_BASE",
            "adapter_applied": False,
            "adapter_path": None,
            "adapter_identity": None,
            "base_repository": model_contract["base_repository"],
            "base_revision": model_contract["base_revision"],
            "tokenizer_identity": {
                "repository": model_contract["base_repository"],
                "revision": model_contract["base_revision"],
                "source": "AutoTokenizer.from_pretrained(base_repository, revision=base_revision)",
                "chat_template_sha256": _sha256_bytes((chat_template or "").encode("utf-8")),
            },
            "task_ids": list(task_ids),
            "conditions": conditions,
        }
    return {
        "contract_sha256": validation["contract_sha256"],
        "adapter_identity": adapter,
        "base_repository": model_contract["base_repository"],
        "base_revision": model_contract["base_revision"],
        "task_ids": list(task_ids),
        "conditions": conditions,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--base-only", action="store_true")
    parser.add_argument("--adapter-path", type=Path)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()

    contract = _load_contract()
    validation = _validate_contract(contract)
    if args.validate_only:
        print(json.dumps({"status": "PASS", **validation}, indent=2))
        return 0

    if args.base_only and args.adapter_path is not None:
        raise SystemExit("--base-only and --adapter-path are mutually exclusive")
    if not args.base_only and args.adapter_path is None:
        raise SystemExit("waiting for frozen tuned adapter from Chat B")
    if args.output_dir is None:
        raise SystemExit("--output-dir is required for a real pilot run")

    model_contract = contract["model"]
    generation = model_contract["generation"]
    adapter: dict[str, Any] | None = None
    if args.base_only:
        transport = LocalQwenPeftTransport(
            base_repository=model_contract["base_repository"],
            base_revision=model_contract["base_revision"],
            adapter_path=None,
            max_new_tokens=generation["max_new_tokens"],
            max_input_tokens=generation["max_input_tokens"],
            base_only=True,
        )
    else:
        adapter_path = args.adapter_path.resolve()
        adapter = _adapter_identity(adapter_path)
        transport = LocalQwenPeftTransport(
            base_repository=model_contract["base_repository"],
            base_revision=model_contract["base_revision"],
            adapter_path=adapter_path,
            max_new_tokens=generation["max_new_tokens"],
            max_input_tokens=generation["max_input_tokens"],
        )

    budgets = contract["budgets"]
    config = LiveModelConfig(
        model_name=(
            f"{model_contract['base_repository']}+RAW-BASE"
            if args.base_only
            else f"{model_contract['base_repository']}+PEFT:{adapter['tree_identity_sha256'][:12]}"
        ),
        # A custom transport is injected, so this command is never executed.
        # It remains an inert non-secret identity field required by LiveModelConfig.
        command=("local-qwen-peft-transport",),
        request_timeout_seconds=budgets["model_request_timeout_seconds"],
        tool_version="tuned-debugger-pilot-v1",
    )
    limits = LiveRunLimits(
        max_model_requests=budgets["model_requests_max"],
        max_controller_steps=budgets["controller_steps_max"],
        max_model_phase_seconds=budgets["model_phase_seconds_max"],
        max_retries=budgets["model_retries_per_logical_call_max"],
        continue_on_task_failure=True,
    )

    authorization = LiveExecutionAuthorization.authorize(True, live_selected=True)
    report = run_live_evaluation(
        repository_root=REPO_ROOT,
        authorization=authorization,
        config=config,
        limits=limits,
        task_ids=_task_ids(contract),
        policies=(DemoPolicy.STATIC_BASELINE, DemoPolicy.PDB_ON_UNCERTAINTY),
        repetitions=1,
        transport_factory=lambda task, policy, repetition: transport,
        evaluation_id="tuned-debugger-pilot-v1",
        interactive_debugger_controls=True,
        retain_observable_model_directives=True,
    )

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(output_dir / "pilot_report.json", report)
    _write_json(
        output_dir / "pilot_evidence.json",
        _build_evidence(report, validation, adapter),
    )
    _write_json(
        output_dir / "run_identity.json",
        _run_identity(
            validation,
            model_contract,
            adapter,
            _task_ids(contract),
            [item["condition_id"] for item in contract["conditions"]],
            base_only=args.base_only,
            chat_template=transport.tokenizer.chat_template,
        ),
    )
    print(str(output_dir / "pilot_report.json"))
    print(str(output_dir / "pilot_evidence.json"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
