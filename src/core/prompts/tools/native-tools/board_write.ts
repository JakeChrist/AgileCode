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
			name: "create_ticket",
			description: "Create one durable Backlog ticket on an explicitly identified board.",
			parameters: {
				type: "object",
				additionalProperties: false,
				properties: { ...base, initial_state: { type: "string", enum: ["backlog", "ready"] } },
				required: ["board_id", "statement_of_work", "expected_revision"],
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
