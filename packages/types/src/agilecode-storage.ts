import { z } from "zod"

import { boardScopeSchema } from "./board-scope.js"
import { ticketIdSchema, ticketSchema, type Ticket, type TicketWorkflowState } from "./ticket.js"

/** Version of the project-local `.agilecode` directory contract. */
export const AGILECODE_STORE_FORMAT_VERSION = 1 as const

export const activeBoardStates = ["backlog", "ready", "in_progress", "blocked", "review", "done"] as const

const orderedTicketIdsSchema = z.array(ticketIdSchema)

/** Ordering only; ticket content and lifecycle metadata remain in individual records. */
export const agileCodeBoardSchema = z
	.object({
		formatVersion: z.literal(AGILECODE_STORE_FORMAT_VERSION),
		columns: z
			.object({
				backlog: orderedTicketIdsSchema,
				ready: orderedTicketIdsSchema,
				in_progress: orderedTicketIdsSchema,
				blocked: orderedTicketIdsSchema,
				review: orderedTicketIdsSchema,
				done: orderedTicketIdsSchema,
			})
			.strict(),
		archiveOrder: orderedTicketIdsSchema,
	})
	.strict()
	.superRefine(({ columns, archiveOrder }, context) => {
		const seen = new Set<string>()
		for (const state of activeBoardStates) {
			for (const id of columns[state]) {
				if (seen.has(id)) {
					context.addIssue({
						code: z.ZodIssueCode.custom,
						path: ["columns", state],
						message: `Ticket ${id} may appear only once on the board`,
					})
				}
				seen.add(id)
			}
		}
		for (const id of archiveOrder) {
			if (seen.has(id)) {
				context.addIssue({
					code: z.ZodIssueCode.custom,
					path: ["archiveOrder"],
					message: `Ticket ${id} may appear only once in board ordering`,
				})
			}
			seen.add(id)
		}
	})

export type AgileCodeBoard = z.infer<typeof agileCodeBoardSchema>

/** Portable repository-owned board preferences; secrets and machine settings are excluded. */
export const agileCodeRepositorySettingsSchema = z
	.object({
		formatVersion: z.literal(AGILECODE_STORE_FORMAT_VERSION),
		automaticArchival: z
			.object({ enabled: z.boolean(), retentionDays: z.number().int().min(1).max(36_500) })
			.strict(),
		repositorySelection: z.object({ preferredScopeId: z.string().min(1).max(256).nullable() }).strict(),
		showArchived: z.boolean(),
		suppressDragToExecuteWarning: z.boolean(),
		workflowPreferences: z.record(z.string(), z.union([z.string(), z.number(), z.boolean(), z.null()])),
	})
	.strict()

export type AgileCodeRepositorySettings = z.infer<typeof agileCodeRepositorySettingsSchema>

/** The scope is recorded for diagnostics; the directory remains rooted inside that scope. */
export const agileCodeStoreManifestSchema = z
	.object({
		formatVersion: z.literal(AGILECODE_STORE_FORMAT_VERSION),
		scope: boardScopeSchema,
	})
	.strict()

export type AgileCodeStoreManifest = z.infer<typeof agileCodeStoreManifestSchema>

export const agileCodeProjectStoreSchema = z
	.object({
		manifest: agileCodeStoreManifestSchema,
		board: agileCodeBoardSchema,
		settings: agileCodeRepositorySettingsSchema,
		activeTickets: z.array(ticketSchema),
		archivedTickets: z.array(ticketSchema),
	})
	.strict()
	.superRefine((store, context) => {
		const records = new Map<string, { ticket: Ticket; location: "activeTickets" | "archivedTickets" }>()
		for (const location of ["activeTickets", "archivedTickets"] as const) {
			for (const [index, ticket] of store[location].entries()) {
				if (records.has(ticket.id)) {
					context.addIssue({
						code: z.ZodIssueCode.custom,
						path: [location, index, "id"],
						message: `Ticket ${ticket.id} has more than one record`,
					})
				}
				records.set(ticket.id, { ticket, location })

				const shouldBeArchived = location === "archivedTickets"
				if ((ticket.lifecycle.state === "archived") !== shouldBeArchived) {
					context.addIssue({
						code: z.ZodIssueCode.custom,
						path: [location, index, "lifecycle", "state"],
						message: `${ticket.id} is stored in the wrong directory for its workflow state`,
					})
				}
			}
		}

		const placements = new Map<string, TicketWorkflowState>()
		for (const state of activeBoardStates) {
			for (const id of store.board.columns[state]) placements.set(id, state)
		}

		for (const [index, ticket] of store.activeTickets.entries()) {
			const placement = placements.get(ticket.id)
			if (!placement) {
				context.addIssue({
					code: z.ZodIssueCode.custom,
					path: ["activeTickets", index, "id"],
					message: `Active ticket ${ticket.id} is missing from the board`,
				})
			} else if (placement !== ticket.lifecycle.state) {
				context.addIssue({
					code: z.ZodIssueCode.custom,
					path: ["board", "columns", placement],
					message: `${ticket.id} is ordered under ${placement} but its state is ${ticket.lifecycle.state}`,
				})
			}
		}

		for (const [id, state] of placements) {
			if (!records.has(id)) {
				context.addIssue({
					code: z.ZodIssueCode.custom,
					path: ["board", "columns", state],
					message: `Board entry ${id} has no active ticket record`,
				})
			}
		}

		const archivedIds = new Set(store.archivedTickets.map(({ id }) => id))
		for (const id of store.board.archiveOrder) {
			if (!archivedIds.has(id)) {
				context.addIssue({
					code: z.ZodIssueCode.custom,
					path: ["board", "archiveOrder"],
					message: `Archive entry ${id} has no archived ticket record`,
				})
			}
		}
		for (const [index, ticket] of store.archivedTickets.entries()) {
			if (!store.board.archiveOrder.includes(ticket.id)) {
				context.addIssue({
					code: z.ZodIssueCode.custom,
					path: ["archivedTickets", index, "id"],
					message: `Archived ticket ${ticket.id} is missing from archive ordering`,
				})
			}
		}
	})

export type AgileCodeProjectStore = z.infer<typeof agileCodeProjectStoreSchema>
