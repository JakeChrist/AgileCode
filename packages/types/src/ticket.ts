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
const textList = z.array(nonEmptyText)
const timestamp = z.string().datetime({ offset: true })

/** Customer-visible, editable statement-of-work content. */
export const ticketStatementOfWorkSchema = z
	.object({
		title: nonEmptyText,
		objective: z.string().trim().default(""),
		context: z.string().trim().default(""),
		requirements: textList.default([]),
		deliverables: textList.optional(),
		constraints: textList.default([]),
		includedScope: textList.default([]),
		excludedScope: textList.optional(),
		dependencies: textList.default([]),
		acceptanceCriteria: textList.default([]),
		validation: textList.default([]),
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

export const ticketExecutionPurposeSchema = z.enum(["initial", "resume", "review_correction"])
export const ticketExecutionOutcomeSchema = z.enum(["active", "completed", "failed", "cancelled"])
export type TicketExecutionPurpose = z.infer<typeof ticketExecutionPurposeSchema>
export type TicketExecutionOutcome = z.infer<typeof ticketExecutionOutcomeSchema>

/**
 * A repository-bound pointer to ordinary task history.  Transcript data stays in
 * the task store; this small record is safe to keep with the ticket when that
 * history is subsequently removed.
 */
export const ticketExecutionReferenceSchema = z
	.object({
		historyItemId: nonEmptyText,
		boardId: z.string().regex(/^(git|workspace):[a-f0-9]{64}$/),
		purpose: ticketExecutionPurposeSchema,
		startedAt: timestamp,
		outcome: ticketExecutionOutcomeSchema.optional(),
		finishedAt: timestamp.optional(),
	})
	.strict()
	.superRefine((reference, context) => {
		if (reference.finishedAt && (!reference.outcome || reference.outcome === "active")) {
			context.addIssue({
				code: z.ZodIssueCode.custom,
				path: ["finishedAt"],
				message: "A finished execution must have a terminal outcome",
			})
		}
	})

export type TicketExecutionReference = z.infer<typeof ticketExecutionReferenceSchema>

export const ticketExecutionReferencesSchema = z
	.object({
		/** @deprecated Retained while version-one ticket records are migrated. */
		historyItemIds: z.array(nonEmptyText),
		attempts: z.array(ticketExecutionReferenceSchema).optional(),
	})
	.strict()
	.superRefine((execution, context) => {
		const seen = new Set<string>()
		for (const [index, attempt] of (execution.attempts ?? []).entries()) {
			const key = `${attempt.historyItemId}:${attempt.startedAt}`
			if (seen.has(key)) {
				context.addIssue({
					code: z.ZodIssueCode.custom,
					path: ["attempts", index],
					message: "Duplicate execution reference",
				})
			}
			seen.add(key)
		}
	})

export type TicketExecutionReferences = z.infer<typeof ticketExecutionReferencesSchema>

/** Durable ticket record, separated into editable SOW and system-managed metadata. */
export const ticketSchema = z
	.object({
		formatVersion: z.literal(TICKET_FORMAT_VERSION),
		id: ticketIdSchema,
		statementOfWork: ticketStatementOfWorkSchema,
		lifecycle: ticketLifecycleSchema,
		execution: ticketExecutionReferencesSchema,
		originatingReview: z.object({ ticketId: ticketIdSchema, commentId: nonEmptyText }).strict().optional(),
	})
	.strict()

export type Ticket = z.infer<typeof ticketSchema>
