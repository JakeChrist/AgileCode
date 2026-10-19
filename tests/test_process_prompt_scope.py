"""Contracts for the actual built-in composed prompts, not claims about model behavior."""
from __future__ import annotations

import pytest

from agilecode.core.context import PromptBuilder
from agilecode.core.defaults import (
    requirements_engineering_team,
    standard_team,
    systems_architecture_team,
)
from agilecode.core.types import StepContext


TEAMS = (standard_team(), requirements_engineering_team(), systems_architecture_team())
STEP_CASES = [
    pytest.param(team, process, step, id=f"{team.id}/{process.id}/{step.id}")
    for team in TEAMS
    for process in team.processes
    for step in process.steps
]

# These steps assess an existing subject. Authoring criteria, draft generation,
# traceability work, and revisions must not inherit the subject-preservation rule.
REVIEW_STEPS = {
    "review-stakeholder-needs",
    "review-requirements",
    "evaluate-requirements-coverage",
    "review-requirement-changes",
    "collect-verification-evidence",
    "review-verification-results",
    "evaluate-need-satisfaction",
    "identify-requirements-deficiencies",
    "review-system-architecture",
    "analyze-interface-compatibility",
    "review-interface-agreements",
    "examine-architecture-and-evidence",
    "review-architecture-change",
}


def build_prompt(team, process, step, workspace, description="Produce the requested documents."):
    context = StepContext(
        ticket={"key": "T-1", "title": "Document work", "description": description},
        run={}, attempt={}, team=team, process=process, workspace=workspace,
        outputs={}, previous_output=None, correction_report=None, execute=None,
    )
    return PromptBuilder().build(step, context, [])


@pytest.mark.parametrize("team,process,step", STEP_CASES)
def test_composed_builtin_prompts_keep_review_rules_at_the_assigned_step(
    tmp_path, team, process, step
):
    prompt = build_prompt(team, process, step, tmp_path)
    assert team.member(step.team_member_id).system_prompt in prompt
    assert f"CURRENT STEP: {step.name} ({step.kind})\n{step.prompt}" in prompt
    assert "rewrite another role's work without an assigned revision step" not in prompt
    assert "Record review findings in the requested findings document" not in prompt

    if step.kind == "analysis":
        assert "authoring, analysis, traceability, or revision assigned by the current step" in prompt
        assert "including updates to assigned documents produced by earlier roles" in prompt
        assert "An assigned output document need not already exist" in prompt
        assert "create it when the current step calls for authoring" in prompt
        assert "Limit file changes to the assigned document deliverables" in prompt
        assert "do not implement product code or make unrelated changes" in prompt
        assert {"write_file", "edit_file"} <= set(team.member(step.team_member_id).allowed_tools)
        is_review = step.id in REVIEW_STEPS
        for instruction in (
            "Preserve the subject and supporting evidence being assessed",
            "Record findings in the assigned findings or results document",
            "do not silently correct the subject or rewrite its baseline",
            "Return required changes to the responsible author or assigned revision step",
        ):
            assert (instruction in prompt) == is_review
            assert instruction not in team.member(step.team_member_id).system_prompt


def test_derive_requirements_creates_missing_draft_before_later_review(tmp_path):
    team = requirements_engineering_team()
    process = team.process("requirements-definition")
    step = process.steps[0]
    assert step.id == "derive-requirements"
    (tmp_path / "stakeholder-needs.md").write_text("NEED-1: Users can export their records.\n")
    assert not (tmp_path / "requirements.md").exists()
    prompt = build_prompt(
        team, process, step, tmp_path,
        "Derive requirements from stakeholder-needs.md and save requirements.md.",
    )
    assert "Create the initial requirements draft in the requested repository document" in prompt
    assert "An existing requirements document is not a prerequisite" in prompt
    assert "If the ticket requests updates to an existing draft, read and update that document instead" in prompt
    assert "Give each requirement a stable identifier and preserve traceability" in prompt
    assert "Keep unsupported assumptions and unresolved stakeholder decisions explicit" in prompt
    assert "For every requirement, define objective verification criteria" in prompt
    assert "On a correction pass, address the supplied review or baseline findings" in prompt
    assert "Save the substantive draft before completing this step; independent review follows" in prompt
    assert [item.id for item in process.steps[1:]] == [
        "review-requirements", "baseline-requirements",
    ]
    assert "stakeholder-needs.md" in prompt.history[0]["content"]


