import {
	AGILECODE_STORE_FORMAT_VERSION,
	agileCodeProjectStoreSchema,
	type AgileCodeProjectStore,
} from "../agilecode-storage.js"
import { TICKET_FORMAT_VERSION, type Ticket } from "../ticket.js"

const ticket = (id: string, state: "ready" | "archived"): Ticket => ({
	formatVersion: TICKET_FORMAT_VERSION,
	id,
	statementOfWork: {
		title: `Implement ${id}`,
		objective: "Keep project work transparent.",
		context: "The board needs an independently editable record.",
		requirements: ["Store one ticket per file."],
		constraints: ["Do not store transcripts."],
		includedScope: ["Project-local storage."],
		dependencies: [],
		acceptanceCriteria: ["The record validates."],
		validation: ["Run schema tests."],
	},
	lifecycle:
		state === "archived"
			? {
					state,
					createdAt: "2026-08-14T12:00:00.000Z",
					archivedAt: "2026-08-16T12:00:00.000Z",
					archivedFrom: "done",
					reviewComments: [{ id: "review-1", comment: "Accepted.", createdAt: "2026-08-15T12:00:00.000Z" }],
					blockedReasons: [],
					failedAttempts: [],
				}
			: {
					state,
					createdAt: "2026-08-14T12:00:00.000Z",
					reviewComments: [],
					blockedReasons: [],
					failedAttempts: [],
				},
	execution: { historyItemIds: state === "archived" ? ["task-19"] : [] },
})

const completeStore: AgileCodeProjectStore = {
	manifest: {
		formatVersion: AGILECODE_STORE_FORMAT_VERSION,
		scope: { id: `git:${"a".repeat(64)}`, kind: "git", rootPath: "/work/project" },
	},
	board: {
		formatVersion: AGILECODE_STORE_FORMAT_VERSION,
		columns: { backlog: [], ready: ["AC-004"], in_progress: [], blocked: [], review: [], done: [] },
		archiveOrder: ["AC-003"],
	},
	settings: {
		formatVersion: AGILECODE_STORE_FORMAT_VERSION,
		automaticArchival: { enabled: true, retentionDays: 30 },
		repositorySelection: { preferredScopeId: `git:${"b".repeat(64)}` },
		showArchived: true,
		suppressDragToExecuteWarning: false,
		workflowPreferences: {},
	},
	activeTickets: [ticket("AC-004", "ready")],
	archivedTickets: [ticket("AC-003", "archived")],
}

describe("agileCodeProjectStoreSchema", () => {
	it.each(["git", "workspace"] as const)("validates a complete %s-backed example", (kind) => {
		const store = structuredClone(completeStore)
		store.manifest.scope.kind = kind
		store.manifest.scope.id = `${kind}:${"a".repeat(64)}`

		expect(agileCodeProjectStoreSchema.safeParse(store).success).toBe(true)
	})

	it("rejects duplicate board entries", () => {
		const store = structuredClone(completeStore)
		store.board.columns.ready.push("AC-004")
		expect(agileCodeProjectStoreSchema.safeParse(store).success).toBe(false)
	})

	it("requires every board entry to match an independently stored active ticket", () => {
		const store = structuredClone(completeStore)
		store.board.columns.ready = ["AC-999"]
		expect(agileCodeProjectStoreSchema.safeParse(store).success).toBe(false)
	})

	it("requires archived state and preserves historical references", () => {
		const store = structuredClone(completeStore)
		store.archivedTickets[0]!.lifecycle = {
			state: "done",
			createdAt: "2026-08-14T12:00:00.000Z",
			reviewComments: [],
			blockedReasons: [],
			failedAttempts: [],
		}
		expect(agileCodeProjectStoreSchema.safeParse(store).success).toBe(false)
		expect(completeStore.archivedTickets[0]?.execution.historyItemIds).toEqual(["task-19"])
	})

	it("rejects transcript-shaped additions to every strict record", () => {
		const store = { ...structuredClone(completeStore), transcript: [{ role: "assistant", content: "response" }] }
		expect(agileCodeProjectStoreSchema.safeParse(store).success).toBe(false)
	})
})
