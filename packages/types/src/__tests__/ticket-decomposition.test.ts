import { describe, expect, it } from "vitest"

import { ticketSetProposalSchema } from "../ticket-decomposition.js"

const sow = (title: string) => ({
	title,
	objective: "Objective",
	context: "Context",
	requirements: ["Requirement"],
	constraints: [],
	includedScope: ["Scope"],
	dependencies: [],
	acceptanceCriteria: ["Accepted"],
	validation: ["Test"],
})

describe("ticketSetProposalSchema", () => {
	it("accepts traceable acyclic decompositions", () => {
		const result = ticketSetProposalSchema.parse({
			sourceWorkDefinition: "Authoritative SOW",
			tickets: [
				{ proposalId: "api", statementOfWork: sow("API"), dependsOn: [], sourceItems: ["R1"] },
				{ proposalId: "ui", statementOfWork: sow("UI"), dependsOn: ["api"], sourceItems: ["R2"] },
			],
			unassignedSourceItems: ["Deferred deliverable"],
		})
		expect(result.tickets[1]!.dependsOn).toEqual(["api"])
	})

	it.each([
		[[{ proposalId: "one", statementOfWork: sow("One"), dependsOn: ["missing"], sourceItems: ["R1"] }], "Unknown"],
		[
			[
				{ proposalId: "one", statementOfWork: sow("One"), dependsOn: ["two"], sourceItems: ["R1"] },
				{ proposalId: "two", statementOfWork: sow("Two"), dependsOn: ["one"], sourceItems: ["R2"] },
			],
			"cycle",
		],
	])("rejects invalid dependency graphs", (tickets, message) => {
		expect(() =>
			ticketSetProposalSchema.parse({ sourceWorkDefinition: "SOW", tickets, unassignedSourceItems: [] }),
		).toThrow(message)
	})
})
