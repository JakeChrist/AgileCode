import { z } from "zod"

import { ticketStatementOfWorkSchema, type TicketStatementOfWork } from "./ticket.js"
import { validateTicketReadiness } from "./ticket-validation.js"

const nonEmptyText = z.string().trim().min(1)

/** Fields that can introduce work beyond the user's original request. */
export const ticketImprovementScopeFieldSchema = z.enum([
	"includedScope",
	"requirements",
	"deliverables",
	"acceptanceCriteria",
])

export const ticketImprovementEvidenceSchema = z
	.object({
		path: nonEmptyText,
		observation: nonEmptyText,
	})
	.strict()

/**
 * Traceability for one scope-defining draft item. `requestExcerpt` must be copied
 * from the original request; codebase evidence may clarify how to do the work,
 * but cannot independently authorize new work.
 */
export const ticketImprovementScopeTraceSchema = z
	.object({
		field: ticketImprovementScopeFieldSchema,
		value: nonEmptyText,
		requestExcerpt: nonEmptyText,
		evidenceIndexes: z.array(z.number().int().nonnegative()).default([]),
	})
	.strict()

/** AI-authored content only. Ticket identity, lifecycle, and execution state are deliberately excluded. */
export const proposedTicketImprovementSchema = z
	.object({
		proposal: ticketStatementOfWorkSchema.required(),
		unresolvedQuestions: z.array(nonEmptyText),
		assumptions: z.array(nonEmptyText),
		codebaseEvidence: z.array(ticketImprovementEvidenceSchema),
		scopeTraceability: z.array(ticketImprovementScopeTraceSchema),
	})
	.strict()

export type ProposedTicketImprovement = z.infer<typeof proposedTicketImprovementSchema>

export type TicketImprovementValidationCode =
	| "malformed_output"
	| "incomplete_draft"
	| "untraceable_scope"
	| "invalid_evidence_reference"

export interface TicketImprovementValidationIssue {
	code: TicketImprovementValidationCode
	path: string
	message: string
}

export type TicketImprovementValidationResult =
	| { valid: true; originalRequest: string; value: ProposedTicketImprovement }
	| { valid: false; originalRequest: string; issues: TicketImprovementValidationIssue[] }

const scopeFields = ["includedScope", "requirements", "deliverables", "acceptanceCriteria"] as const

/** Parse and validate an AI response while always retaining the untouched request for recovery. */
export function validateTicketImprovementOutput(
	output: unknown,
	originalRequest: string,
): TicketImprovementValidationResult {
	const parsed = proposedTicketImprovementSchema.safeParse(output)
	if (!parsed.success) {
		return {
			valid: false,
			originalRequest,
			issues: parsed.error.issues.map((issue) => ({
				code: "malformed_output",
				path: issue.path.join("."),
				message: issue.message,
			})),
		}
	}

	const value = parsed.data
	const issues: TicketImprovementValidationIssue[] = validateTicketReadiness(value.proposal).issues.map((issue) => ({
		code: "incomplete_draft",
		path: `proposal.${issue.field}`,
		message: issue.message,
	}))
	const traces = new Map(value.scopeTraceability.map((trace) => [`${trace.field}\0${trace.value}`, trace]))

	for (const field of scopeFields) {
		for (const item of value.proposal[field] ?? []) {
			const path = `proposal.${field}`
			const trace = traces.get(`${field}\0${item}`)
			if (!trace || !originalRequest.includes(trace.requestExcerpt)) {
				issues.push({
					code: "untraceable_scope",
					path,
					message: `Scope item is not supported by an exact excerpt from the original request: ${item}`,
				})
				continue
			}
			for (const evidenceIndex of trace.evidenceIndexes) {
				if (!value.codebaseEvidence[evidenceIndex]) {
					issues.push({
						code: "invalid_evidence_reference",
						path: `scopeTraceability.${field}.${item}`,
						message: `Codebase evidence index ${evidenceIndex} does not exist`,
					})
				}
			}
		}
	}

	return issues.length > 0 ? { valid: false, originalRequest, issues } : { valid: true, originalRequest, value }
}

/** Creates an independently editable manual-form value without improvement metadata. */
export function ticketDraftFromImprovement(result: ProposedTicketImprovement): TicketStatementOfWork {
	return structuredClone(result.proposal)
}
