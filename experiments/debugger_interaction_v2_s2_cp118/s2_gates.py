"""S2 — Gate computation (legacy unchanged + strict additive).

S2 reports BOTH:

- **Gate B legacy**: the repository's existing Gate-B computation, unchanged
  (``experiments/debugger_interaction_v2/runner.py:_compute_gate_b``).
  Administrative D1 transitions are already structurally excluded there
  (they carry ``parse_result.status == "administrative"`` and no
  ``action_name``).

- **Gate B strict**: a real iterative debugger loop, computed additively by
  S2 from the same telemetry plus the real observation statuses recorded in
  the projected trajectory.  All six conditions must hold:

  1. first MODEL-AUTHORED accepted PDB command;
  2. the command reaches the real PDB backend;
  3. it produces a SUCCESSFUL NON-ERROR PDB observation/state
     (observation ``status == ok``);
  4. that exact observation is bound into the next actual model request
     (``prior_observation_id`` + ``rendered_observation_sha256``);
  5. the model authors a second accepted PDB command;
  6. the second command reaches the real PDB backend and also produces a
     successful non-error PDB observation/control result.

  Tool-error observations may be retained as real provenance evidence but
  MUST NOT satisfy Gate B strict.  Administrative D1 transitions count
  toward neither Gate B.

The observation produced by an accepted PDB command is identified
deterministically: the controller executes the command, produces exactly
one real observation, and the very next model request binds it as its
``prior_observation_id`` (the same evidence Gate B legacy uses).
"""

from __future__ import annotations

import json
from typing import Any, Optional

from experiments.debugger_interaction_v2.runner import _compute_gate_b

# PDB action names that count as "real debugger commands" (same frozen set
# as the S1 runner's Gate-B filter).
_PDB_ACTIONS = frozenset({
    "start_pdb_session",
    "get_stack_summary",
    "get_frame_locals",
    "safe_eval_expression",
    "continue_pdb_session",
    "step_pdb_session",
    "next_pdb_session",
})


def compute_gate_b_legacy(telemetry: list[dict[str, Any]]) -> dict[str, Any]:
    """Gate B legacy: the repository's existing computation, unchanged."""

    return _compute_gate_b(telemetry)


def observation_status_map(trajectory_jsonl: str) -> dict[str, str]:
    """Map ``observation_id -> status`` from the projected trajectory events.

    Observation events carry ``payload.observation`` (the real observation
    mapping) with ``observation_id`` and ``status`` (``ok``/``error``/
    ``rejected``/``timeout``).  Fail closed on malformed records.
    """

    status_by_id: dict[str, str] = {}
    for line in trajectory_jsonl.strip().splitlines():
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"trajectory event is not valid JSON: {exc}"
            ) from exc
        if event.get("event_type") != "observation":
            continue
        payload = event.get("payload")
        observation = payload.get("observation") if isinstance(payload, dict) else None
        if not isinstance(observation, dict):
            raise RuntimeError(
                "observation event payload lacks a mapping observation record"
            )
        obs_id = observation.get("observation_id")
        status = observation.get("status")
        if not isinstance(obs_id, str) or not obs_id:
            raise RuntimeError("observation event lacks observation_id")
        if not isinstance(status, str) or not status:
            raise RuntimeError(f"observation {obs_id} lacks a status")
        status_by_id[obs_id] = status
    return status_by_id


def _is_admin_record(record: dict[str, Any]) -> bool:
    """Administrative D1 transitions: never model-authored, never counted."""

    return record.get("raw_response_status") == "administrative_navigation" or (
        record.get("parse_result", {}).get("status") == "administrative"
    )


def _accepted_pdb_records(
    telemetry: list[dict[str, Any]],
) -> list[tuple[int, dict[str, Any]]]:
    """(index, record) pairs of model-authored accepted PDB commands.

    Administrative D1 transitions carry ``parse_result.status ==
    "administrative"`` and are never accepted model commands.
    """

    return [
        (index, record)
        for index, record in enumerate(telemetry)
        if record.get("parse_result", {}).get("status") == "accepted"
        and record.get("translated_directive", {}).get("action_name") in _PDB_ACTIONS
    ]


def _next_model_record(
    telemetry: list[dict[str, Any]],
    index: int,
) -> tuple[int, dict[str, Any]]:
    """The next actual model request after ``index`` (skipping administrative
    records, which are harness-produced and never follow a PDB command)."""

    for next_index in range(index + 1, len(telemetry)):
        record = telemetry[next_index]
        if not _is_admin_record(record):
            return next_index, record
    raise RuntimeError(
        "no model request follows the accepted PDB command — "
        "observation binding cannot be verified"
    )


def _produced_observation(
    telemetry: list[dict[str, Any]],
    record_index: int,
    status_by_id: dict[str, str],
) -> tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    """For an accepted PDB command at ``record_index``, return
    ``(produced_observation_id, observation_status, rendered_sha, error)``.

    The produced observation is the ``prior_observation_id`` of the next
    actual model request; its status comes from the real trajectory
    observations.  ``error`` is set (and the rest are ``None``) when no
    next model request exists.
    """

    try:
        _, next_record = _next_model_record(telemetry, record_index)
    except RuntimeError as exc:
        return None, None, None, str(exc)
    provenance = next_record.get("provenance", {})
    produced_id = provenance.get("prior_observation_id")
    rendered_sha = provenance.get("rendered_observation_sha256")
    if not isinstance(produced_id, str) or not produced_id:
        return None, None, rendered_sha, (
            "accepted PDB command produced no bound observation"
        )
    status = status_by_id.get(produced_id)
    return produced_id, status, rendered_sha, None


