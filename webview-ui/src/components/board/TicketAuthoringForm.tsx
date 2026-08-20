import { useEffect, useId, useMemo, useState } from "react"

import { ticketStatementOfWorkSchema, type TicketStatementOfWork } from "@roo-code/types"

export type TicketAuthoringValues = Partial<TicketStatementOfWork>

interface TicketAuthoringFormProps {
	initialValues?: TicketAuthoringValues
	onChange: (values: TicketAuthoringValues) => void
	onCancel: () => void
	onSubmit: (values: TicketStatementOfWork) => void
}

const textFields = [
	["title", "Title"],
	["objective", "Objective"],
	["context", "Context"],
] as const

const listFields = [
	["requirements", "Requirements", true],
	["deliverables", "Deliverables", false],
	["constraints", "Constraints", true],
	["includedScope", "Included scope", true],
	["excludedScope", "Excluded scope", false],
	["dependencies", "Dependencies", false],
	["acceptanceCriteria", "Acceptance criteria", true],
	["validation", "Validation expectations", true],
] as const

const startingValues = (values?: TicketAuthoringValues): TicketAuthoringValues => ({
	title: "",
	objective: "",
	context: "",
	...Object.fromEntries(listFields.map(([name]) => [name, values?.[name]?.length ? values[name] : [""]])),
	...values,
})

