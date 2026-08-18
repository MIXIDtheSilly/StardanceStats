<script lang="ts">
	import { untrack } from 'svelte';
	import { fade } from 'svelte/transition';
	import { enhance } from '$app/forms';
	import AskBars from '$components/AskBars.svelte';
	import Icon from '$components/Icon.svelte';
	import Perch from '$components/Perch.svelte';
	import { full } from '$lib/format';
	import type { AskAnswer, AskCell, AskColumn } from '$lib/types';
	import type { ActionData } from './$types';

	let { form }: { form: ActionData } = $props();

	const MAX_QUESTION = 400;

	let question = $state(untrack(() => form?.question ?? ''));
	let busy = $state(false);
	let box = $state<HTMLTextAreaElement | null>(null);

	let answer = $derived(form && 'answer' in form ? (form.answer as AskAnswer) : null);
	let failure = $derived(form && 'error' in form ? (form.error as string) : null);

	// Back to auto first, or the box can only ever get taller.
	$effect(() => {
		question;
		if (!box) return;
		box.style.height = 'auto';
		box.style.height = `${box.scrollHeight}px`;
	});

	/** Enter sends; a newline needs Shift, as in every chat box. */
	function keyed(event: KeyboardEvent) {
		if (event.key === 'Enter' && !event.shiftKey) {
			event.preventDefault();
			box?.form?.requestSubmit();
		}
	}

	function cell(row: Record<string, AskCell>, column: AskColumn): string {
		const value = row[column.key];
		if (value === null || value === undefined || value === '') return '--';
		if (typeof value === 'boolean') return value ? 'yes' : 'no';

		if (typeof value === 'number') {
			if (column.format === 'hours') return `${value.toFixed(1)}h`;
			if (column.format === 'seconds') return `${(value / 3600).toFixed(1)}h`;
			// An id is a name that happens to be a number, so it keeps no separators.
			if (column.key === '_id' || column.key.endsWith('_id')) return String(value);
			if (column.format === 'number') return full(value);
			return Number.isInteger(value) ? full(value) : String(value);
		}

		if (column.format === 'date') {
			const when = Date.parse(value);
			return Number.isNaN(when) ? value : new Date(when).toLocaleString('en', {
				dateStyle: 'medium',
				timeStyle: 'short'
			});
		}
		return value;
	}

	/** Only real figures line up on the right; a handle or a date reads as text. */
	function numeric(column: AskColumn): boolean {
		return column.format === 'number' || column.format === 'hours' || column.format === 'seconds';
	}

	function link(row: Record<string, AskCell>, column: AskColumn): string | null {
		const value = row[column.key];
		if (value === null || value === undefined || value === '') return null;
		if (column.format === 'username') return `/people/${encodeURIComponent(String(value))}`;
		if (column.format === 'project') return `/projects/${encodeURIComponent(String(value))}`;
		return null;
	}

	function bars(shown: AskAnswer): { label: string; value: number }[] {
		if (!shown.chart) return [];
		return shown.rows.map((row) => ({
			label: String(row[shown.chart!.label] ?? ''),
			value: Number(row[shown.chart!.value] ?? 0)
		}));
	}

	function figure(shown: AskAnswer): string {
		const value = shown.rows[0]?.[shown.columns[0].key];
		return typeof value === 'number' ? full(value) : String(value ?? '--');
	}
</script>

<svelte:head>
	<title>Ask | Stardance Stats</title>
</svelte:head>

