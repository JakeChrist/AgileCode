import { TICKET_FORMAT_VERSION, ticketSchema } from "../ticket.js"

const minimalTicket = {
	formatVersion: TICKET_FORMAT_VERSION,
	id: "AC-001",
	statementOfWork: {
		title: "Define the ticket contract",
		objective: "Provide one validated ticket definition.",
		context: "Board features need a durable contract.",
		requirements: ["Persist ticket information."],
		constraints: ["Do not embed transcripts."],
		includedScope: ["Ticket data."],
		dependencies: [],
		acceptanceCriteria: ["Valid records round trip."],
		validation: ["Run schema tests."],
	},
	lifecycle: {
		state: "backlog" as const,
		createdAt: "2026-08-14T12:00:00.000Z",
	},
	execution: { historyItemIds: [] },
}

describe("ticketSchema", () => {
	it("accepts a minimal executable statement of work", () => {
		const result = ticketSchema.parse(minimalTicket)

		expect(result.statementOfWork.deliverables).toBeUndefined()
		expect(result.lifecycle.reviewComments).toEqual([])
	})

	it("round trips every supported field without loss", () => {
		const complete = {
			...minimalTicket,
			statementOfWork: {
				...minimalTicket.statementOfWork,
				deliverables: ["A published TypeScript contract."],
				excludedScope: ["Board UI."],
				dependencies: ["AC-000"],
			},
			lifecycle: {
				state: "archived" as const,
				createdAt: "2026-08-14T12:00:00.000Z",
				completedAt: "2026-08-15T09:00:00.000Z",
				acceptedAt: "2026-08-15T10:00:00.000Z",
				archivedAt: "2026-08-15T11:00:00.000Z",
				archivedFrom: "accepted" as const,
				reviewComments: [
					{ id: "review-1", comment: "Looks good.", author: "Ada", createdAt: "2026-08-15T08:00:00.000Z" },
				],
				blockedReasons: [
					{
						reason: "Awaiting approval.",
						createdAt: "2026-08-14T13:00:00.000Z",
						resolvedAt: "2026-08-14T14:00:00.000Z",
					},
				],
				failedAttempts: [
					{
						historyItemId: "task-41",
						summary: "Type checking failed.",
						failedAt: "2026-08-14T15:00:00.000Z",
					},
				],
			},
			execution: { historyItemIds: ["task-41", "task-42"] },
		}

		expect(ticketSchema.parse(JSON.parse(JSON.stringify(complete)))).toEqual(complete)
	})

	it.each([
		[
			"unknown workflow state",
			{ ...minimalTicket, lifecycle: { ...minimalTicket.lifecycle, state: "queued" } },
			["lifecycle", "state"],
		],
		["malformed identifier", { ...minimalTicket, id: "ac 1" }, ["id"]],
		["unknown format version", { ...minimalTicket, formatVersion: 2 }, ["formatVersion"]],
		[
			"missing mandatory SOW content",
			{ ...minimalTicket, statementOfWork: { ...minimalTicket.statementOfWork, objective: undefined } },
			["statementOfWork", "objective"],
		],
		[
			"empty mandatory SOW content",
			{ ...minimalTicket, statementOfWork: { ...minimalTicket.statementOfWork, requirements: [] } },
			["statementOfWork", "requirements"],
		],
	])("rejects %s with a specific path", (_name, record, expectedPath) => {
		const result = ticketSchema.safeParse(record)

		expect(result.success).toBe(false)
		if (!result.success) expect(result.error.issues[0]?.path).toEqual(expectedPath)
	})

	it("requires an unresolved reason while blocked", () => {
		const result = ticketSchema.safeParse({
			...minimalTicket,
			lifecycle: { ...minimalTicket.lifecycle, state: "blocked" },
		})
		expect(result.success).toBe(false)
		if (!result.success) expect(result.error.issues[0]?.message).toContain("unresolved blocked reason")
	})

	it("requires complete archive metadata and rejects it in other states", () => {
		expect(
			ticketSchema.safeParse({ ...minimalTicket, lifecycle: { ...minimalTicket.lifecycle, state: "archived" } })
				.success,
		).toBe(false)
		expect(
			ticketSchema.safeParse({
				...minimalTicket,
				lifecycle: { ...minimalTicket.lifecycle, archivedAt: "2026-08-15T11:00:00.000Z" },
			}).success,
		).toBe(false)
	})

	it("rejects embedded transcripts and terminal logs", () => {
		const result = ticketSchema.safeParse({
			...minimalTicket,
			execution: {
				historyItemIds: ["task-41"],
				transcript: [{ role: "user", content: "secret" }],
				terminalLogs: "output",
			},
		})

		expect(result.success).toBe(false)
	})
})