export default function TicketAuthoringForm({ initialValues, onChange, onCancel, onSubmit }: TicketAuthoringFormProps) {
	const prefix = useId()
	const initial = useMemo(() => startingValues(initialValues), [initialValues])
	const [values, setValues] = useState<TicketAuthoringValues>(initial)
	const [errors, setErrors] = useState<Record<string, string>>({})
	const dirty = JSON.stringify(values) !== JSON.stringify(initial)

	useEffect(() => {
		if (!dirty) return
		const protect = (event: BeforeUnloadEvent) => event.preventDefault()
		window.addEventListener("beforeunload", protect)
		return () => window.removeEventListener("beforeunload", protect)
	}, [dirty])

	const update = (next: TicketAuthoringValues) => {
		setValues(next)
		onChange(next)
	}
	const cancel = () => {
		if (dirty && !window.confirm("Discard this unsaved ticket draft?")) return
		onCancel()
	}
	const submit = () => {
		const normalized = Object.fromEntries(
			Object.entries(values).map(([key, value]) => [
				key,
				Array.isArray(value)
					? (() => {
							const items = value.map((item) => item.trim()).filter(Boolean)
							return (key === "deliverables" || key === "excludedScope") && items.length === 0
								? undefined
								: items
						})()
					: value?.trim(),
			]),
		)
		const result = ticketStatementOfWorkSchema.safeParse(normalized)
		if (!result.success) {
			setErrors(Object.fromEntries(result.error.issues.map((issue) => [String(issue.path[0]), issue.message])))
			return
		}
		setErrors({})
		onSubmit(result.data)
	}

	return (
		<section
			aria-labelledby={`${prefix}-heading`}
			className="absolute inset-0 z-20 flex flex-col bg-vscode-editor-background">
			<header className="flex items-center justify-between border-b border-vscode-panel-border px-4 py-3">
				<div>
					<h2 id={`${prefix}-heading`} className="m-0 text-base font-semibold">
						Write ticket
					</h2>
					<p className="m-0 mt-1 text-xs text-vscode-descriptionForeground">
						Statement of work only. Identity, dates, workflow, execution, review, and archive metadata are
						managed by AgileCode.
					</p>
				</div>
				<button type="button" aria-label="Close ticket form" onClick={cancel}>
					Close
				</button>
			</header>
			<form
				className="min-h-0 flex-1 overflow-y-auto p-4"
				onSubmit={(event) => {
					event.preventDefault()
					submit()
				}}>
				<div className="mx-auto grid max-w-4xl grid-cols-1 gap-5 md:grid-cols-2">
					{textFields.map(([name, label]) => {
						const errorId = `${prefix}-${name}-error`
						const inputId = `${prefix}-${name}`
						return (
							<label key={name} className={name === "title" ? "md:col-span-2" : ""}>
								<span className="mb-1 block font-medium">{label}</span>
								{name === "title" ? (
									<input
										id={inputId}
										className="w-full rounded border border-vscode-input-border bg-vscode-input-background p-2 text-vscode-input-foreground"
										value={values[name] ?? ""}
										aria-invalid={!!errors[name]}
										aria-describedby={errors[name] ? errorId : undefined}
										onChange={(event) => update({ ...values, [name]: event.target.value })}
									/>
								) : (
									<textarea
										id={inputId}
										rows={5}
										className="w-full resize-y rounded border border-vscode-input-border bg-vscode-input-background p-2 text-vscode-input-foreground"
										value={values[name] ?? ""}
										aria-invalid={!!errors[name]}
										aria-describedby={errors[name] ? errorId : undefined}
										onChange={(event) => update({ ...values, [name]: event.target.value })}
									/>
								)}
								{errors[name] && (
									<span id={errorId} className="mt-1 block text-xs text-vscode-errorForeground">
										{errors[name]}
									</span>
								)}
							</label>
						)
					})}
					{listFields.map(([name, label, required]) => {
						const items = values[name] ?? [""]
						const errorId = `${prefix}-${name}-error`
						return (
							<fieldset key={name} className="min-w-0 rounded border border-vscode-panel-border p-3">
								<legend className="px-1 font-medium">
									{label}
									{required ? " *" : ""}
								</legend>
								<div className="flex flex-col gap-2">
									{items.map((item, index) => (
										<div key={index} className="flex items-start gap-1">
											<textarea
												rows={2}
												aria-label={`${label} ${index + 1}`}
												aria-invalid={!!errors[name]}
												aria-describedby={errors[name] ? errorId : undefined}
												className="min-w-0 flex-1 resize-y rounded border border-vscode-input-border bg-vscode-input-background p-2 text-vscode-input-foreground"
												value={item}
												onChange={(event) =>
													update({
														...values,
														[name]: items.map((current, i) =>
															i === index ? event.target.value : current,
														),
													})
												}
											/>
											<div className="flex shrink-0 flex-col">
												<button
													type="button"
													aria-label={`Move ${label} ${index + 1} up`}
													disabled={index === 0}
													onClick={() => {
														const next = [...items]
														;[next[index - 1], next[index]] = [next[index], next[index - 1]]
														update({ ...values, [name]: next })
													}}>
													↑
												</button>
												<button
													type="button"
													aria-label={`Move ${label} ${index + 1} down`}
													disabled={index === items.length - 1}
													onClick={() => {
														const next = [...items]
														;[next[index], next[index + 1]] = [next[index + 1], next[index]]
														update({ ...values, [name]: next })
													}}>
													↓
												</button>
												<button
													type="button"
													aria-label={`Remove ${label} ${index + 1}`}
													onClick={() =>
														update({
															...values,
															[name]:
																items.length === 1
																	? [""]
																	: items.filter((_, i) => i !== index),
														})
													}>
													×
												</button>
											</div>
										</div>
									))}
									<button
										type="button"
										className="self-start text-vscode-textLink-foreground"
										onClick={() => update({ ...values, [name]: [...items, ""] })}>
										Add {label.toLowerCase()}
									</button>
									{errors[name] && (
										<span id={errorId} className="text-xs text-vscode-errorForeground">
											{errors[name]}
										</span>
									)}
								</div>
							</fieldset>
						)
					})}
				</div>
				<footer className="mx-auto mt-6 flex max-w-4xl justify-end gap-2 border-t border-vscode-panel-border pt-4">
					<button type="button" onClick={cancel}>
						Cancel
					</button>
					<button
						type="submit"
						className="rounded bg-vscode-button-background px-3 py-2 text-vscode-button-foreground">
						Create ticket
					</button>
				</footer>
			</form>
		</section>
	)
}
