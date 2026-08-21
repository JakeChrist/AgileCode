/**
 * Standard skills bundled in memory with the extension. Keeping their content
 * here makes clean installations independent of user and workspace storage.
 */

import { SkillMetadata, SkillContent } from "../../shared/skills"

interface BuiltInSkillDefinition {
	name: string
	description: string
	instructions: string
	modeSlugs?: string[]
}

const BUILT_IN_SKILLS: Record<string, BuiltInSkillDefinition> = {
	"work-definition": {
		name: "work-definition",
		description:
			"Turn a broad objective or conversational decision set into a coherent work definition before decomposing it into executable tickets.",
		modeSlugs: ["architect", "scrum-master"],
		instructions: `Create a single coherent work definition from the user's objective and the relevant conversation. This is an analysis artifact, not a ticket-creation or implementation task.

First synthesize the body of work. Do not fragment it into tickets until its boundaries, outcomes, and major work areas are coherent. Do not create tickets, call board mutation tools, or otherwise persist or change board state merely because this skill was invoked. If the user later explicitly asks for decomposition or persistence, treat that as a separate action.

Use the following sections when applicable. State "Not identified" rather than inventing information that the available context does not establish:

1. **Motivating Problem or Opportunity** — Explain what prompts the work and why it matters.
2. **User Intent** — State the outcome the user is trying to achieve, including the affected users or stakeholders.
3. **Relevant Current State** — Summarize existing behavior, workflows, systems, and known limitations relevant to the work.
4. **Desired End State** — Describe the observable future condition and how it differs from the current state.
5. **Included Scope** — Define capabilities, flows, systems, and outcomes contained in this body of work.
6. **Excluded Scope** — Name meaningful boundaries and adjacent work that must not be silently absorbed.
7. **Constraints** — Record approved product, technical, operational, compliance, timing, or resource constraints.
8. **Preserved Behavior** — Identify behavior and compatibility guarantees that must remain unchanged.
9. **Major Work Areas** — Group the substantial capability or system areas involved without turning them into implementation tickets or prematurely prescribing task boundaries.
10. **Dependencies** — Record prerequisites, external inputs, related efforts, ordering needs, and why each matters.
11. **Risks** — Describe material delivery, product, technical, operational, security, or adoption risks and possible mitigations when known.
12. **Open Questions** — List unresolved matters that affect scope, readiness, design, or validation, and identify an owner or decision path when known.

Add a **Decision Record** that keeps conversational status explicit:

- **Decisions Made** — Only conclusions that the user or an authorized source actually confirmed. Include rationale when available.
- **Proposals** — Candidate approaches that remain unapproved; never present them as requirements.
- **Rejected Alternatives** — Options explicitly rejected, with the reason when known, so they are not accidentally reintroduced.
- **Unresolved Matters** — Decisions still needed, including competing options or missing evidence when known.

Reconcile contradictions visibly instead of choosing an interpretation silently. Keep requirements and scope traceable to confirmed intent and decisions. End with a brief **Coherence Check** covering whether the problem, end state, boundaries, preserved behavior, work areas, dependencies, risks, and decisions align; call out any gaps that prevent responsible decomposition.`,
	},
	"agile-ticket-creation": {
		name: "agile-ticket-creation",
		description:
			"Create durable, implementation-independent AgileCode tickets with explicit scope, requirements, dependencies, acceptance criteria, and validation expectations.",
		modeSlugs: ["scrum-master"],
		instructions: `Convert the supplied objective, work definition, or relevant conversation into one or more self-contained, executable ticket statements of work. Ticket creation defines authorized work; it does not authorize or begin execution. Do not write production code, tests, configuration, or other implementation artifacts merely because this skill was invoked.

## Determine the real outcome

Replace vague requests with the observable user or project outcome actually sought. Use confirmed context and decisions, not the request's wording alone. Distinguish confirmed facts from assumptions and open questions. Ask for or record material missing decisions rather than inventing product behavior, constraints, or technical choices.

## Decompose before drafting

Create the smallest cohesive set of tickets that delivers the outcome:

- Keep work together only when its parts must be delivered and validated together to provide value.
- Split unrelated concerns, independently valuable or independently deliverable outcomes, different owners, and work that can be accepted separately into separate tickets.
- Do not use one ticket as a container for a project, a sequence of loosely related changes, or opportunistic follow-up work.
- Identify ordering and relationships between the resulting tickets. A dependency is not a reason to combine otherwise independent work.
- When refinement reveals distinct, corrective, or newly discovered work, define it as a linked follow-up ticket when ticket tools are available instead of expanding the active ticket.

## Required ticket format

Use these sections for every ticket. Write "None identified" where the available context establishes that no item exists; use an explicit open question where information is unknown.

1. **Title** — A concise statement of the completed capability or outcome.
2. **Objective** — The actual desired user or project outcome and why it matters, not a restatement of vague input.
3. **Context** — The current state and confirmed background needed by a specialist who cannot see the originating conversation.
4. **Included Scope** — The behaviors, deliverables, interfaces, or outcomes this ticket owns.
5. **Preserved Behavior** — Existing behavior, compatibility, data, interfaces, and workflows that must remain unchanged.
6. **Constraints** — Confirmed product, technical, operational, security, compliance, timing, or customer-mandated implementation constraints. Do not promote a proposal into a constraint.
7. **Out of Scope** — Meaningful adjacent or easily confused work that this ticket deliberately excludes. Do not pad this section with arbitrary exclusions.
8. **Requirements** — Describe completed behavior and deliverables in precise, testable terms. State what must be true, not implementation tasks or a preferred design, unless the customer explicitly requires that implementation.
9. **Dependencies** — Prerequisite tickets, decisions, external inputs, blockers, and ordering constraints, including why each matters and whether it blocks readiness.
10. **Acceptance Criteria** — Objective, observable pass/fail conditions that collectively prove the requirements and outcome. Avoid subjective terms such as "better", "easy", or "properly" unless quantified.
11. **Validation** — State how completion will be demonstrated at the narrowest effective test layer. Cover the material success, boundary, failure, integration, and regression evidence applicable to the ticket; name expected evidence rather than merely saying "tests pass".
12. **Open Questions** — Only unresolved matters that affect scope, readiness, acceptance, or validation, with an owner or decision path when known.

## Quality gate

Before presenting or saving tickets, verify that each ticket is independently understandable, cohesive, deliverable, and verifiable; every requirement traces to the objective and is covered by acceptance criteria and validation; included scope, preserved behavior, constraints, dependencies, and out-of-scope boundaries do not conflict; no unrelated work is combined; and no implementation has begun. If a material open question prevents objective acceptance, mark the ticket as not ready rather than fabricating certainty.`,
	},
	"agile-ticket-implementation": {
		name: "agile-ticket-implementation",
		description:
			"Coordinate an approved ticket through disciplined engineering stages while the Kanban board remains authoritative for its lifecycle.",
		modeSlugs: ["orchestrator"],
		instructions: `Coordinate implementation of one approved board ticket. The ticket's approved scope, requirements, acceptance criteria, and validation are the work contract; do not silently expand or reinterpret them.

## Board authority

The Kanban board is the sole authority for ticket lifecycle state. Begin work only after the board-controlled start has authorized the ticket. Observe and honor board-controlled blocking, cancellation, completion, user Review, and acceptance decisions at every handoff. If the board reports that the ticket is blocked or cancelled, stop dispatching new stages and report the current evidence and unfinished work. Resume a blocked ticket only after the board authorizes resumption.

Do not move, rename, copy, or create ticket files among legacy lifecycle directories (including backlog, in-progress, blocked, review, or done directories). Do not use filesystem location as lifecycle state, and do not substitute file operations for board transitions. Specialists may report evidence and recommendations, but only the board workflow may start, block, cancel, move to user Review, complete, or accept the ticket.

Internal **Code Review** is an engineering-quality gate performed by Code Reviewer. It is not the board's **Review** state, which is a user review and acceptance phase. Finishing internal Code Review or creating a commit does not itself move the ticket to Review or Done and does not imply user acceptance.

## Required engineering sequence

Preserve context, decisions, artifacts, findings, and evidence across every handoff. Delegate each stage to its named mode and keep each specialist within that mode's responsibility:

1. **Requirements Engineer** — Inspect the ticket and relevant project behavior; make the required outcomes, constraints, preserved behavior, edge cases, failure behavior, assumptions, open questions, and testable acceptance criteria explicit. Escalate a material ambiguity instead of inventing a requirement.
2. **Architect** — Convert approved requirements into the necessary architecture and design decisions. Identify interfaces, boundaries, compatibility constraints, risks, and tradeoffs without implementing the solution. Return unresolved requirement questions to Requirements Engineer.
3. **Implementation Planner** — Turn the approved ticket, requirements, and architectural decisions into an ordered, repository-aware implementation and validation plan. Escalate requirement or architecture gaps to their owners rather than deciding them silently.
4. **Code** — Implement only the approved plan and ticket scope, including the lowest-layer automated tests needed for the behavior. Preserve unrelated behavior and report any discovery that changes requirements, architecture, or scope rather than absorbing it.
5. **Verification and Validation Engineer**, using **Production Test Design** — Independently trace every requirement and acceptance criterion to objective evidence. Design and run production-representative checks at the narrowest effective layer, covering material success, boundary, negative, failure, integration, and regression behavior. Report failures, evidence gaps, environmental limits, and residual risk; never weaken the criteria or tests to obtain a pass.
6. **Code Reviewer** — After V&V passes, independently review maintainability, clarity, complexity, responsibility boundaries, engineering quality, and fit with the surrounding codebase. Classify actionable findings and explicitly state whether substantive findings remain. This internal review does not perform or replace the board's user Review.
7. **Git Committer**, only when the workspace folder is a Git repository and this workflow uses Git — After V&V passes and substantive Code Review findings are cleared, stage and commit only the approved ticket changes. A successful commit is engineering-delivery evidence, not a board transition.

If the workspace folder is not a Git repository, skip Git Committer. Record that the commit stage was inapplicable and complete the engineering sequence without treating the absence of Git as a failure. Do not initialize a repository merely to satisfy this workflow.

## Corrective loops

- If V&V fails or finds an evidence gap requiring implementation or test changes, return the actionable findings to **Code**. After Code makes corrections, repeat the complete **V&V with Production Test Design** stage. Do not proceed to Code Reviewer on stale or failed evidence.
- If Code Reviewer reports any substantive finding, return it to **Code**. Then repeat **V&V with Production Test Design** for the corrected implementation before repeating **Code Reviewer**. Even when a review correction appears non-functional, do not reuse pre-change verification as final evidence.
- Route discoveries that alter requirements or acceptance criteria back to **Requirements Engineer**, architectural decisions back to **Architect**, and sequencing impacts back to **Implementation Planner**, then continue through Code and all downstream gates affected by the change.
- Optional, explicitly non-substantive review observations do not force a corrective loop. Record them separately so they are not mistaken for cleared substantive findings.

After the applicable engineering stages pass, report the outcome and evidence to the board workflow and wait for its authoritative next action. Never declare the ticket accepted or Done solely from internal stage results.`,
	},
	"production-test-design": {
		name: "production-test-design",
		description:
			"Design production-representative tests and evidence at the narrowest layer that can demonstrate a requirement without duplicating implementation behavior.",
		modeSlugs: ["code", "verification-validation-engineer"],
		instructions: `Design evidence from the required behavior outward, not from the easiest test to write or from the implementation inward.

## Trace the production path

1. State the requirement, intended use, material risk, and observable pass/fail outcome before choosing tools or a test layer.
2. Trace the actual production path responsible for that outcome: entry point, production logic, internal collaborators, persistence or transport, and user-visible or externally observable effect. Identify exactly which part of that path the evidence must exercise.
3. Choose the narrowest adequate boundary, following the repository's test-placement guidance:
   - Use a package-local unit test for production logic that can be exercised directly and whose outcome is observable without a runtime boundary.
   - Use an integration test when the evidence depends on contracts between multiple real internal modules or lightweight real infrastructure.
   - Use a webview UI test for React rendering, hooks, local component state, forms, validation, and UI wiring that does not require the real extension host.
   - Use end-to-end only when the requirement depends on the real extension host, workspace or browser APIs, cross-process messaging, file watching, or a complete workflow.
   - Use a justified mix when no single boundary provides both precise fault localization and confidence across a material production boundary.

## Preserve the behavior under test

Tests must invoke the real production behavior responsible for the requirement and verify its observable outputs, state changes, persisted data, messages, requests, rendered UI, or other meaningful effects wherever practical. An assertion that code merely executed, or one coupled only to private implementation details, is not sufficient evidence.

Mocks, monkeypatches, fake implementations, stubs, spies that replace behavior, and test-only substitutes must not replace the behavior being tested merely for convenience. Do not mock the production function, algorithm, state transition, request construction, serialization, persistence behavior, or module interaction whose correctness the test claims to prove. Do not duplicate that behavior in a test helper or expected-value oracle.

Substitution is reserved for a genuinely external boundary that cannot reasonably participate because it is unavailable, unsafe, destructive, prohibitively expensive, or nondeterministic. Prefer lightweight real infrastructure when reasonable. For every allowed substitute, name the external contract it represents, explain why the real boundary cannot participate, verify the production-side interaction at that boundary, and record what remains unverified. Fixtures may supply inputs and external responses; they must not implement the production decision being validated.

## Design the evidence

Cover material success, boundary, negative, failure, recovery, interface, integration, and regression behavior in proportion to risk. For a regression, place the reproducing test at the lowest layer that would have failed for the defect; add end-to-end coverage only when a lower layer cannot represent the failure mode.

For each proposed test, record: the requirement and risk; the traced production path; the chosen boundary and why narrower layers are inadequate; real components and infrastructure used; any external substitution and residual gap; stimulus; observable evidence; and the defect the assertion would detect. Confirm that the assertion would fail if the required production behavior were removed or broken.

Keep verification (conformance to specified requirements and design) distinct from validation (fitness for intended use). Neither is replaced by maintainability-focused Code Review. Do not weaken acceptance criteria or tests to get a pass. Report evidence, gaps, environmental limits, and residual risk explicitly. In non-Code modes, propose needed test or implementation changes without silently making them.`,
	},
	"create-mcp-server": {
		name: "create-mcp-server",
		description:
			"Instructions for creating MCP (Model Context Protocol) servers that expose tools and resources for the agent to use. Use when the user asks to create a new MCP server or add MCP capabilities.",
		instructions: `You have the ability to create an MCP server and add it to a configuration file that will then expose the tools and resources for you to use with \`use_mcp_tool\` and \`access_mcp_resource\`.

When creating MCP servers, it's important to understand that they operate in a non-interactive environment. The server cannot initiate OAuth flows, open browser windows, or prompt for user input during runtime. All credentials and authentication tokens must be provided upfront through environment variables in the MCP settings configuration. For example, Spotify's API uses OAuth to get a refresh token for the user, but the MCP server cannot initiate this flow. While you can walk the user through obtaining an application client ID and secret, you may have to create a separate one-time setup script (like get-refresh-token.js) that captures and logs the final piece of the puzzle: the user's refresh token (i.e. you might run the script using execute_command which would open a browser for authentication, and then log the refresh token so that you can see it in the command output for you to use in the MCP settings configuration).

Unless the user specifies otherwise, new local MCP servers should be created in your MCP servers directory. You can find the path to this directory by checking the MCP settings file, or ask the user where they'd like the server created.

### MCP Server Types and Configuration

MCP servers can be configured in two ways in the MCP settings file:

1. Local (Stdio) Server Configuration:

\`\`\`json
{
	"mcpServers": {
		"local-weather": {
			"command": "node",
			"args": ["/path/to/weather-server/build/index.js"],
			"env": {
				"OPENWEATHER_API_KEY": "your-api-key"
			}
		}
	}
}
\`\`\`

2. Remote (SSE) Server Configuration:

\`\`\`json
{
	"mcpServers": {
		"remote-weather": {
			"url": "https://api.example.com/mcp",
			"headers": {
				"Authorization": "Bearer your-api-key"
			}
		}
	}
}
\`\`\`

Common configuration options for both types:

- \`disabled\`: (optional) Set to true to temporarily disable the server
- \`timeout\`: (optional) Maximum time in seconds to wait for server responses (default: 60)
- \`alwaysAllow\`: (optional) Array of tool names that don't require user confirmation
- \`disabledTools\`: (optional) Array of tool names that are not included in the system prompt and won't be used

### Example Local MCP Server

For example, if the user wanted to give you the ability to retrieve weather information, you could create an MCP server that uses the OpenWeather API to get weather information, add it to the MCP settings configuration file, and then notice that you now have access to new tools and resources in the system prompt that you might use to show the user your new capabilities.

The following example demonstrates how to build a local MCP server that provides weather data functionality using the Stdio transport. While this example shows how to implement resources, resource templates, and tools, in practice you should prefer using tools since they are more flexible and can handle dynamic parameters. The resource and resource template implementations are included here mainly for demonstration purposes of the different MCP capabilities, but a real weather server would likely just expose tools for fetching weather data. (The following steps are for macOS)

1. Use the \`create-typescript-server\` tool to bootstrap a new project in your MCP servers directory:

\`\`\`bash
cd /path/to/your/mcp-servers
npx @modelcontextprotocol/create-server weather-server
cd weather-server
# Install dependencies
npm install axios zod @modelcontextprotocol/sdk
\`\`\`

This will create a new project with the following structure:

\`\`\`
weather-server/
	├── package.json
			{
				...
				"type": "module", // added by default, uses ES module syntax (import/export) rather than CommonJS (require/module.exports) (Important to know if you create additional scripts in this server repository like a get-refresh-token.js script)
				"scripts": {
					"build": "tsc && node -e \\"require('fs').chmodSync('build/index.js', '755')\\"",
					...
				}
				...
			}
	├── tsconfig.json
	└── src/
			└── index.ts      # Main server implementation
\`\`\`

2. Replace \`src/index.ts\` with the following:

\`\`\`typescript
#!/usr/bin/env node
import { McpServer, ResourceTemplate } from "@modelcontextprotocol/sdk/server/mcp.js"
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js"
import { z } from "zod"
import axios from "axios"

const API_KEY = process.env.OPENWEATHER_API_KEY // provided by MCP config
if (!API_KEY) {
	throw new Error("OPENWEATHER_API_KEY environment variable is required")
}

// Define types for OpenWeather API responses
interface WeatherData {
	main: {
		temp: number
		humidity: number
	}
	weather: Array<{
		description: string
	}>
	wind: {
		speed: number
	}
}

interface ForecastData {
	list: Array<
		WeatherData & {
			dt_txt: string
		}
	>
}

// Create an MCP server
const server = new McpServer({
	name: "weather-server",
	version: "0.1.0",
})

// Create axios instance for OpenWeather API
const weatherApi = axios.create({
	baseURL: "http://api.openweathermap.org/data/2.5",
	params: {
		appid: API_KEY,
		units: "metric",
	},
})

// Add a tool for getting weather forecasts
server.tool(
	"get_forecast",
	{
		city: z.string().describe("City name"),
		days: z.number().min(1).max(5).optional().describe("Number of days (1-5)"),
	},
	async ({ city, days = 3 }) => {
		try {
			const response = await weatherApi.get<ForecastData>("forecast", {
				params: {
					q: city,
					cnt: Math.min(days, 5) * 8,
				},
			})

			return {
				content: [
					{
						type: "text",
						text: JSON.stringify(response.data.list, null, 2),
					},
				],
			}
		} catch (error) {
			if (axios.isAxiosError(error)) {
				return {
					content: [
						{
							type: "text",
							text: \`Weather API error: \${error.response?.data.message ?? error.message}\`,
						},
					],
					isError: true,
				}
			}
			throw error
		}
	},
)

// Add a resource for current weather in San Francisco
server.resource("sf_weather", { uri: "weather://San Francisco/current", list: true }, async (uri) => {
	try {
		const response = weatherApi.get<WeatherData>("weather", {
			params: { q: "San Francisco" },
		})

		return {
			contents: [
				{
					uri: uri.href,
					mimeType: "application/json",
					text: JSON.stringify(
						{
							temperature: response.data.main.temp,
							conditions: response.data.weather[0].description,
							humidity: response.data.main.humidity,
							wind_speed: response.data.wind.speed,
							timestamp: new Date().toISOString(),
						},
						null,
						2,
					),
				},
			],
		}
	} catch (error) {
		if (axios.isAxiosError(error)) {
			throw new Error(\`Weather API error: \${error.response?.data.message ?? error.message}\`)
		}
		throw error
	}
})

// Add a dynamic resource template for current weather by city
server.resource(
	"current_weather",
	new ResourceTemplate("weather://{city}/current", { list: true }),
	async (uri, { city }) => {
		try {
			const response = await weatherApi.get("weather", {
				params: { q: city },
			})

			return {
				contents: [
					{
						uri: uri.href,
						mimeType: "application/json",
						text: JSON.stringify(
							{
								temperature: response.data.main.temp,
								conditions: response.data.weather[0].description,
								humidity: response.data.main.humidity,
								wind_speed: response.data.wind.speed,
								timestamp: new Date().toISOString(),
							},
							null,
							2,
						),
					},
				],
			}
		} catch (error) {
			if (axios.isAxiosError(error)) {
				throw new Error(\`Weather API error: \${error.response?.data.message ?? error.message}\`)
			}
			throw error
		}
	},
)

// Start receiving messages on stdin and sending messages on stdout
const transport = new StdioServerTransport()
await server.connect(transport)
console.error("Weather MCP server running on stdio")
\`\`\`

(Remember: This is just an example–you may use different dependencies, break the implementation up into multiple files, etc.)

3. Build and compile the executable JavaScript file

\`\`\`bash
npm run build
\`\`\`

4. Whenever you need an environment variable such as an API key to configure the MCP server, walk the user through the process of getting the key. For example, they may need to create an account and go to a developer dashboard to generate the key. Provide step-by-step instructions and URLs to make it easy for the user to retrieve the necessary information. Then use the ask_followup_question tool to ask the user for the key, in this case the OpenWeather API key.

5. Install the MCP Server by adding the MCP server configuration to the MCP settings file. On macOS/Linux this is typically at \`~/.roo-code/settings/mcp_settings.json\`, on Windows at \`%APPDATA%\\roo-code\\settings\\mcp_settings.json\`. The settings file may have other MCP servers already configured, so you would read it first and then add your new server to the existing \`mcpServers\` object.

IMPORTANT: Regardless of what else you see in the MCP settings file, you must default any new MCP servers you create to disabled=false, alwaysAllow=[] and disabledTools=[].

\`\`\`json
{
	"mcpServers": {
		...,
		"weather": {
			"command": "node",
			"args": ["/path/to/weather-server/build/index.js"],
			"env": {
				"OPENWEATHER_API_KEY": "user-provided-api-key"
			}
		},
	}
}
\`\`\`

(Note: the user may also ask you to install the MCP server to the Claude desktop app, in which case you would read then modify \`~/Library/Application\\ Support/Claude/claude_desktop_config.json\` on macOS for example. It follows the same format of a top level \`mcpServers\` object.)

6. After you have edited the MCP settings configuration file, the system will automatically run all the servers and expose the available tools and resources in the 'Connected MCP Servers' section.

7. Now that you have access to these new tools and resources, you may suggest ways the user can command you to invoke them - for example, with this new weather tool now available, you can invite the user to ask "what's the weather in San Francisco?"

## Editing MCP Servers

The user may ask to add tools or resources that may make sense to add to an existing MCP server (listed under 'Connected MCP Servers' in the system prompt), e.g. if it would use the same API. This would be possible if you can locate the MCP server repository on the user's system by looking at the server arguments for a filepath. You might then use list_files and read_file to explore the files in the repository, and use write_to_file or apply_diff to make changes to the files.

However some MCP servers may be running from installed packages rather than a local repository, in which case it may make more sense to create a new MCP server.

# MCP Servers Are Not Always Necessary

The user may not always request the use or creation of MCP servers. Instead, they might provide tasks that can be completed with existing tools. While using the MCP SDK to extend your capabilities can be useful, it's important to understand that this is just one specialized type of task you can accomplish. You should only implement MCP servers when the user explicitly requests it (e.g., "add a tool that...").

Remember: The MCP documentation and example provided above are to help you understand and work with existing MCP servers or create new ones when requested by the user. You already have access to tools and capabilities that can be used to accomplish a wide range of tasks.`,
	},
	"create-mode": {
		name: "create-mode",
		description:
			"Instructions for creating custom modes in Roo Code. Use when the user asks to create a new mode, edit an existing mode, or configure mode settings.",
		instructions: `Custom modes can be configured in two ways:

1. Globally via the custom modes file in your Roo Code settings directory (typically ~/.roo-code/settings/custom_modes.yaml on macOS/Linux or %APPDATA%\\roo-code\\settings\\custom_modes.yaml on Windows) - created automatically on startup
2. Per-workspace via '.roomodes' in the workspace root directory

When modes with the same slug exist in both files, the workspace-specific .roomodes version takes precedence. This allows projects to override global modes or define project-specific modes.

If asked to create a project mode, create it in .roomodes in the workspace root. If asked to create a global mode, use the global custom modes file.

- The following fields are required and must not be empty:

    - slug: A valid slug (lowercase letters, numbers, and hyphens). Must be unique, and shorter is better.
    - name: The display name for the mode
    - roleDefinition: A detailed description of the mode's role and capabilities
    - groups: Array of allowed tool groups (can be empty). Each group can be specified either as a string (e.g., "edit" to allow editing any file) or with file restrictions (e.g., ["edit", { fileRegex: "\\.md$", description: "Markdown files only" }] to only allow editing markdown files)

- The following fields are optional but highly recommended:

    - description: A short, human-readable description of what this mode does (5 words)
    - whenToUse: A clear description of when this mode should be selected and what types of tasks it's best suited for. This helps the Orchestrator mode make better decisions.
    - customInstructions: Additional instructions for how the mode should operate

- For multi-line text, include newline characters in the string like "This is the first line.\\nThis is the next line.\\n\\nThis is a double line break."

Both files should follow this structure (in YAML format):

customModes:

- slug: designer # Required: unique slug with lowercase letters, numbers, and hyphens
  name: Designer # Required: mode display name
  description: UI/UX design systems expert # Optional but recommended: short description (5 words)
  roleDefinition: >-
  You are Roo, a UI/UX expert specializing in design systems and frontend development. Your expertise includes:
    - Creating and maintaining design systems
    - Implementing responsive and accessible web interfaces
    - Working with CSS, HTML, and modern frontend frameworks
    - Ensuring consistent user experiences across platforms # Required: non-empty
      whenToUse: >-
      Use this mode when creating or modifying UI components, implementing design systems,
      or ensuring responsive web interfaces. This mode is especially effective with CSS,
      HTML, and modern frontend frameworks. # Optional but recommended
      groups: # Required: array of tool groups (can be empty)
    - read # Read files group (read_file, search_files, list_files, codebase_search)
    - edit # Edit files group (apply_diff, write_to_file) - allows editing any file
    # Or with file restrictions:
    # - - edit
    # - fileRegex: \\.md$
    # description: Markdown files only # Edit group that only allows editing markdown files
    - browser # Browser group (browser_action)
    - command # Command group (execute_command)
    - mcp # MCP group (use_mcp_tool, access_mcp_resource)
      customInstructions: Additional instructions for the Designer mode # Optional`,
	},
}

/**
 * Get all built-in skills as SkillMetadata objects
 */
export function getBuiltInSkills(): SkillMetadata[] {
	return Object.values(BUILT_IN_SKILLS).map((skill) => ({
		name: skill.name,
		description: skill.description,
		path: `<built-in:${skill.name}>`,
		source: "built-in" as const,
		modeSlugs: skill.modeSlugs,
	}))
}

/**
 * Get a specific built-in skill's full content by name
 */
export function getBuiltInSkillContent(name: string): SkillContent | null {
	const skill = BUILT_IN_SKILLS[name]
	if (!skill) return null

	return {
		name: skill.name,
		description: skill.description,
		path: `<built-in:${skill.name}>`,
		source: "built-in" as const,
		modeSlugs: skill.modeSlugs,
		instructions: skill.instructions,
	}
}

/**
 * Check if a skill name is a built-in skill
 */
export function isBuiltInSkill(name: string): boolean {
	return name in BUILT_IN_SKILLS
}

/**
 * Get names of all built-in skills
 */
export function getBuiltInSkillNames(): string[] {
	return Object.keys(BUILT_IN_SKILLS)
}
