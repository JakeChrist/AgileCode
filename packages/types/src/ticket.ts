import { z } from "zod"

/** The persisted ticket format understood by this version of AgileCode. */
export const TICKET_FORMAT_VERSION = 1 as const

export const ticketIdSchema = z
	.string()
	.regex(/^[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+$/, "Ticket identifier must use uppercase segments separated by hyphens")

export const ticketWorkflowStateSchema = z.enum([
	"backlog",
	"ready",
	"in_progress",
	"blocked",
	"review",
	"done",
	"archived",
])

export type TicketWorkflowState = z.infer<typeof ticketWorkflowStateSchema>

const nonEmptyText = z.string().trim().min(1)
const nonEmptyTextList = z.array(nonEmptyText).min(1)
const timestamp = z.string().datetime({ offset: true })

/** Customer-visible, editable statement-of-work content. */
export const ticketStatementOfWorkSchema = z
	.object({
		title: nonEmptyText,
		objective: nonEmptyText,
		context: nonEmptyText,
		requirements: nonEmptyTextList,
		deliverables: nonEmptyTextList.optional(),
		constraints: nonEmptyTextList,
		includedScope: nonEmptyTextList,
		excludedScope: nonEmptyTextList.optional(),
		dependencies: z.array(nonEmptyText),
		acceptanceCriteria: nonEmptyTextList,
		validation: nonEmptyTextList,
	})
	.strict()

export type TicketStatementOfWork = z.infer<typeof ticketStatementOfWorkSchema>

export const ticketReviewCommentSchema = z
	.object({
		id: nonEmptyText,
		comment: nonEmptyText,
		createdAt: timestamp,
		author: nonEmptyText.optional(),
	})
	.strict()

export const ticketBlockedReasonSchema = z
	.object({
		reason: nonEmptyText,
		createdAt: timestamp,
		resolvedAt: timestamp.optional(),
	})
	.strict()

/**
 * A failed attempt records only a useful summary and a reference to ordinary task
 * history. Task messages, transcripts, and terminal output deliberately do not
 * form part of the ticket contract.
 */
export const ticketFailedAttemptSchema = z
	.object({
		historyItemId: nonEmptyText,
		summary: nonEmptyText,
		failedAt: timestamp,
	})
	.strict()

export const ticketLifecycleSchema = z
	.object({
		state: ticketWorkflowStateSchema,
		createdAt: timestamp,
		completedAt: timestamp.optional(),
		acceptedAt: timestamp.optional(),
		archivedAt: timestamp.optional(),
		archivedFrom: ticketWorkflowStateSchema.exclude(["archived"]).optional(),
		reviewComments: z.array(ticketReviewCommentSchema).default([]),
		blockedReasons: z.array(ticketBlockedReasonSchema).default([]),
		failedAttempts: z.array(ticketFailedAttemptSchema).default([]),
	})
	.strict()
	.superRefine((lifecycle, context) => {
		if (lifecycle.state === "blocked" && !lifecycle.blockedReasons.some((reason) => !reason.resolvedAt)) {
			context.addIssue({
				code: z.ZodIssueCode.custom,
				path: ["blockedReasons"],
				message: "A blocked ticket must have an unresolved blocked reason",
			})
		}

		const archiveFieldsPresent = lifecycle.archivedAt !== undefined || lifecycle.archivedFrom !== undefined
		if (lifecycle.state === "archived" && (!lifecycle.archivedAt || !lifecycle.archivedFrom)) {
			context.addIssue({
				code: z.ZodIssueCode.custom,
				path: ["archivedAt"],
				message: "An archived ticket must record archivedAt and archivedFrom",
			})
		} else if (lifecycle.state !== "archived" && archiveFieldsPresent) {
			context.addIssue({
				code: z.ZodIssueCode.custom,
				path: ["archivedAt"],
				message: "Archive metadata is only valid for an archived ticket",
			})
		}
	})

export type TicketLifecycle = z.infer<typeof ticketLifecycleSchema>

export const ticketExecutionReferencesSchema = z
	.object({
		historyItemIds: z.array(nonEmptyText),
	})
	.strict()

export type TicketExecutionReferences = z.infer<typeof ticketExecutionReferencesSchema>

/** Durable ticket record, separated into editable SOW and system-managed metadata. */
export const ticketSchema = z
	.object({
		formatVersion: z.literal(TICKET_FORMAT_VERSION),
		id: ticketIdSchema,
		statementOfWork: ticketStatementOfWorkSchema,
		lifecycle: ticketLifecycleSchema,
		execution: ticketExecutionReferencesSchema,
	})
	.strict()

export type Ticket = z.infer<typeof ticketSchema>
