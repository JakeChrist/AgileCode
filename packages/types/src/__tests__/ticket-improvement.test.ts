import { describe, expect, it } from "vitest"

import {
	proposedTicketImprovementSchema,
	ticketDraftFromImprovement,
	validateTicketImprovementOutput,
} from "../ticket-improvement.js"

const originalRequest = "Add CSV export to the board. The export must include ticket titles and statuses."
const proposal = {
	title: "Export the board as CSV",
	objective: "Let users export board ticket data.",
	context: "Users need a portable board snapshot.",
	includedScope: ["Add CSV export to the board."],
	excludedScope: ["Importing CSV files"],
	requirements: ["The export must include ticket titles and statuses."],
	deliverables: [],
	constraints: [],
	dependencies: [],
	acceptanceCriteria: ["CSV export includes ticket titles and statuses."],
	validation: ["Unit test CSV serialization and manually verify the editable draft."],
}

const validOutput = {
	proposal,
	unresolvedQuestions: ["Should archived tickets be included?"],
	assumptions: ["The existing board ordering should be retained."],
	codebaseEvidence: [{ path: "src/board.ts", observation: "The board exposes ticket title and status." }],
	scopeTraceability: [
		{ field: "includedScope", value: proposal.includedScope[0], requestExcerpt: "Add CSV export to the board." },
		{
			field: "requirements",
			value: proposal.requirements[0],
			requestExcerpt: "The export must include ticket titles and statuses.",
			evidenceIndexes: [0],
		},
		{
			field: "acceptanceCriteria",
			value: proposal.acceptanceCriteria[0],
			requestExcerpt: "include ticket titles and statuses",
		},
	],
}

describe("ticket improvement output", () => {
	it("accepts a complete proposed draft and keeps questions separate", () => {
		const result = validateTicketImprovementOutput(validOutput, originalRequest)
		expect(result.valid).toBe(true)
		if (!result.valid) return
		expect(result.value.unresolvedQuestions).toEqual(["Should archived tickets be included?"])
		expect(result.value.proposal.requirements).not.toContain(result.value.unresolvedQuestions[0])
	})

	it("returns actionable paths and preserves the request for malformed and partial output", () => {
		const malformed = validateTicketImprovementOutput("not structured", originalRequest)
		expect(malformed).toMatchObject({ valid: false, originalRequest })

		const partial = validateTicketImprovementOutput(
			{
				proposal: { ...proposal, objective: "" },
				unresolvedQuestions: [],
				assumptions: [],
				codebaseEvidence: [],
				scopeTraceability: [],
			},
			originalRequest,
		)
		expect(partial).toMatchObject({ valid: false, originalRequest })
		if (partial.valid) return
		expect(partial.issues.some((issue) => issue.path === "proposal.objective")).toBe(true)
	})

	it("rejects syntactically valid unrelated scope expansion", () => {
		const expanded = structuredClone(validOutput)
		expanded.proposal.requirements.push("Add a PDF export service.")
		expanded.scopeTraceability.push({
			field: "requirements",
			value: "Add a PDF export service.",
			requestExcerpt: "PDF export",
		})
		const result = validateTicketImprovementOutput(expanded, originalRequest)
		expect(result.valid).toBe(false)
		if (result.valid) return
		expect(result.issues).toContainEqual(expect.objectContaining({ code: "untraceable_scope" }))
	})

	it("rejects output that does not define executable work", () => {
		const incomplete = structuredClone(validOutput)
		incomplete.proposal.validation = []
		const result = validateTicketImprovementOutput(incomplete, originalRequest)
		expect(result.valid).toBe(false)
		if (result.valid) return
		expect(result.issues).toContainEqual(
			expect.objectContaining({ code: "incomplete_draft", path: "proposal.validation" }),
		)
	})

	it("round trips every proposed field into an independently editable draft", () => {
		const parsed = proposedTicketImprovementSchema.parse(validOutput)
		const draft = ticketDraftFromImprovement(parsed)
		expect(draft).toEqual(parsed.proposal)
		draft.title = "Edited title"
		expect(parsed.proposal.title).toBe("Export the board as CSV")
		expect(draft).not.toHaveProperty("unresolvedQuestions")
	})
})