@pytest.mark.parametrize("team_factory,process_id,step_id,assignment", [
    (systems_architecture_team, "architecture-review", "establish-architecture-review-criteria",
     "define focused readiness questions, required evidence"),
])
def test_review_roles_can_author_their_assigned_criteria(
    tmp_path, team_factory, process_id, step_id, assignment
):
    team = team_factory()
    process = team.process(process_id)
    step = next(item for item in process.steps if item.id == step_id)
    prompt = build_prompt(team, process, step, tmp_path)
    assert assignment in prompt
    assert "create it when the current step calls for authoring" in prompt
    assert "Record findings in the assigned findings or results document" not in prompt


@pytest.mark.parametrize("team_factory,process_id,step_id,assignment", [
    (requirements_engineering_team, "stakeholder-needs-definition", "establish-needs-traceability",
     "Assign stable stakeholder-need identifiers and establish traceability"),
    (requirements_engineering_team, "requirements-definition", "baseline-requirements",
     "Prepare the requirements baseline"),
    (requirements_engineering_team, "requirements-validation", "prepare-requirements-validation",
     "On a correction pass, revise"),
    (requirements_engineering_team, "requirements-change-management", "establish-new-baseline",
     "Establish the revised requirements baseline"),
    (systems_architecture_team, "architecture-development", "baseline-system-architecture",
     "Establish the reviewed architecture baseline"),
    (systems_architecture_team, "interface-management", "establish-interface-definitions",
     "Finalize reviewed interface definitions"),
    (systems_architecture_team, "architecture-change-management", "update-architecture-baseline",
     "Document approved architectural changes in the baseline"),
])
def test_document_handoffs_allow_assigned_revision_and_baseline_work(
    tmp_path, team_factory, process_id, step_id, assignment
):
    team = team_factory()
    process = team.process(process_id)
    step = next(item for item in process.steps if item.id == step_id)
    prompt = build_prompt(team, process, step, tmp_path)
    assert assignment in prompt
    assert "including updates to assigned documents produced by earlier roles" in prompt
    assert "do not silently correct the subject or rewrite its baseline" not in prompt


def test_review_step_inventory_matches_real_builtin_steps():
    actual_ids = {step.id for team in TEAMS for process in team.processes for step in process.steps}
    assert REVIEW_STEPS <= actual_ids


def test_requirements_definition_is_three_steps_with_correction_routing():
    process = requirements_engineering_team().process("requirements-definition")
    assert [step.id for step in process.steps] == [
        "derive-requirements", "review-requirements", "baseline-requirements",
    ]
    assert [step.team_member_id for step in process.steps] == [
        "requirements-engineer", "verification-validation-engineer", "requirements-manager",
    ]
    assert [step.correction_target for step in process.steps] == [
        None, "derive-requirements", "derive-requirements",
    ]
    assert "previous review findings" in process.steps[1].prompt
    assert "Do not restart unrelated analysis" in process.steps[1].prompt

@pytest.mark.parametrize("process_id,step_ids,owners,targets", [
    (
        "stakeholder-needs-definition",
        ["develop-stakeholder-needs", "review-stakeholder-needs", "establish-needs-traceability"],
        ["requirements-engineer", "verification-validation-engineer", "requirements-manager"],
        [None, "develop-stakeholder-needs", "develop-stakeholder-needs"],
    ),
    (
        "requirements-validation",
        ["prepare-requirements-validation", "trace-requirements-to-needs",
         "evaluate-requirements-coverage", "update-validated-baseline"],
        ["requirements-engineer", "requirements-manager",
         "verification-validation-engineer", "requirements-manager"],
        [None, "prepare-requirements-validation", "prepare-requirements-validation",
         "prepare-requirements-validation"],
    ),
    (
        "requirements-change-management",
        ["analyze-change-request", "perform-impact-analysis", "develop-requirement-changes",
         "review-requirement-changes", "establish-new-baseline"],
        ["requirements-engineer", "requirements-manager", "requirements-engineer",
         "verification-validation-engineer", "requirements-manager"],
        [None, None, "perform-impact-analysis", "develop-requirement-changes",
         "develop-requirement-changes"],
    ),
    (
        "product-verification",
        ["identify-applicable-requirements", "collect-verification-evidence",
         "review-verification-results", "record-verification-status"],
        ["requirements-manager", "verification-validation-engineer",
         "requirements-engineer", "requirements-manager"],
        [None, None, "collect-verification-evidence", "collect-verification-evidence"],
    ),
    (
        "product-validation",
        ["identify-applicable-stakeholder-needs", "evaluate-need-satisfaction",
         "identify-requirements-deficiencies", "record-validation-results"],
        ["requirements-manager", "verification-validation-engineer",
         "requirements-engineer", "requirements-manager"],
        [None, None, "evaluate-need-satisfaction", "evaluate-need-satisfaction"],
    ),
])
def test_remaining_requirements_procedures_are_scoped_and_route_corrections(
    process_id, step_ids, owners, targets
):
    process = requirements_engineering_team().process(process_id)
    assert [step.id for step in process.steps] == step_ids
    assert [step.team_member_id for step in process.steps] == owners
    assert [step.correction_target for step in process.steps] == targets
    assert all(step.kind == "analysis" for step in process.steps)


