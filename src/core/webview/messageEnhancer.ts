import {
	ProviderSettings,
	ClineMessage,
	GlobalState,
	TelemetryEventName,
	validateTicketImprovementOutput,
	type ProposedTicketImprovement,
	type TicketImprovementValidationIssue,
} from "@roo-code/types"
import { TelemetryService } from "@roo-code/telemetry"
import { supportPrompt } from "../../shared/support-prompt"
import { singleCompletionHandler } from "../../utils/single-completion-handler"
import { ProviderSettingsManager } from "../config/ProviderSettingsManager"
import { ClineProvider } from "./ClineProvider"

export interface MessageEnhancerOptions {
	text: string
	apiConfiguration: ProviderSettings
	customSupportPrompts?: Record<string, any>
	listApiConfigMeta: Array<{ id: string; name?: string }>
	enhancementApiConfigId?: string
	includeTaskHistoryInEnhance?: boolean
	currentClineMessages?: ClineMessage[]
	providerSettingsManager: ProviderSettingsManager
}

export interface MessageEnhancerResult {
	success: boolean
	enhancedText?: string
	error?: string
}

export interface TicketImprovementRepositoryIdentity {
	id: string
	name?: string
}

export interface TicketImprovementOptions {
	roughRequest: string
	repository: TicketImprovementRepositoryIdentity
	apiConfiguration: ProviderSettings
	listApiConfigMeta: Array<{ id: string; name?: string }>
	enhancementApiConfigId?: string
	providerSettingsManager: ProviderSettingsManager
}

export type TicketImprovementResult =
	| { success: true; originalRequest: string; draft: ProposedTicketImprovement }
	| {
			success: false
			originalRequest: string
			code: "provider_failure" | "parsing_failure" | "validation_failure"
			error: string
			issues?: TicketImprovementValidationIssue[]
	  }

const TICKET_IMPROVEMENT_INSTRUCTION = `You improve a rough request into a proposed ticket statement of work.
Return only one JSON object. Do not return markdown, implementation steps, code changes, or tool calls. Do not create or execute work.
The object must have exactly these fields:
{
  "proposal": {
    "title": "non-empty string",
    "objective": "non-empty string",
    "context": "string",
    "requirements": ["non-empty string"],
    "deliverables": ["non-empty string"],
    "constraints": ["non-empty string"],
    "includedScope": ["non-empty string"],
    "excludedScope": ["non-empty string"],
    "dependencies": ["non-empty string"],
    "acceptanceCriteria": ["non-empty string"],
    "validation": ["non-empty string"]
  },
  "unresolvedQuestions": ["non-empty string"],
  "assumptions": ["non-empty string"],
  "codebaseEvidence": [{ "path": "repository-relative path", "observation": "non-empty string" }],
  "scopeTraceability": [{
    "field": "includedScope | requirements | deliverables | acceptanceCriteria",
    "value": "an exact item from the corresponding proposal array",
    "requestExcerpt": "an exact, non-empty excerpt copied from the rough request",
    "evidenceIndexes": [0]
  }]
}
Every includedScope, requirements, deliverables, and acceptanceCriteria item must have a traceability entry. Repository evidence may clarify requested work but must not expand its scope. Use empty arrays when there is no content.`

/**
 * Enhances a message prompt using AI, optionally including task history for context
 */
export class MessageEnhancer {
	/** Produces a validated, non-persisted ticket proposal from a rough request. */
	static async improveTicket(options: TicketImprovementOptions): Promise<TicketImprovementResult> {
		const originalRequest = options.roughRequest
		let response: string

		try {
			const configToUse = await this.resolveEnhancementConfiguration(options)
			const prompt = `${TICKET_IMPROVEMENT_INSTRUCTION}\n\nRepository identity:\n${JSON.stringify(options.repository)}\n\nRough request:\n${originalRequest}`
			response = await singleCompletionHandler(configToUse, prompt)
		} catch (error) {
			return {
				success: false,
				originalRequest,
				code: "provider_failure",
				error: error instanceof Error ? error.message : String(error),
			}
		}

		let output: unknown
		try {
			output = JSON.parse(response)
		} catch (error) {
			return {
				success: false,
				originalRequest,
				code: "parsing_failure",
				error: error instanceof Error ? error.message : String(error),
			}
		}

		const validation = validateTicketImprovementOutput(output, originalRequest)
		if (!validation.valid) {
			return {
				success: false,
				originalRequest,
				code: "validation_failure",
				error: "The model response did not satisfy the Improve Ticket output contract.",
				issues: validation.issues,
			}
		}

		return { success: true, originalRequest, draft: validation.value }
	}

