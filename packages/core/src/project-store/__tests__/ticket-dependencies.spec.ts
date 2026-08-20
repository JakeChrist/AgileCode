import type { Ticket } from "@roo-code/types"
import { describe, expect, it } from "vitest"

import { dependencyStatus, validateTicketDependencies } from "../ticket-dependencies.js"

const ticket = (id: string, dependencies: string[] = []): Ticket => ({
	formatVersion: 1,
	id,
	statementOfWork: {
		title: id,
		objective: "objective",
		context: "",
		requirements: ["requirement"],
		constraints: [],
		includedScope: ["scope"],
		dependencies,
		acceptanceCriteria: ["criterion"],
		validation: ["test"],
	},
	lifecycle: {
		state: "backlog",
		createdAt: "2026-08-20T00:00:00.000Z",
		reviewComments: [],
		blockedReasons: [],
		failedAttempts: [],
	},
	execution: { historyItemIds: [] },
})

describe("ticket dependency graph", () => {
	it("accepts an acyclic board-local graph", () => {
		expect(() =>
			validateTicketDependencies(
				"AC-003",
				["AC-002"],
				[ticket("AC-001"), ticket("AC-002", ["AC-001"]), ticket("AC-003")],
			),
		).not.toThrow()
	})

	it.each([
		["self dependency", "AC-001", ["AC-001"], [ticket("AC-001")], /itself/],
		["cycle", "AC-001", ["AC-002"], [ticket("AC-001"), ticket("AC-002", ["AC-001"])], /cycle/],
		["missing ticket", "AC-001", ["AC-404"], [ticket("AC-001")], /Unknown/],
		["cross-repository reference", "AC-001", ["other/AC-002"], [ticket("AC-001")], /board-local/],
	] as const)("rejects a %s", (_name, id, dependencies, tickets, message) => {
		expect(() => validateTicketDependencies(id, dependencies, tickets)).toThrow(message)
	})

	it("reports archived prerequisites according to their preserved state", () => {
		const completed = ticket("AC-001")
		completed.lifecycle = {
			...completed.lifecycle,
			state: "archived",
			archivedFrom: "done",
			archivedAt: "2026-08-20T01:00:00.000Z",
		}
		const unresolved = ticket("AC-002")
		unresolved.lifecycle = {
			...unresolved.lifecycle,
			state: "archived",
			archivedFrom: "ready",
			archivedAt: "2026-08-20T01:00:00.000Z",
		}
		expect(dependencyStatus(completed)).toBe("archived-completed")
		expect(dependencyStatus(unresolved)).toBe("archived-unresolved")
		expect(dependencyStatus(undefined)).toBe("missing")
	})
})