<div class="page">
	<header class="head">
		<h1>Ask</h1>
	</header>

	<form class="ask" method="POST" use:enhance={() => {
		busy = true;
		return async ({ update }) => {
			await update({ reset: false });
			busy = false;
		};
	}}>
		<Perch art="star-creature-blue" at="top-right" w="3.6rem" x="18%" y="-52%" />

		<textarea
			bind:this={box}
			bind:value={question}
			name="question"
			rows="1"
			maxlength={MAX_QUESTION}
			placeholder="which projects earned the most stardust per hour?"
			aria-label="Your question"
			spellcheck="false"
			onkeydown={keyed}
		></textarea>

		<button class="ask__go" type="submit" disabled={busy || question.trim().length < 3}>
			{#if busy}
				<span class="spinner" aria-hidden="true"></span> Thinking
			{:else}
				<Icon name="search" size="0.9rem" /> Ask
			{/if}
		</button>
	</form>

	{#if busy}
		<p class="waiting muted" transition:fade={{ duration: 120 }}>
			Reading the database. This usually takes a few seconds.
		</p>
	{:else if failure}
		<p class="failed">{failure}</p>
	{:else if answer}
		{#key answer.question + answer.title}
			<section class="answer" in:fade={{ duration: 160 }}>
				<header class="answer__head">
					<h2>{answer.title}</h2>
					{#if answer.summary}
						<p class="muted">{answer.summary}</p>
					{/if}
				</header>

				{#if !answer.rows.length}
					<p class="empty muted">Nothing in the database matches that.</p>
				{:else if answer.display === 'number'}
					<p class="figure tabular">{figure(answer)}</p>
					<p class="figure__label muted">{answer.columns[0].label}</p>
				{:else if answer.display === 'bar'}
					<AskBars rows={bars(answer)} />
				{:else}
					<div class="scroller">
						<table>
							<thead>
								<tr>
									<th class="rownum"><span class="visually-hidden">Row</span></th>
									{#each answer.columns as column (column.key)}
										<th class:num={numeric(column)}>{column.label}</th>
									{/each}
								</tr>
							</thead>
							<tbody>
								{#each answer.rows as row, index (index)}
									<tr>
										<th class="rownum tabular" scope="row">{index + 1}</th>
										{#each answer.columns as column (column.key)}
											{@const href = link(row, column)}
											{#if href}
												<td class:num={numeric(column)}><a href={href}>{cell(row, column)}</a></td>
											{:else}
												<td class:num={numeric(column)}>{cell(row, column)}</td>
											{/if}
										{/each}
									</tr>
								{/each}
							</tbody>
						</table>
					</div>
				{/if}

				<footer class="answer__foot muted">
					{full(answer.row_count)}
					{answer.row_count === 1 ? 'row' : 'rows'} from {answer.collection}
					{#if answer.truncated}(capped){/if}
					· {(answer.elapsed_ms / 1000).toFixed(1)}s
					{#if answer.attempts > 1}· {answer.attempts} tries{/if}
				</footer>
			</section>
		{/key}
	{/if}
</div>

<style>
	.page {
		width: 100%;
		max-width: 64rem;
		display: flex;
		flex-direction: column;
		gap: var(--space-m);
	}

	.head {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: var(--space-m);
		flex-wrap: wrap;
		padding-bottom: var(--space-m);
		border-bottom: 1px solid var(--color-space-surface-faint);
	}

	.head h1 {
		font-size: var(--font-size-xxl);
	}

	.ask {
		position: relative;
		display: flex;
		align-items: center;
		gap: var(--space-s);
		padding: var(--space-s);
		border: 1px solid var(--color-space-surface-soft);
		border-radius: var(--radius);
		background: var(--color-space-bg);
	}

	.ask:focus-within {
		border-color: var(--color-brand-salmon);
	}

	textarea {
		flex: 1;
		min-width: 0;
		border: none;
		background: none;
		/* A textarea keeps its own font and colour however the page is set. */
		font-family: var(--font-text);
		font-size: var(--font-size-m);
		color: var(--color-space-text);
		line-height: 1.5;
		/* The script sets the height; the floor keeps one line clear of a scrollbar. */
		resize: none;
		min-height: 1.6rem;
		max-height: 12rem;
		overflow-y: auto;
	}

	textarea:focus {
		outline: none;
	}

	textarea::placeholder {
		color: var(--color-set-1-fg-secondary);
	}

	.ask__go {
		display: inline-flex;
		align-items: center;
		gap: var(--space-xs);
		padding: var(--space-xs) var(--space-m);
		border: none;
		border-radius: var(--radius-pill);
		background: var(--color-brand-salmon-soft);
		color: var(--color-brand-salmon);
		font-size: var(--font-size-s);
		font-weight: 700;
		white-space: nowrap;
		cursor: pointer;
	}

	.ask__go:hover:enabled {
		background: var(--color-brand-salmon);
		color: var(--color-space-bg);
	}

	.ask__go:disabled {
		opacity: 0.45;
		cursor: default;
	}

	.spinner {
		width: 0.8rem;
		height: 0.8rem;
		border: 2px solid currentColor;
		border-top-color: transparent;
		border-radius: 50%;
		animation: spin 0.7s linear infinite;
	}

	@keyframes spin {
		to {
			rotate: 360deg;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.spinner {
			animation: none;
		}
	}

	.waiting,
	.empty {
		padding: var(--space-l) 0;
		font-size: var(--font-size-s);
		line-height: 1.6;
	}

	.failed {
		padding: var(--space-m);
		border: 1px solid var(--color-brand-salmon);
		border-radius: var(--radius);
		background: var(--color-brand-salmon-soft);
		font-size: var(--font-size-s);
	}

	.answer {
		display: flex;
		flex-direction: column;
		gap: var(--space-m);
		padding: var(--space-l);
		border: 1px solid var(--color-space-surface-faint);
		border-radius: var(--radius);
	}

	.answer__head h2 {
		font-size: var(--font-size-xl);
	}

	.answer__head p {
		margin-top: var(--space-xxs);
		font-size: var(--font-size-s);
	}

	.figure {
		font-size: var(--font-size-xxxl);
		font-weight: 700;
		color: var(--color-brand-salmon);
		line-height: 1;
	}

	.figure__label {
		margin-top: calc(var(--space-m) * -1 + var(--space-xxs));
		font-size: var(--font-size-s);
	}

	.scroller {
		overflow-x: auto;
	}

	table {
		width: 100%;
		border-collapse: collapse;
		font-size: var(--font-size-s);
	}

	th,
	td {
		padding: var(--space-xs) var(--space-s);
		text-align: left;
		white-space: nowrap;
		border-bottom: 1px solid var(--color-space-surface-faint);
	}

	thead th {
		font-size: var(--font-size-xs);
		text-transform: uppercase;
		letter-spacing: 0.06em;
		color: var(--color-set-1-fg-secondary);
	}

	tbody tr:hover {
		background: var(--color-overlay-light-soft);
	}

	.rownum {
		width: 1rem;
		font-weight: 400;
		color: var(--color-set-1-fg-secondary);
	}

	.num {
		text-align: right;
		font-variant-numeric: tabular-nums;
	}

	td a {
		color: var(--color-brand-blue);
	}

	/* Without wrapping, one long comment sets the width of the table. */
	td:not(.num) {
		white-space: normal;
		max-width: 32rem;
	}

	.answer__foot {
		font-size: var(--font-size-xs);
	}

	@media (max-width: 560px) {
		.ask {
			flex-direction: column;
			align-items: stretch;
		}

		.answer {
			padding: var(--space-m);
		}
	}
</style>
