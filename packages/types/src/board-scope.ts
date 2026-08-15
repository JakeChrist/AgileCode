import { z } from "zod"

/** A durable board boundary. Execution state intentionally lives outside this model. */
export const boardScopeSchema = z
	.object({
		id: z.string().regex(/^(git|workspace):[a-f0-9]{64}$/),
		kind: z.enum(["git", "workspace"]),
		rootPath: z.string().min(1),
	})
	.strict()

export type BoardScope = z.infer<typeof boardScopeSchema>
