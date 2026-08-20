import { TICKET_FORMAT_VERSION, type Ticket, type TicketStatementOfWork } from "../ticket.js"
import { validateTicketExecutionEligibility, validateTicketReadiness } from "../ticket-validation.js"

const complete: TicketStatementOfWork = {
	title: "Validate ticket readiness",
	objective: "Prevent incomplete work from executing.",
	context: "Saved records and executable tickets have different quality bars.",
	requirements: ["Return field-specific validation issues."],
	constraints: [],
	includedScope: ["Readiness decisions."],
	dependencies: [],
	acceptanceCriteria: ["A complete ticket is eligible."],
	validation: ["Run table-driven unit tests."],
}

const ticket = (id: string, state: Ticket["lifecycle"]["state"], sow = complete): Ticket => ({
	formatVersion: TICKET_FORMAT_VERSION,
	id,
	statementOfWork: sow,
	lifecycle: {
		state,
		createdAt: "2026-08-20T00:00:00.000Z",
		reviewComments: [],
		blockedReasons: [],
		failedAttempts: [],
	},
	execution: { historyItemIds: [] },
})

describe("ticket readiness validation", () => {
	it.each<[string, TicketStatementOfWork, boolean, string[]]>([
		["complete", complete, true, []],
		[
			"incomplete",
			{ ...complete, objective: "", requirements: [], includedScope: [], acceptanceCriteria: [], validation: [] },
			false,
			["objective", "includedScope", "requirements", "acceptanceCriteria", "validation"],
		],
		[
			"irrelevant optional fields omitted",
			{ ...complete, context: "", constraints: [], deliverables: undefined, excludedScope: undefined },
			true,
			[],
		],
	])("classifies %s content", (_name, statementOfWork, ready, fields) => {
		const result = validateTicketReadiness(statementOfWork)
		expect(result.ready).toBe(ready)
		expect(result.issues.map(({ field }) => field)).toEqual(fields)
	})

	it("rejects a ticket whose required dependency is not complete", () => {
		const subject = ticket("AC-030", "ready", { ...complete, dependencies: ["AC-001", "AC-002"] })
		const result = validateTicketExecutionEligibility(subject, [
			subject,
			ticket("AC-001", "done"),
			ticket("AC-002", "blocked"),
		])

		expect(result).toEqual({
			ready: false,
			issues: [{ field: "dependencies", message: "Required dependencies are unresolved: AC-002" }],
		})
	})

	it("uses an archived prerequisite's preserved state for execution eligibility", () => {
		const subject = ticket("AC-030", "ready", { ...complete, dependencies: ["AC-001"] })
		const prerequisite = ticket("AC-001", "archived", complete)
		prerequisite.lifecycle.archivedAt = "2026-08-20T00:00:00.000Z"
		prerequisite.lifecycle.archivedFrom = "done"

		expect(validateTicketExecutionEligibility(subject, [prerequisite])).toEqual({ ready: true, issues: [] })
		prerequisite.lifecycle.archivedFrom = "ready"
		expect(validateTicketExecutionEligibility(subject, [prerequisite])).toMatchObject({
			ready: false,
			issues: [{ field: "dependencies", message: "Required dependencies are unresolved: AC-001" }],
		})
	})
})
