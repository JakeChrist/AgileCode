import {
	decideTicketTransition,
	findExecutionSlotOccupant,
	getTicketStatementOfWorkLock,
	ticketWorkflowStateDefinitions,
	type TicketTransitionContext,
	type TicketWorkflowAction,
} from "../ticket-workflow.js"

const decide = (
	state: TicketTransitionContext["state"],
	execution: TicketTransitionContext["execution"],
	action: TicketWorkflowAction,
) => decideTicketTransition({ state, execution }, action)

describe("ticket workflow contract", () => {
	it.each([
		["in_progress", ["task-1"], true],
		["blocked", ["task-1"], true],
		["blocked", [], false],
		["ready", ["task-1"], false],
		["review", ["task-1"], false],
	] as const)(
		"classifies %s execution history %o as occupying the repository slot: %s",
		(state, history, occupied) => {
			const candidate = {
				id: "AC-073",
				lifecycle: { state } as never,
				execution: { historyItemIds: [...history] },
			}
			expect(findExecutionSlotOccupant([candidate])?.id).toBe(occupied ? candidate.id : undefined)
			expect(findExecutionSlotOccupant([candidate], candidate.id)).toBeUndefined()
		},
	)

	it("keeps execution-slot decisions repository scoped", () => {
		const running = {
			id: "AC-073",
			lifecycle: { state: "in_progress" } as never,
			execution: { historyItemIds: ["task-1"] },
		}
		expect(findExecutionSlotOccupant([running])?.id).toBe("AC-073")
		expect(findExecutionSlotOccupant([])).toBeUndefined()
	})

	it.each([
		["in_progress", [], true],
		["blocked", ["task-1"], true],
		["blocked", [], false],
		["ready", ["task-1"], false],
	] as const)("derives the statement-of-work lock for %s with execution history %o", (state, history, locked) => {
		expect(
			getTicketStatementOfWorkLock({
				lifecycle: { state } as never,
				execution: { historyItemIds: [...history] },
			}),
		).toMatchObject({ locked })
	})

	it("defines exactly the seven customer-visible states", () => {
		expect(Object.keys(ticketWorkflowStateDefinitions)).toEqual([
			"backlog",
			"ready",
			"in_progress",
			"blocked",
			"review",
			"done",
			"archived",
		])
	})

	it.each([
		["backlog", "ready"],
		["backlog", "blocked"],
		["backlog", "backlog"],
		["ready", "backlog"],
		["ready", "blocked"],
		["ready", "ready"],
		["blocked", "backlog"],
		["blocked", "ready"],
		["blocked", "blocked"],
	] as const)("permits ordinary non-executing movement from %s to %s", (source, destination) => {
		expect(decide(source, "none", { type: "move", destination, actor: "user" })).toMatchObject({
			allowed: true,
			state: destination,
			effect: "metadata",
		})
	})

	it("requests execution without prematurely displaying In Progress", () => {
		expect(decide("ready", "none", { type: "move", destination: "in_progress", actor: "user" })).toEqual({
			allowed: true,
			state: "ready",
			effect: "runtime",
			operation: "request_execution",
		})
		expect(decide("ready", "active", { type: "execution_started" })).toMatchObject({
			allowed: true,
			state: "in_progress",
		})
	})

	it.each([
		["in_progress", "resumable", { type: "waiting_for_user", reason: "Need credentials" }, "blocked"],
		["in_progress", "none", { type: "technical_failure", summary: "Build failed" }, "blocked"],
		["in_progress", "none", { type: "execution_completed" }, "review"],
		["review", "none", { type: "review_rejected", comment: "Fix the copy" }, "ready"],
		["review", "none", { type: "corrective_work_requested", comment: "Add a test" }, "ready"],
		["review", "none", { type: "accept" }, "done"],
	] as const)("maps %s %s via %o to %s", (source, execution, action, destination) => {
		expect(decide(source, execution, action)).toMatchObject({ allowed: true, state: destination })
	})

	it("defines pause/resume and cancellation as runtime actions", () => {
		expect(decide("blocked", "resumable", { type: "move", destination: "in_progress", actor: "user" })).toEqual({
			allowed: true,
			state: "blocked",
			effect: "runtime",
			operation: "request_resume",
		})
		expect(decide("blocked", "resumable", { type: "cancel", actor: "user" })).toMatchObject({
			allowed: true,
			state: "ready",
			effect: "runtime",
			operation: "cancel_execution",
		})
	})

	it.each([
		["in_progress", "active", { type: "move", destination: "backlog", actor: "user" }],
		["blocked", "resumable", { type: "move", destination: "ready", actor: "user" }],
		["backlog", "none", { type: "move", destination: "in_progress", actor: "agent" }],
		["ready", "none", { type: "execution_started" }],
		["review", "none", { type: "move", destination: "done", actor: "user" }],
	] as const)("rejects contradictory transition from %s with %s execution", (source, execution, action) => {
		expect(decide(source, execution, action)).toMatchObject({ allowed: false, state: source })
	})

	it("archives with history, restores it, and confirms permanent deletion", () => {
		const archived = decide("review", "none", { type: "archive" })
		expect(archived).toMatchObject({ allowed: true, state: "archived", archivedFrom: "review" })
		expect(
			decideTicketTransition(
				{ state: "archived", execution: "none", archivedFrom: "review" },
				{ type: "restore" },
			),
		).toMatchObject({ allowed: true, state: "review", operation: "restore" })
		expect(decide("archived", "none", { type: "delete_permanently", confirmed: false })).toMatchObject({
			allowed: false,
		})
		expect(decide("archived", "none", { type: "delete_permanently", confirmed: true })).toMatchObject({
			allowed: true,
			effect: "user_confirmation",
			operation: "delete",
		})
	})
})
