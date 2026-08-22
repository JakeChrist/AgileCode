import { z } from "zod"

import { ticketStatementOfWorkSchema } from "./ticket.js"

export const ticketProposalIdSchema = z
	.string()
	.trim()
	.regex(/^[a-z][a-z0-9-]*$/, "Use a stable proposal id")

export const decomposedTicketSchema = z
	.object({
		proposalId: ticketProposalIdSchema,
		statementOfWork: ticketStatementOfWorkSchema,
		dependsOn: z.array(ticketProposalIdSchema).default([]),
		sourceItems: z.array(z.string().trim().min(1)).min(1),
	})
	.strict()

/** A reviewable, non-persistent decomposition of one authoritative work definition. */
export const ticketSetProposalSchema = z
	.object({
		sourceWorkDefinition: z.string().trim().min(1),
		tickets: z.array(decomposedTicketSchema).min(1),
		unassignedSourceItems: z.array(z.string().trim().min(1)),
	})
	.strict()
	.superRefine((proposal, context) => {
		const ids = new Set<string>()
		for (const [index, ticket] of proposal.tickets.entries()) {
			if (ids.has(ticket.proposalId))
				context.addIssue({
					code: z.ZodIssueCode.custom,
					path: ["tickets", index, "proposalId"],
					message: "Proposal ids must be unique",
				})
			ids.add(ticket.proposalId)
		}
		const visiting = new Set<string>()
		const visited = new Set<string>()
		const byId = new Map(proposal.tickets.map((ticket) => [ticket.proposalId, ticket]))
		const visit = (id: string): void => {
			if (visiting.has(id)) {
				context.addIssue({ code: z.ZodIssueCode.custom, message: `Proposal dependency cycle includes ${id}` })
				return
			}
			if (visited.has(id)) return
			visiting.add(id)
			for (const dependency of byId.get(id)?.dependsOn ?? []) {
				if (!byId.has(dependency))
					context.addIssue({
						code: z.ZodIssueCode.custom,
						message: `Unknown proposal dependency ${dependency}`,
					})
				else visit(dependency)
			}
			visiting.delete(id)
			visited.add(id)
		}
		for (const id of ids) visit(id)
	})

export type TicketSetProposal = z.infer<typeof ticketSetProposalSchema>
