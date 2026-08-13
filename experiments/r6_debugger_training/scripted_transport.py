#!/usr/bin/env python3
"""R6 — scripted trajectory transport (perfect-protocol model stand-in).

Emits the canonical directive for every stage of the frozen r5.9 bridge
protocol, driven by the CURRENT user prompt (phase + available commands) and
per-task gold metadata (breakpoint line, single-line diagnosis, corrected
whole-file content).  Used ONLY to generate authentic execution-grounded
training trajectories over DISJOINT QuixBugs tasks: real PDB observations,
real sanitized diagnostics, real verifier feedback — with the repair target
taken from the task's gold repair (allowed: training tasks, never the five
R6 holdouts).

The emitted text follows exactly the accepted bridge grammar:
``reproduce``, ``break <n>``, ``stack``, ``locals``, ``next``,
``diagnosis <one-line text>``, ``file <path>`` + complete replacement
content.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from experiments.debugger_interaction_v2_r5.adapter import (
    TransportResponse,
)

_PHASE_RE = re.compile(r"Current phase:\s*(\w+)")
_COMMANDS_RE = re.compile(r"Available commands:\n((?:  - \S+\n?)+)")


def available_commands(user_prompt: str) -> tuple[str, ...]:
    match = _COMMANDS_RE.search(user_prompt)
    if match is None:
        return ()
    return tuple(
        line.strip()[2:].strip()
        for line in match.group(1).splitlines()
        if line.strip().startswith("- ")
    )


class ScriptedTrajectoryTransport:
    """Perfect-protocol transport for training-trajectory generation."""

    def __init__(
        self,
        *,
        module_path: str,
        breakpoint_line: int,
        diagnosis_text: str,
        corrected_source: str,
        rejected_source: Optional[str] = None,
    ) -> None:
        if not module_path or not module_path.endswith(".py"):
            raise ValueError(f"invalid module path: {module_path!r}")
        if type(breakpoint_line) is not int or breakpoint_line < 1:
            raise ValueError(f"invalid breakpoint line: {breakpoint_line!r}")
        if not diagnosis_text or "\n" in diagnosis_text:
            raise ValueError("diagnosis must be one non-empty line")
        if not corrected_source.strip():
            raise ValueError("corrected source must be non-empty")
        self.module_path = module_path
        self.breakpoint_line = breakpoint_line
        self.diagnosis_text = diagnosis_text
        self.corrected_source = corrected_source
        self.rejected_source = rejected_source
        self.patch_requests = 0

    def _directive(self, user_prompt: str) -> str:
        phase_match = _PHASE_RE.search(user_prompt)
        if phase_match is None:
            raise ValueError("user prompt has no 'Current phase' line")
        phase = phase_match.group(1)
        commands = available_commands(user_prompt)

        if phase == "Reproduce":
            return "reproduce"

        if phase == "RuntimeEvidence":
            if "break" in commands:
                return f"break {self.breakpoint_line}"
            if "stack" in commands:
                return "stack"
            if "locals" in commands:
                return "locals"
            if "step" in commands or "next" in commands:
                return "next"
            if "diagnosis" in commands:
                return f"diagnosis {self.diagnosis_text}"
            raise ValueError(
                f"RuntimeEvidence with unexpected commands: {commands}"
            )

        if phase == "Patch":
            if "file" in commands:
                self.patch_requests += 1
                source = self.corrected_source
                if self.patch_requests == 1 and self.rejected_source is not None:
                    source = self.rejected_source
                return f"file {self.module_path}\n{source}"
            if "patch" in commands:
                raise ValueError(
                    "scripted transport requires the 'file' representation"
                )
            raise ValueError(f"Patch with unexpected commands: {commands}")

        raise ValueError(f"unexpected phase {phase!r}")

    def request(
        self,
        system_prompt: str,
        user_prompt: str,
        timeout_seconds: float,
    ) -> TransportResponse:
        text = self._directive(user_prompt)
        return TransportResponse(
            raw_text=text,
            usage={
                "prompt_tokens": len(user_prompt.split()),
                "completion_tokens": len(text.split()),
                "total_tokens": len(user_prompt.split()) + len(text.split()),
            },
        )


__all__ = ["ScriptedTrajectoryTransport", "available_commands"]
