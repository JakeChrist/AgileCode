import type OpenAI from "openai"

const object = (properties: Record<string, unknown>, required: string[] = []): Record<string, unknown> => ({
	type: "object",
	properties,
	required,
	additionalProperties: false,
})

export const boardReadTools: OpenAI.Chat.ChatCompletionTool[] = [
	{
		type: "function",
		function: {
			name: "list_boards",
			description: "List repository board scopes available in the current VS Code workspace. This is read-only.",
			parameters: object({}),
		},
	},
	{
		type: "function",
		function: {
			name: "inspect_board",
			description: "Read ordered workflow states and ticket summaries from one explicitly identified board.",
			parameters: object(
				{
					board_id: { type: "string", description: "Exact board scope id returned by list_boards." },
					include_archived: {
						type: "boolean",
						description: "Include archived summaries; defaults to false.",
					},
					states: {
						type: "array",
						items: {
							type: "string",
							enum: ["backlog", "ready", "in_progress", "blocked", "review", "done", "archived"],
						},
						description: "Optional workflow-state filter.",
					},
				},
				["board_id"],
			),
		},
	},
	{
		type: "function",
		function: {
			name: "inspect_ticket",
			description: "Read a ticket's complete stored statement of work and lifecycle metadata from one board.",
			parameters: object(
				{
					board_id: { type: "string", description: "Exact board scope id returned by list_boards." },
					ticket_id: { type: "string" },
					include_archived: {
						type: "boolean",
						description: "Allow lookup in the archive; defaults to false.",
					},
				},
				["board_id", "ticket_id"],
			),
		},
	},
]