def compute_gate_b_strict(
    telemetry: list[dict[str, Any]],
    status_by_id: dict[str, str],
) -> dict[str, Any]:
    """Gate B strict: the six-condition real iterative loop definition.

    Returns a detail dict with one entry per condition (``met`` + ``detail``),
    a flat ``passed`` boolean, ``successful_pdb_observations`` (status-ok
    observations produced by accepted PDB commands), and the accepted
    PDB-command count.  Fail closed: any unverifiable condition is NOT met.
    """

    accepted_pdb = _accepted_pdb_records(telemetry)
    details: list[dict[str, Any]] = []
    successful: list[str] = []

    # --- Condition 1: first MODEL-AUTHORED accepted PDB command ----------
    if not accepted_pdb:
        details.append({
            "condition": 1,
            "met": False,
            "detail": "no model-authored accepted PDB command",
        })
        for condition in (2, 3, 4, 5, 6):
            details.append({
                "condition": condition,
                "met": False,
                "detail": "no first accepted PDB command",
            })
        return {
            "passed": False,
            "reason": "no model-authored accepted PDB command",
            "accepted_pdb_count": 0,
            "successful_pdb_observations": [],
            "conditions": details,
        }
    first_index, first_record = accepted_pdb[0]
    details.append({
        "condition": 1,
        "met": True,
        "detail": "first accepted PDB command: "
                  f"{first_record['translated_directive']['action_name']}",
    })

    # --- Conditions 2-4: first command reaches PDB, produces a successful
    # non-error observation, and that exact observation is bound into the
    # next actual model request -------------------------------------------
    produced_id, status, rendered_sha, error = _produced_observation(
        telemetry, first_index, status_by_id
    )
    if error is not None:
        details.append({"condition": 2, "met": False, "detail": error})
        details.append({"condition": 3, "met": False, "detail": error})
        details.append({"condition": 4, "met": False, "detail": error})
    else:
        details.append({
            "condition": 2,
            "met": True,
            "detail": f"command reached real PDB; produced observation "
                      f"{produced_id}",
        })
        if status is None:
            details.append({
                "condition": 3,
                "met": False,
                "detail": f"observation {produced_id} has no recorded status "
                          f"in the trajectory",
            })
        elif status == "ok":
            details.append({
                "condition": 3,
                "met": True,
                "detail": f"observation {produced_id} status=ok "
                          f"(successful non-error PDB state)",
            })
            successful.append(produced_id)
        else:
            details.append({
                "condition": 3,
                "met": False,
                "detail": f"observation {produced_id} status={status!r} — "
                          f"tool/error observations MUST NOT satisfy "
                          f"Gate B strict",
            })
        if rendered_sha:
            details.append({
                "condition": 4,
                "met": True,
                "detail": f"exact observation {produced_id} bound into next "
                          f"model request (rendered_observation_sha256 "
                          f"{rendered_sha})",
            })
        else:
            details.append({
                "condition": 4,
                "met": False,
                "detail": "next model request lacks "
                          "prior_observation_id/rendered_observation_sha256 "
                          "binding",
            })

    # --- Conditions 5-6: second accepted PDB command also reaches PDB and
    # produces a successful non-error observation -------------------------
    if len(accepted_pdb) < 2:
        details.append({
            "condition": 5,
            "met": False,
            "detail": "no second accepted PDB command",
        })
        details.append({
            "condition": 6,
            "met": False,
            "detail": "no second accepted PDB command",
        })
    else:
        second_index, second_record = accepted_pdb[1]
        details.append({
            "condition": 5,
            "met": True,
            "detail": "second accepted PDB command: "
                      f"{second_record['translated_directive']['action_name']}",
        })
        produced_id, status, rendered_sha, error = _produced_observation(
            telemetry, second_index, status_by_id
        )
        if error is not None:
            details.append({"condition": 6, "met": False, "detail": error})
        elif status == "ok":
            details.append({
                "condition": 6,
                "met": True,
                "detail": f"second command reached real PDB and produced "
                          f"observation {produced_id} status=ok (successful "
                          f"non-error PDB observation/control result)",
            })
            successful.append(produced_id)
        else:
            details.append({
                "condition": 6,
                "met": False,
                "detail": f"second command produced observation {produced_id} "
                          f"status={status!r} — tool/error observations MUST "
                          f"NOT satisfy Gate B strict",
            })

    passed = all(detail.get("met") for detail in details)
    return {
        "passed": passed,
        "reason": (
            "real iterative debugger loop confirmed: 2 accepted PDB commands, "
            "each producing a successful non-error PDB observation bound "
            "into the next model request"
            if passed
            else "not a real iterative debugger loop under the strict definition"
        ),
        "accepted_pdb_count": len(accepted_pdb),
        "successful_pdb_observations": successful,
        "conditions": details,
    }


__all__ = [
    "compute_gate_b_legacy",
    "compute_gate_b_strict",
    "observation_status_map",
]
