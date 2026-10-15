"""Per-attempt inputs owned by the execution layer, not project definitions."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from agilecode.domain.types import Process, Step, StepResult, Team


@dataclass
class StepContext:
    ticket: dict[str, Any]
    run: dict[str, Any]
    attempt: dict[str, Any]
    team: Team
    process: Process
    workspace: Path
    outputs: dict[str, StepResult]
    previous_output: str | None
    correction_report: str | None
    execute: Callable[[Step, StepContext], Awaitable[StepResult]]
    phase: str = "development"
    extra: dict[str, Any] = field(default_factory=dict)


__all__ = ["StepContext"]
