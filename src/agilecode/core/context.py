"""Pinned process handoffs and coherent tool-exchange context selection."""
from __future__ import annotations
import json
import re
from agilecode.core.types import AgileCodeError, Step, StepContext, ModelProfile


def requirement_ids(output: str) -> list[str]:
    return list(dict.fromkeys(re.findall(r"\bREQ-\d+\b", output)))


class PromptBuilder:
    def build(self, step: Step, context: StepContext, skills: list[dict]) -> str:
        member = context.team.member(step.team_member_id)
        parts = [member.system_prompt,
                 f"CURRENT STEP: {step.name} ({step.kind})\n{step.prompt}",
                 "PROCESS AUTHORITY: The application controls the sequence. Never skip stages, commit early, merge, push, or change the ticket branch.",
                 f"TICKET: {context.ticket.get('key', '')} — {context.ticket['title']}\n{context.ticket['description']}",
                 f"WORKTREE: {context.workspace}\nAll relative paths default here. Original project/global context is read-only. Shell state persists while its process lives; it is not an OS sandbox.",
                 "TOOLS: Use native function calls. finish_step must be called ALONE with status, summary, output when your assigned step is complete. ask_user must also be alone. Ordinary text is not step completion.",
                 "EVIDENCE: Tool results include _execution_id. Verification must call record_check for every REQ-ID, referring to actual read/test/diff execution IDs. A claim in text is not execution evidence."]
        for identity, output in context.outputs.items():
            parts.append(f"PRIOR STEP ARTIFACT [{identity}] ({output.status})\n{output.output}")
        if context.correction_report:
            parts.append("CURRENT CORRECTION FINDINGS\n" + context.correction_report)
        if context.extra:
            parts.append("ADDITIONAL RECORDED CONTEXT\n" + json.dumps(context.extra, ensure_ascii=False, default=str))
        eligible = [s for s in skills if not s.get("error") and not s.get("overridden")]
        if eligible:
            parts.append("AVAILABLE SKILLS (load only when relevant)\n" + "\n".join(f"{s['id']}: {s['description']}" for s in eligible))
        return "\n\n".join(parts)


class ContextManager:
    """Keeps the exact system/handoff and newest complete exchanges.

    Older transcript text is never deleted from SQLite. When compacted, an explicit
    bounded extract identifies omitted turns; no fabricated LLM summary is created.
    """
    def prepare(self, messages: list[dict], profile: ModelProfile) -> tuple[list[dict], dict]:
        budget = profile.context_window - profile.max_tokens - 512
        def estimate(items):
            # Deliberately labeled heuristic; not a model-specific tokenizer.
            return (len(json.dumps(items, ensure_ascii=False).encode("utf-8")) + 2) // 3
        if not messages:
            return [], {"estimated_input_tokens": 0, "estimator": "utf8_bytes/3"}
        pinned = [m for m in messages[:1] if m["role"] == "system"]
        rest = messages[len(pinned):]
        if estimate(pinned) > budget:
            raise AgileCodeError("Exact ticket requirements/plan exceed this model's context budget. Increase the configured context or deliberately revise the ticket; no constraints were dropped.")
        groups: list[list[dict]] = []
        for message in rest:
            if message["role"] == "tool" and groups:
                groups[-1].append(message)
            else:
                groups.append([message])
        chosen = list(groups)
        omitted = 0
        while chosen and estimate(pinned + [m for g in chosen for m in g]) > budget:
            if len(chosen) == 1:
                raise AgileCodeError("The newest tool exchange exceeds the context budget. Reduce tool output or increase context; the exchange was not split.")
            omitted += len(chosen.pop(0))
        result = pinned + [m for group in chosen for m in group]
        if omitted:
            notice = {"role": "user", "content": f"Context window notice: {omitted} older conversation messages are omitted here, but retained in project history. Exact ticket, plan, requirements and correction artifacts remain in the system context."}
            while len(chosen) > 1 and estimate(pinned + [notice] + [m for g in chosen for m in g]) > budget:
                omitted += len(chosen.pop(0))
                notice["content"] = f"Context window notice: {omitted} older messages omitted; full history is retained. Exact process artifacts remain pinned."
            result = pinned + [notice] + [m for g in chosen for m in g]
            if estimate(result) > budget:
                raise AgileCodeError("Pinned context plus newest exchange exceeds the configured budget.")
        return result, {"estimated_input_tokens": estimate(result), "estimator": "utf8_bytes/3", "omitted_messages": omitted}
