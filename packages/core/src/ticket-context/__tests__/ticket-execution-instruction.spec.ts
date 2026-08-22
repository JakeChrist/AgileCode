import type { BoardScope, Ticket, TicketWorkflowState } from "@roo-code/types"
import { describe, expect, it, vi } from "vitest"

import { compileTicketExecutionInstruction, formatTicketExecutionInstruction } from "../ticket-execution-instruction.js"

const board: BoardScope = { id: "git:board-one", kind: "git", rootPath: "/repo" }
const timestamp = "2026-08-14T00:00:00.000Z"

function ticket(
	id = "AC-067",
	state: TicketWorkflowState = "ready",
	overrides: Partial<Ticket["statementOfWork"]> = {},
): Ticket {
	return {
		formatVersion: 1,
		id,
		statementOfWork: {
			title: "Compile a Ticket",
			objective: "Create the authoritative execution instruction.",
			context: "The ticket is the contract for the run.",
			includedScope: ["Deterministic instruction content"],
			excludedScope: ["Unrelated Chat behavior"],
			requirements: ["Identify the ticket and board"],
			deliverables: ["A task-ready instruction"],
			constraints: ["Preserve existing Chat behavior"],
			dependencies: [],
			acceptanceCriteria: ["Every approved section is present"],
			validation: ["Run structured-content tests"],
			...overrides,
		},
		lifecycle: {
			state,
			createdAt: timestamp,
			reviewComments: [],
			blockedReasons: state === "blocked" ? [{ reason: "Waiting", createdAt: timestamp }] : [],
			failedAttempts: [],
			...(state === "archived" ? { archivedAt: timestamp, archivedFrom: "ready" as const } : {}),
		},
		execution: { historyItemIds: state === "in_progress" ? ["task-1"] : [] },
	}
}

function service(active: Ticket[], archived: Ticket[] = []) {
	return {
		listTickets: () => active,
		listArchivedTickets: () => archived,
		dispose: vi.fn(async () => undefined),
	}
}

const compile = (active: Ticket[], archived: Ticket[] = [], selectedBoardId = board.id) =>
	compileTicketExecutionInstruction(board, "AC-067", {
		selectedBoardId,
		createService: vi.fn(async () => service(active, archived)) as never,
	})

describe("ticket execution instruction", () => {
	it("deterministically includes every complete approved statement-of-work section", async () => {
		const result = await compile([ticket()])
		expect(result.ok).toBe(true)
		if (!result.ok) return
		expect(result.instruction).toMatchSnapshot()
		expect(result.instruction).not.toContain("historyItemIds")
	})

	it("formats a minimal executable ticket without inventing optional sections", () => {
		const instruction = formatTicketExecutionInstruction(
			ticket("AC-067", "ready", { context: "", excludedScope: undefined, deliverables: undefined }),
			board,
		)
		expect(instruction).toContain("## Objective")
		expect(instruction).not.toContain("## Context")
		expect(instruction).not.toContain("## Deliverables")
	})

	it.each([
		["incomplete", [ticket("AC-067", "ready", { objective: "" })], [], "ticket-not-ready"],
		[
			"dependency-blocked",
			[ticket("AC-067", "ready", { dependencies: ["AC-001"] }), ticket("AC-001", "ready")],
			[],
			"dependency-blocked",
		],
		["locked", [ticket("AC-067", "in_progress")], [], "ticket-locked"],
		["archived", [], [ticket("AC-067", "archived")], "ticket-archived"],
	] as const)("rejects an %s ticket without an instruction", async (_name, active, archived, code) => {
		const result = await compile([...active], [...archived])
		expect(result).toMatchObject({ ok: false, code })
		expect(result).not.toHaveProperty("instruction")
	})

	it("rejects a ticket selected from another repository before loading a board", async () => {
		const result = await compile([ticket()], [], "git:another-board")
		expect(result).toMatchObject({ ok: false, code: "repository-mismatch" })
		expect(result).not.toHaveProperty("instruction")
	})
})
