import type OpenAI from "openai"

const list = { type: "array", items: { type: "string", minLength: 1 } }
const statement = {
	type: "object",
	additionalProperties: false,
	properties: {
		title: { type: "string", minLength: 1 },
		objective: { type: "string" },
		context: { type: "string" },
		requirements: list,
		deliverables: list,
		constraints: list,
		includedScope: list,
		excludedScope: list,
		dependencies: list,
		acceptanceCriteria: list,
		validation: list,
	},
	required: [
		"title",
		"objective",
		"context",
		"requirements",
		"constraints",
		"includedScope",
		"dependencies",
		"acceptanceCriteria",
		"validation",
	],
}

const base = {
	board_id: { type: "string", description: "Exact target board id returned by list_boards." },
	statement_of_work: statement,
	expected_revision: {
		type: "integer",
		minimum: 0,
		description: "Revision returned by the latest board inspection.",
	},
}

export const boardWriteTools: OpenAI.Chat.ChatCompletionTool[] = [
	{
		type: "function",
		function: {
			name: "move_ticket",
			description:
				"Move one explicitly identified non-executing ticket using the board's authoritative transition rules.",
			parameters: {
				type: "object",
				additionalProperties: false,
				properties: {
					board_id: base.board_id,
					ticket_id: { type: "string" },
					destination: {
						type: "string",
						enum: ["backlog", "ready", "in_progress", "blocked", "review", "done"],
					},
					position: { type: "integer", minimum: 0 },
					expected_revision: base.expected_revision,
				},
				required: ["board_id", "ticket_id", "destination", "position", "expected_revision"],
			},
		},
	},
	{
		type: "function",
		function: {
			name: "reorder_tickets",
			description:
				"Atomically replace a column order. Supply both the complete desired order and the complete order most recently inspected.",
			parameters: {
				type: "object",
				additionalProperties: false,
				properties: {
					board_id: base.board_id,
					state: { type: "string", enum: ["backlog", "ready", "in_progress", "blocked", "review", "done"] },
					ordered_ids: list,
					expected_order: list,
					expected_revision: base.expected_revision,
				},
				required: ["board_id", "state", "ordered_ids", "expected_order", "expected_revision"],
			},
		},
	},
	{
		type: "function",
		function: {
			name: "block_ticket",
			description:
				"Manually block a non-running ticket and record a human-readable reason. Running work is rejected until its runtime has safely paused.",
			parameters: {
				type: "object",
				additionalProperties: false,
				properties: {
					board_id: base.board_id,
					ticket_id: { type: "string" },
					reason: { type: "string", minLength: 1 },
					position: { type: "integer", minimum: 0 },
					expected_revision: base.expected_revision,
				},
				required: ["board_id", "ticket_id", "reason", "position", "expected_revision"],
			},
		},
	},
	{
		type: "function",
		function: {
			name: "decompose_work",
			description:
				"Validate and return a reviewable ticket-set proposal, or persist it only when the user explicitly approved immediate creation.",
			parameters: {
				type: "object",
				additionalProperties: false,
				properties: {
					board_id: base.board_id,
					proposal: {
						type: "object",
						additionalProperties: false,
						properties: {
							sourceWorkDefinition: { type: "string", minLength: 1 },
							tickets: {
								type: "array",
								minItems: 1,
								items: {
									type: "object",
									additionalProperties: false,
									properties: {
										proposalId: { type: "string" },
										statementOfWork: statement,
										dependsOn: list,
										sourceItems: list,
									},
									required: ["proposalId", "statementOfWork", "dependsOn", "sourceItems"],
								},
							},
							unassignedSourceItems: list,
						},
						required: ["sourceWorkDefinition", "tickets", "unassignedSourceItems"],
					},
					create_approved_set: {
						type: "boolean",
						description:
							"True only after explicit user approval or an explicit instruction to create immediately.",
					},
					expected_revision: base.expected_revision,
				},
				required: ["board_id", "proposal", "create_approved_set", "expected_revision"],
			},
		},
	},
	{
		type: "function",
		function: {
			name: "create_ticket",
			description: "Create one durable Backlog ticket on an explicitly identified board.",
			parameters: {
				type: "object",
				additionalProperties: false,
				properties: {
					...base,
					initial_state: { type: "string", enum: ["backlog", "ready"] },
					originating_review_ticket_id: { type: "string" },
					originating_review_comment_id: { type: "string" },
				},
				required: ["board_id", "statement_of_work", "expected_revision"],
			},
		},
	},
	{
		type: "function",
		function: {
			name: "record_review_feedback",
			description: "Record user review comments separately from the reviewed ticket's original requirements.",
			parameters: {
				type: "object",
				additionalProperties: false,
				properties: {
					board_id: base.board_id,
					ticket_id: { type: "string" },
					comment: { type: "string", minLength: 1 },
					author: { type: "string", minLength: 1 },
					expected_revision: base.expected_revision,
				},
				required: ["board_id", "ticket_id", "comment", "expected_revision"],
			},
		},
	},
	...(["archive_ticket", "restore_ticket"] as const).map((name) => ({
		type: "function" as const,
		function: {
			name,
			description:
				name === "archive_ticket"
					? "Remove a resolved ticket from the active board without destroying it. General remove requests must use this tool."
					: "Restore an archived ticket to its recorded pre-archive workflow state with identity and history intact.",
			parameters: {
				type: "object",
				additionalProperties: false,
				properties: {
					board_id: base.board_id,
					ticket_id: { type: "string" },
					expected_revision: base.expected_revision,
				},
				required: ["board_id", "ticket_id", "expected_revision"],
			},
		},
	})),
	{
		type: "function",
		function: {
			name: "delete_ticket",
			description:
				"Permanently destroy an archived ticket only after explicit, confirmed user intent. Never use for a general remove request.",
			parameters: {
				type: "object",
				additionalProperties: false,
				properties: {
					board_id: base.board_id,
					ticket_id: { type: "string" },
					confirmed: {
						type: "boolean",
						description: "True only when the user explicitly confirmed permanent deletion.",
					},
					expected_revision: base.expected_revision,
				},
				required: ["board_id", "ticket_id", "confirmed", "expected_revision"],
			},
		},
	},
	{
		type: "function",
		function: {
			name: "update_ticket",
			description:
				"Replace the editable statement of work of one eligible ticket while preserving its identity and lifecycle history.",
			parameters: {
				type: "object",
				additionalProperties: false,
				properties: { ...base, ticket_id: { type: "string" } },
				required: ["board_id", "ticket_id", "statement_of_work", "expected_revision"],
			},
		},
	},
]