def test_verification_execution_keeps_methods_evidence_and_negative_results(tmp_path):
    team = requirements_engineering_team()
    process = team.process("product-verification")
    step = next(step for step in process.steps if step.id == "collect-verification-evidence")
    prompt = build_prompt(team, process, step, tmp_path)
    assert "select or confirm a suitable verification method" in prompt
    assert "Collect and assess actual evidence" in prompt
    assert "PASS, FAIL, or NOT VERIFIED" in prompt
    assert "even when some product requirements FAIL" in prompt


def test_product_validation_does_not_reopen_genuine_product_failures():
    process = requirements_engineering_team().process("product-validation")
    evaluation = next(step for step in process.steps if step.id == "evaluate-need-satisfaction")
    findings = next(step for step in process.steps if step.id == "identify-requirements-deficiencies")
    assert "A supported unmet need is a valid finding" in evaluation.prompt
    assert "not because the product genuinely fails to meet a need" in findings.prompt


@pytest.mark.parametrize("process_id,step_ids,owners,targets", [
    (
        "architecture-development",
        ["analyze-architectural-drivers", "evaluate-architecture-alternatives",
         "define-architecture-interfaces", "review-system-architecture",
         "baseline-system-architecture"],
        ["systems-architect", "systems-architect", "interface-engineer",
         "architecture-reviewer", "systems-architect"],
        [None, "analyze-architectural-drivers", None,
         "evaluate-architecture-alternatives", "evaluate-architecture-alternatives"],
    ),
    (
        "interface-management",
        ["define-interface-contracts", "analyze-interface-compatibility",
         "review-interface-agreements", "establish-interface-definitions"],
        ["interface-engineer", "systems-architect", "architecture-reviewer",
         "interface-engineer"],
        [None, "define-interface-contracts", "define-interface-contracts",
         "define-interface-contracts"],
    ),
    (
        "architecture-review",
        ["establish-architecture-review-criteria", "prepare-architecture-evidence",
         "examine-architecture-and-evidence"],
        ["architecture-reviewer", "systems-architect", "architecture-reviewer"],
        [None, None, "prepare-architecture-evidence"],
    ),
    (
        "architecture-change-management",
        ["analyze-architecture-change", "assess-architecture-change-impacts",
         "evaluate-architecture-change-alternatives", "review-architecture-change",
         "update-architecture-baseline"],
        ["systems-architect", "interface-engineer", "systems-architect",
         "architecture-reviewer", "systems-architect"],
        [None, None, "assess-architecture-change-impacts",
         "evaluate-architecture-change-alternatives",
         "evaluate-architecture-change-alternatives"],
    ),
])
def test_architecture_procedures_have_expected_roles_and_correction_paths(
    process_id, step_ids, owners, targets
):
    process = systems_architecture_team().process(process_id)
    assert [step.id for step in process.steps] == step_ids
    assert [step.team_member_id for step in process.steps] == owners
    assert [step.correction_target for step in process.steps] == targets
    assert all(step.kind == "analysis" for step in process.steps)


def test_architecture_review_reports_rework_without_rerunning_it(tmp_path):
    team = systems_architecture_team()
    process = team.process("architecture-review")
    step = process.steps[-1]
    prompt = build_prompt(team, process, step, tmp_path)
    assert "one evidence-linked findings and readiness report" in prompt
    assert "Return CHANGES_REQUIRED to Prepare Architecture Evidence only when" in prompt
    assert "return PASS with that recommendation and findings" in prompt
    assert "system-level decision to the Project Manager" in prompt


def test_architecture_change_requires_impact_reassessment_on_expanded_scope(tmp_path):
    team = systems_architecture_team()
    process = team.process("architecture-change-management")
    step = process.steps[2]
    prompt = build_prompt(team, process, step, tmp_path)
    assert "materially alters the subsystem or interface impact scope" in prompt
    assert "return CHANGES_REQUIRED" in prompt
    assert step.correction_target == "assess-architecture-change-impacts"


def test_architecture_baselines_do_not_silently_approve_unreviewed_changes():
    team = systems_architecture_team()
    for process_id in ("architecture-development", "interface-management",
                       "architecture-change-management"):
        step = team.process(process_id).steps[-1]
        assert "unreviewed" in step.prompt
    change_step = team.process("architecture-change-management").steps[-1]
    assert "reviewed but unapproved proposals" in change_step.prompt
