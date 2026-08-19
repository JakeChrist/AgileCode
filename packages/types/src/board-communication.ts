import { z } from "zod"

import { agileCodeBoardSchema, agileCodeRepositorySettingsSchema } from "./agilecode-storage.js"
import { boardScopeSchema } from "./board-scope.js"
import type { BoardScope } from "./board-scope.js"
import { ticketIdSchema, ticketSchema, ticketStatementOfWorkSchema } from "./ticket.js"
import { ticketWorkflowStateSchema } from "./ticket.js"

/** Operations which may cross the extension/webview boundary for the board. */
export const boardOperationSchema = z.enum([
	"load_board",
	"initialize_board",
	"get_ticket",
	"create_ticket",
	"update_ticket",
	"move_ticket",
	"improve_ticket_draft",
	"start_ticket_execution",
	"cancel_ticket_execution",
	"accept_ticket",
	"reject_ticket",
	"archive_ticket",
	"restore_ticket",
	"delete_ticket",
	"update_board_settings",
])

export type BoardOperation = z.infer<typeof boardOperationSchema>

const requestIdSchema = z.string().trim().min(1)

const requestBase = {
	requestId: requestIdSchema,
	boardId: boardScopeSchema.shape.id,
}

/** Webview -> extension requests. Required fields are specific to each operation. */
export const boardRequestSchema = z.discriminatedUnion("operation", [
	z.object({ ...requestBase, operation: z.literal("load_board") }).strict(),
	z.object({ ...requestBase, operation: z.literal("initialize_board") }).strict(),
	z.object({ ...requestBase, operation: z.literal("get_ticket"), ticketId: ticketIdSchema }).strict(),
	z
		.object({
			...requestBase,
			operation: z.literal("create_ticket"),
			ticket: ticketStatementOfWorkSchema,
		})
		.strict(),
	z
		.object({
			...requestBase,
			operation: z.literal("update_ticket"),
			ticketId: ticketIdSchema,
			statementOfWork: ticketStatementOfWorkSchema,
		})
		.strict(),
	z
		.object({
			...requestBase,
			operation: z.literal("move_ticket"),
			ticketId: ticketIdSchema,
			destination: ticketWorkflowStateSchema.exclude(["archived"]),
			position: z.number().int().nonnegative(),
		})
		.strict(),
	z
		.object({
			...requestBase,
			operation: z.literal("improve_ticket_draft"),
			draft: ticketStatementOfWorkSchema,
			instructions: z.string().trim().min(1).optional(),
		})
		.strict(),
	z
		.object({
			...requestBase,
			operation: z.literal("start_ticket_execution"),
			ticketId: ticketIdSchema,
		})
		.strict(),
	z
		.object({
			...requestBase,
			operation: z.literal("cancel_ticket_execution"),
			ticketId: ticketIdSchema,
		})
		.strict(),
	z.object({ ...requestBase, operation: z.literal("accept_ticket"), ticketId: ticketIdSchema }).strict(),
	z
		.object({
			...requestBase,
			operation: z.literal("reject_ticket"),
			ticketId: ticketIdSchema,
			comment: z.string().trim().min(1),
		})
		.strict(),
	z.object({ ...requestBase, operation: z.literal("archive_ticket"), ticketId: ticketIdSchema }).strict(),
	z.object({ ...requestBase, operation: z.literal("restore_ticket"), ticketId: ticketIdSchema }).strict(),
	z
		.object({
			...requestBase,
			operation: z.literal("delete_ticket"),
			ticketId: ticketIdSchema,
			confirmed: z.literal(true),
		})
		.strict(),
	z
		.object({
			...requestBase,
			operation: z.literal("update_board_settings"),
			settings: agileCodeRepositorySettingsSchema,
		})
		.strict(),
])

export type BoardRequest = z.infer<typeof boardRequestSchema>

export const boardErrorCodeSchema = z.enum([
	"board_not_found",
	"ticket_not_found",
	"invalid_request",
	"invalid_transition",
	"conflict",
	"execution_failed",
	"persistence_failed",
	"unavailable",
	"internal_error",
])

export const boardErrorSchema = z
	.object({
		operation: boardOperationSchema,
		code: boardErrorCodeSchema,
		message: z.string().trim().min(1),
		retryable: z.boolean(),
	})
	.strict()

export type BoardError = z.infer<typeof boardErrorSchema>

