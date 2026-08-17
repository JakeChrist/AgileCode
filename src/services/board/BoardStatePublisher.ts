import { RepositoryBoardService, type BoardServiceChange, type BoardServiceOptions } from "@roo-code/core"
import {
	boardStateEventSchema,
	type BoardError,
	type BoardExtensionMessage,
	type BoardScope,
	type BoardSnapshot,
	type BoardStateEvent,
} from "@roo-code/types"

type ServiceFactory = (scope: BoardScope, options: BoardServiceOptions) => Promise<RepositoryBoardService>

/** Owns the selected board service and only publishes snapshots for that selection. */
export class BoardStatePublisher {
	private service: RepositoryBoardService | undefined
	private selection = 0
	private revision = 0

	constructor(
		private readonly post: (message: BoardExtensionMessage) => unknown,
		private readonly createService: ServiceFactory = RepositoryBoardService.create,
	) {}

	async select(scope: BoardScope): Promise<void> {
		const selection = ++this.selection
		this.revision = 0
		this.service?.dispose()
		this.service = undefined
		await this.publish({ type: "board_state_changed", boardId: scope.id, scope, revision: 0, status: "loading" })

		try {
			const service = await this.createService(scope, {
				onDidChange: (change) => this.onChange(selection, scope, change),
				onDidError: (error) => this.onError(selection, scope, error),
			})
			if (selection !== this.selection) {
				service.dispose()
				return
			}
			this.service = service
			await this.publishReady(scope, service.state, service.recoveryDiagnostics)
		} catch (error) {
			await this.onError(selection, scope, error)
		}
	}

	dispose(): void {
		this.selection++
		this.service?.dispose()
		this.service = undefined
	}

	private async onChange(selection: number, scope: BoardScope, change: BoardServiceChange): Promise<void> {
		if (selection !== this.selection) return
		await this.publishReady(scope, change.state, change.diagnostics)
	}

	private async publishReady(
		scope: BoardScope,
		state: RepositoryBoardService["state"],
		diagnostics: RepositoryBoardService["recoveryDiagnostics"],
	): Promise<void> {
		const snapshot: BoardSnapshot = {
			scope,
			board: state.board,
			settings: state.settings,
			activeTickets: state.activeTickets,
			archivedTickets: state.archivedTickets,
			diagnostics,
		}
		await this.publish({
			type: "board_state_changed",
			boardId: scope.id,
			scope,
			revision: ++this.revision,
			status: "ready",
			snapshot,
		})
	}

	private async onError(selection: number, scope: BoardScope, cause: unknown): Promise<void> {
		if (selection !== this.selection) return
		const message = cause instanceof Error ? cause.message : String(cause)
		const error: BoardError = {
			operation: "load_board",
			code: "persistence_failed",
			message,
			retryable: true,
		}
		await this.publish({
			type: "board_state_changed",
			boardId: scope.id,
			scope,
			revision: ++this.revision,
			status: "error",
			error,
			diagnostics: [message],
		})
	}

	private async publish(message: BoardStateEvent): Promise<void> {
		await this.post(boardStateEventSchema.parse(message))
	}
}