	private static async resolveEnhancementConfiguration(options: {
		apiConfiguration: ProviderSettings
		listApiConfigMeta: Array<{ id: string; name?: string }>
		enhancementApiConfigId?: string
		providerSettingsManager: ProviderSettingsManager
	}): Promise<ProviderSettings> {
		if (
			options.enhancementApiConfigId &&
			options.listApiConfigMeta.some(({ id }) => id === options.enhancementApiConfigId)
		) {
			const { name: _, ...profile } = await options.providerSettingsManager.getProfile({
				id: options.enhancementApiConfigId,
			})
			if (profile.apiProvider) return profile
		}
		return options.apiConfiguration
	}

	/**
	 * Enhances a message prompt using the configured AI provider
	 * @param options Configuration options for message enhancement
	 * @returns Enhanced message result with success status
	 */
	static async enhanceMessage(options: MessageEnhancerOptions): Promise<MessageEnhancerResult> {
		try {
			const {
				text,
				apiConfiguration,
				customSupportPrompts,
				listApiConfigMeta,
				enhancementApiConfigId,
				includeTaskHistoryInEnhance,
				currentClineMessages,
				providerSettingsManager,
			} = options

			// Determine which API configuration to use
			const configToUse = await this.resolveEnhancementConfiguration({
				apiConfiguration,
				listApiConfigMeta,
				enhancementApiConfigId,
				providerSettingsManager,
			})

			// Prepare the prompt to enhance
			let promptToEnhance = text

			// Include task history if enabled and available
			if (includeTaskHistoryInEnhance && currentClineMessages && currentClineMessages.length > 0) {
				const taskHistory = this.extractTaskHistory(currentClineMessages)
				if (taskHistory) {
					promptToEnhance = `${text}\n\nUse the following previous conversation context as needed:\n${taskHistory}`
				}
			}

			// Create the enhancement prompt using the support prompt system
			const enhancementPrompt = supportPrompt.create(
				"ENHANCE",
				{ userInput: promptToEnhance },
				customSupportPrompts,
			)

			// Call the single completion handler to get the enhanced prompt
			const enhancedText = await singleCompletionHandler(configToUse, enhancementPrompt)

			return {
				success: true,
				enhancedText,
			}
		} catch (error) {
			return {
				success: false,
				error: error instanceof Error ? error.message : String(error),
			}
		}
	}

	/**
	 * Extracts relevant task history from Cline messages for context
	 * @param messages Array of Cline messages
	 * @returns Formatted task history string
	 */
	private static extractTaskHistory(messages: ClineMessage[]): string {
		try {
			const relevantMessages = messages
				.filter((msg) => {
					// Include user messages (type: "ask" with text) and assistant messages (type: "say" with say: "text")
					if (msg.type === "ask" && msg.text) {
						return true
					}
					if (msg.type === "say" && msg.say === "text" && msg.text) {
						return true
					}
					return false
				})
				.slice(-10) // Limit to last 10 messages to avoid context explosion

			return relevantMessages
				.map((msg) => {
					const role = msg.type === "ask" ? "User" : "Assistant"
					const content = msg.text || ""
					// Truncate long messages
					return `${role}: ${content.slice(0, 500)}${content.length > 500 ? "..." : ""}`
				})
				.join("\n")
		} catch (error) {
			// Log error but don't fail the enhancement
			console.error("Failed to extract task history:", error)
			return ""
		}
	}

	/**
	 * Captures telemetry for prompt enhancement
	 * @param taskId Optional task ID for telemetry tracking
	 * @param includeTaskHistory Whether task history was included in the enhancement
	 */
	static captureTelemetry(taskId?: string, includeTaskHistory?: boolean): void {
		if (TelemetryService.hasInstance()) {
			// Use captureEvent directly to include the includeTaskHistory property
			TelemetryService.instance.captureEvent(TelemetryEventName.PROMPT_ENHANCED, {
				...(taskId && { taskId }),
				includeTaskHistory: includeTaskHistory ?? false,
			})
		}
	}
}