const boardSnapshotSchema = z
	.object({
		scope: boardScopeSchema,
		board: agileCodeBoardSchema,
		settings: agileCodeRepositorySettingsSchema,
		activeTickets: z.array(ticketSchema),
		archivedTickets: z.array(ticketSchema),
		diagnostics: z.array(
			z
				.object({
					record: z.string(),
					problem: z.string().trim().min(1),
					kind: z.enum(["malformed", "unsupported-version", "reconciled"]),
				})
				.strict(),
		),
	})
	.strict()

export type BoardSnapshot = z.infer<typeof boardSnapshotSchema>

const resultBase = { requestId: requestIdSchema, boardId: boardScopeSchema.shape.id }
const success = <T extends BoardOperation, S extends z.ZodRawShape>(operation: T, shape: S) =>
	z.object({ ...resultBase, operation: z.literal(operation), ok: z.literal(true), ...shape }).strict()

const successfulBoardResultSchemas = [
	success("load_board", { snapshot: boardSnapshotSchema }),
	success("get_ticket", { ticket: ticketSchema }),
	success("create_ticket", { ticket: ticketSchema }),
	success("update_ticket", { ticket: ticketSchema }),
	success("move_ticket", { ticket: ticketSchema, board: agileCodeBoardSchema }),
	success("improve_ticket_draft", { draft: ticketStatementOfWorkSchema }),
	success("start_ticket_execution", { ticket: ticketSchema, historyItemId: z.string().trim().min(1) }),
	success("cancel_ticket_execution", { ticket: ticketSchema }),
	success("accept_ticket", { ticket: ticketSchema }),
	success("reject_ticket", { ticket: ticketSchema }),
	success("archive_ticket", { ticket: ticketSchema, board: agileCodeBoardSchema }),
	success("restore_ticket", { ticket: ticketSchema, board: agileCodeBoardSchema }),
	success("delete_ticket", { ticketId: ticketIdSchema, board: agileCodeBoardSchema }),
	success("update_board_settings", { settings: agileCodeRepositorySettingsSchema }),
] as const

const failedBoardResultSchema = z
	.object({ ...resultBase, operation: boardOperationSchema, ok: z.literal(false), error: boardErrorSchema })
	.strict()

/** Extension -> webview correlated outcomes. */
export const boardResultSchema = z
	.union([...successfulBoardResultSchemas, failedBoardResultSchema])
	.superRefine((result, context) => {
		if (!result.ok && result.operation !== result.error.operation) {
			context.addIssue({ code: z.ZodIssueCode.custom, message: "Result and error operations must match" })
		}
	})
export type BoardResult = z.infer<typeof boardResultSchema>

/** Extension -> webview push; every event carries both stable board identity and scope details. */
const boardStateBase = {
	type: z.literal("board_state_changed"),
	boardId: boardScopeSchema.shape.id,
	scope: boardScopeSchema,
	revision: z.number().int().nonnegative(),
}

export const boardStateEventSchema = z
	.discriminatedUnion("status", [
		z.object({ ...boardStateBase, status: z.literal("loading") }).strict(),
		z.object({ ...boardStateBase, status: z.literal("uninitialized") }).strict(),
		z.object({ ...boardStateBase, status: z.literal("ready"), snapshot: boardSnapshotSchema }).strict(),
		z
			.object({
				...boardStateBase,
				status: z.literal("error"),
				error: boardErrorSchema,
				diagnostics: z.array(z.string()),
			})
			.strict(),
	])
	.superRefine((event, context) => {
		if (
			event.boardId !== event.scope.id ||
			(event.status === "ready" && event.boardId !== event.snapshot.scope.id)
		) {
			context.addIssue({ code: z.ZodIssueCode.custom, message: "Board event identities must match" })
		}
	})

export type BoardStateEvent = z.infer<typeof boardStateEventSchema>

export const boardScopesEventSchema = z
	.object({
		type: z.literal("board_scopes_changed"),
		scopes: z.array(boardScopeSchema),
		selectedBoardId: boardScopeSchema.shape.id.optional(),
		unavailableBoardId: boardScopeSchema.shape.id.optional(),
	})
	.strict()
export type BoardScopesEvent = z.infer<typeof boardScopesEventSchema>

export type BoardWebviewMessage =
	| { type: "board_request"; request: BoardRequest }
	| { type: "select_board_scope"; scope: BoardScope }
export type BoardExtensionMessage = { type: "board_result"; result: BoardResult } | BoardStateEvent
