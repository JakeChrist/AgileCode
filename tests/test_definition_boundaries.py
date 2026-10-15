"""Definition ownership and compatibility without an execution-capable host."""
from __future__ import annotations

import importlib
import subprocess
import sys
import textwrap
from dataclasses import fields
from typing import get_type_hints

import pytest
from pydantic import ValidationError

from agilecode.domain import types as definitions


@pytest.mark.parametrize("module_name", [
    "agilecode.domain",
    "agilecode.domain.types",
    "agilecode.core.types",
])
def test_definition_construction_cannot_import_runtime_services(module_name, tmp_path):
    # conftest imports the model adapter, so enforce the boundary in a fresh
    # interpreter rather than trusting the parent process's import cache.
    program = textwrap.dedent("""\
        import importlib
        import importlib.abc
        import sys

        forbidden = (
            'agilecode.runtime', 'agilecode.adapters', 'agilecode.desktop',
            'agilecode.core.application', 'agilecode.core.runner',
            'agilecode.core.project_manager', 'agilecode.core.discussion',
            'agilecode.core.integration', 'agilecode.core.tools',
            'PySide6', 'PyQt6', 'httpx',
        )

        class RejectRuntimeImports(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if any(fullname == name or fullname.startswith(name + '.') for name in forbidden):
                    raise AssertionError('Definitions imported runtime dependency: ' + fullname)
                return None

        sys.meta_path.insert(0, RejectRuntimeImports())
        definitions = importlib.import_module(sys.argv[1])
        member = definitions.TeamMember(id='analyst', name='Analyst', system_prompt='Review the ticket.')
        step = definitions.Step(id='review', name='Review', team_member_id=member.id, prompt='Review inputs.')
        process = definitions.Process(id='analysis', name='Analysis', steps=[step])
        team = definitions.Team(id='team', name='Team', members=[member], processes=[process],
                                default_process_id=process.id)
        assert definitions.Team.model_validate_json(team.model_dump_json()) == team
        assert team.process('analysis').steps[0] is step
        assert team.member('analyst') is member
        settings = definitions.Settings()
        provider = definitions.ProviderConfig(id='local', name='Local')
        pool = definitions.ProviderPool(providers=[provider], default_provider_id='local')
        result = definitions.StepResult(status='PASS', summary='Reviewed.', output='Complete.')
        assert result.status is definitions.ResultStatus.PASS
        assert settings.default_process == 'development'
        assert pool.providers[0].profile.base_url == 'http://127.0.0.1:1234/v1'
        assert list(definitions.BoardState) == ['Backlog', 'Ready', 'In Progress', 'Done']
        assert not hasattr(step, 'run')
        assert not any(name == prefix or name.startswith(prefix + '.')
                       for name in sys.modules for prefix in forbidden)
    """)
    completed = subprocess.run(
        [sys.executable, "-c", program, module_name],
        cwd=tmp_path, text=True, capture_output=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_compatibility_exports_reference_single_definition_owner():
    legacy = importlib.import_module("agilecode.core.types")
    public_domain = importlib.import_module("agilecode.domain")
    for name in definitions.__all__:
        assert getattr(legacy, name) is getattr(definitions, name), name
        assert getattr(public_domain, name) is getattr(definitions, name), name
    assert definitions.Step.__module__ == "agilecode.domain.types"
    assert definitions.Team.__module__ == "agilecode.domain.types"
    assert definitions.Process.__module__ == "agilecode.domain.types"
    assert definitions.Settings.__module__ == "agilecode.domain.types"


def test_step_is_data_and_context_belongs_to_execution():
    from agilecode.core.types import StepContext as LegacyStepContext
    from agilecode.runtime.context import StepContext

    assert LegacyStepContext is StepContext
    assert StepContext.__module__ == "agilecode.runtime.context"
    assert "StepContext" not in definitions.__all__
    assert not hasattr(definitions, "StepContext")
    assert not hasattr(definitions.Step, "run")
    assert not hasattr(definitions.Step, "execute")
    assert get_type_hints(StepContext)["team"] is definitions.Team
    assert get_type_hints(StepContext)["process"] is definitions.Process
    assert [field.name for field in fields(StepContext)] == [
        "ticket", "run", "attempt", "team", "process", "workspace", "outputs",
        "previous_output", "correction_report", "execute", "phase", "extra",
    ]


def test_step_serialization_and_validation_are_unchanged():
    step = definitions.Step(id="review", name="Review", team_member_id="analyst", prompt="Review inputs.")
    assert step.model_dump(mode="json") == {
        "id": "review", "name": "Review", "team_member_id": "analyst",
        "prompt": "Review inputs.", "kind": "analysis", "correction_target": None,
    }
    with pytest.raises(ValidationError):
        step.kind = "unsupported"
    with pytest.raises(ValidationError):
        definitions.Step.model_validate({**step.model_dump(), "unexpected": True})
    with pytest.raises(ValidationError):
        definitions.Step.model_validate({**step.model_dump(), "prompt": ""})


def test_process_validation_stays_with_definition_owner():
    first = definitions.Step(id="first", name="First", team_member_id="analyst", prompt="Review inputs.")
    second = definitions.Step(id="second", name="Second", team_member_id="analyst", prompt="Review again.",
                              correction_target="first")
    process = definitions.Process(id="analysis", name="Analysis", steps=[first, second])
    assert definitions.Process.model_validate_json(process.model_dump_json()) == process
    for steps in ([first, first], [second, first]):
        with pytest.raises(ValidationError):
            definitions.Process(id="analysis", name="Analysis", steps=steps)
    with pytest.raises(ValidationError):
        definitions.Process(id="analysis", name="Analysis", steps=[first], standard=True)


def test_team_reference_validation_stays_with_definition_owner():
    member = definitions.TeamMember(id="analyst", name="Analyst", system_prompt="Review the ticket.")
    step = definitions.Step(id="review", name="Review", team_member_id=member.id, prompt="Review inputs.")
    process = definitions.Process(id="analysis", name="Analysis", steps=[step])
    team = definitions.Team(id="team", name="Team", members=[member], processes=[process],
                            default_process_id=process.id)
    for change in (
        {"members": [member.model_dump(), member.model_dump()]},
        {"default_process_id": "missing"},
        {"processes": [{**process.model_dump(), "steps": [{**step.model_dump(), "team_member_id": "missing"}]}]},
    ):
        with pytest.raises(ValidationError):
            definitions.Team.model_validate({**team.model_dump(), **change})


def test_existing_notification_boundary_uses_shared_event_definition():
    from agilecode.core.types import Event, Events

    events = Events()
    received = []
    unsubscribe = events.subscribe(received.append)
    event = definitions.Event("TicketChanged", ticket_id="ticket")
    assert Event is definitions.Event
    events.emit(event)
    assert received == [event]
    unsubscribe()
    events.emit(event)
    assert received == [event]
