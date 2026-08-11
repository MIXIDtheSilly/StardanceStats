<script lang="ts">
	import Chart from '$components/Chart.svelte';
	import { compact, full, percent, relative, signed } from '$lib/format';
	import type { PageData } from './$types';

	let { data }: { data: PageData } = $props();

	const METRICS = [
		{ key: 'users', label: 'Users', color: 'var(--color-brand-mint)' },
		{ key: 'projects', label: 'Projects', color: 'var(--color-brand-blue)' },
		{ key: 'devlogs', label: 'Devlogs', color: 'var(--color-brand-lilac)' },
		{ key: 'ships', label: 'Ships', color: 'var(--color-brand-salmon)' },
		{ key: 'hours', label: 'Hours', color: 'var(--color-brand-peach)' },
		{ key: 'stardust_paid', label: 'Stardust', color: 'var(--color-brand-yellow)' },
		{ key: 'likes', label: 'Likes', color: 'var(--color-brand-salmon)' },
		{ key: 'views', label: 'Views', color: 'var(--color-brand-mint)' }
	];

	let selected = $state('devlogs');

	let totals = $derived(data.totals?.totals ?? {});
	let series = $derived(data.history?.series ?? {});
	let active = $derived(METRICS.find((m) => m.key === selected) ?? METRICS[0]);
	let points = $derived(series[selected] ?? []);

	// from/to describe the axis we asked for; only the points say what was observed.
	function movement(key: string): { delta: number; days: number } | null {
		const observed = (series[key] ?? []).filter((p) => Number.isFinite(p.v));
		if (observed.length < 2) return null;
		const first = observed[0];
		const last = observed[observed.length - 1];
		return {
			delta: last.v - first.v,
			days: Math.max(1, Math.round((Date.parse(last.ts) - Date.parse(first.ts)) / 86_400_000))
		};
	}

	let span = $derived(movement(selected)?.days ?? data.windowDays);

</script>

<svelte:head>
	<title>Stardance Stats</title>
</svelte:head>

<div class="page">
	<header class="head">
		<h1>Global</h1>
		<span class="muted">
			{#if data.totals}
				updated {relative(data.totals.data_as_of)}
			{:else}
				no data
			{/if}
		</span>
	</header>

	{#if data.totals}
		<section class="figures">
			{#each METRICS as metric (metric.key)}
				{@const moved = movement(metric.key)}
				<button
					class="figure"
					class:figure--on={selected === metric.key}
					style="--accent: {metric.color}"
					onclick={() => (selected = metric.key)}
				>
					<span class="figure__label">{metric.label}</span>
					<span class="figure__value tabular" title={full(totals[metric.key])}>
						{compact(totals[metric.key])}
					</span>
					<span class="figure__delta tabular" class:down={(moved?.delta ?? 0) < 0}>
						{!moved ? '' : moved.delta === 0 ? 'flat' : `${signed(moved.delta)} · ${moved.days}d`}
					</span>
				</button>
			{/each}
		</section>

		<section class="panel">
			<div class="panel__head">
				<h2 style="color: {active.color}">{active.label}</h2>
				<span class="muted">last {span} days, daily</span>
			</div>
			<Chart {points} color={active.color} />
		</section>

		{#if data.meta}
			{@const cov = data.meta.coverage}
			<section class="notes">
				<!-- tracked landed after the first deploy, so an older API omits it. -->
				{#if cov.projects_tracked}
					<span>
						{percent(cov.projects_crawled, cov.projects_tracked)} of
						{full(cov.projects_tracked)} projects crawled
					</span>
				{/if}
				{#if cov.users_tracked}
					<span>
						{percent(cov.users_crawled, cov.users_tracked)} of
						{full(cov.users_tracked)} users crawled
					</span>
				{/if}
				<span>{full(cov.users_complete)} profiles fully resolved</span>
			</section>
		{/if}
	{:else}
		<section class="cold">
			<p>No rollup written yet, so there is nothing to plot.</p>
			<pre><code>python -m src.collector.run</code></pre>
		</section>
	{/if}
</div>

<style>
	.page {
		width: 100%;
		max-width: 84rem;
	}

	.head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: var(--space-m);
		flex-wrap: wrap;
		padding-bottom: var(--space-m);
		border-bottom: 1px solid var(--color-space-surface-faint);
	}

	.head h1 {
		font-size: var(--font-size-xxl);
		font-weight: 700;
	}

	.head span {
		font-size: var(--font-size-s);
	}

	.figures {
		display: grid;
		grid-template-columns: repeat(4, 1fr);
		gap: 1px;
		margin: var(--space-l) 0;
		background: var(--color-space-surface-faint);
		border: 1px solid var(--color-space-surface-faint);
		border-radius: var(--radius);
		overflow: hidden;
	}

	.figure {
		display: flex;
		flex-direction: column;
		gap: 2px;
		padding: var(--space-m);
		background: var(--color-space-bg);
		border: none;
		border-top: 2px solid transparent;
		text-align: left;
		cursor: pointer;
		transition: background 0.15s ease;
	}

	.figure:hover {
		background: var(--color-overlay-light-soft);
	}

	.figure--on {
		border-top-color: var(--accent);
		background: var(--color-overlay-light-soft);
	}

	.figure__label {
		font-size: var(--font-size-xs);
		text-transform: uppercase;
		letter-spacing: 0.08em;
		color: var(--color-set-1-fg-secondary);
	}

	.figure__value {
		font-size: var(--font-size-xxl);
		font-weight: 700;
		line-height: 1.15;
	}

	.figure--on .figure__value {
		color: var(--accent);
	}

	.figure__delta {
		font-size: var(--font-size-xs);
		color: var(--color-brand-mint);
		min-height: 1em;
	}

	.figure__delta.down {
		color: var(--color-brand-salmon);
	}

	.panel {
		padding: var(--space-l);
		border: 1px solid var(--color-space-surface-faint);
		border-radius: var(--radius);
	}

	.panel__head {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: var(--space-m);
		margin-bottom: var(--space-m);
	}

	.panel__head h2 {
		font-size: var(--font-size-l);
	}

	.panel__head span {
		font-size: var(--font-size-xs);
	}

	.notes {
		display: flex;
		flex-wrap: wrap;
		gap: var(--space-m);
		margin-top: var(--space-l);
		font-size: var(--font-size-xs);
		color: var(--color-set-1-fg-secondary);
	}

	.cold {
		margin-top: var(--space-xl);
		display: flex;
		flex-direction: column;
		gap: var(--space-s);
	}

	.cold pre {
		padding: var(--space-s) var(--space-m);
		border-radius: var(--radius);
		background: var(--color-set-1-bg);
		overflow-x: auto;
	}

	.cold code {
		font-family: var(--font-mono);
		font-size: var(--font-size-s);
		color: var(--color-brand-cream);
	}

	@media (max-width: 720px) {
		.figures {
			grid-template-columns: repeat(2, 1fr);
		}
	}
</style>
