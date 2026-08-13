<script lang="ts">
	import { fade } from 'svelte/transition';
	import Chart from '$components/Chart.svelte';
	import RangePicker from '$components/RangePicker.svelte';
	import ErrorState from '$components/ErrorState.svelte';
	import { compact, full, relative, signed } from '$lib/format';
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

	// The server fetches the widest window, so every range below is a slice of what we already hold.
	const RANGES = [1, 3, 7, 14, 30];

	let selected = $state('devlogs');
	let range = $state(7);

	let totals = $derived(data.totals?.totals ?? {});
	let series = $derived(data.history?.series ?? {});
	let active = $derived(METRICS.find((m) => m.key === selected) ?? METRICS[0]);
	let points = $derived(windowed(selected));

	// from/to describe the axis we asked for; only the points say what was observed.
	function observedPoints(key: string) {
		return (series[key] ?? []).filter((p) => Number.isFinite(p.v));
	}

	// Buckets are hourly, so the window is measured off the newest point rather than counted out.
	function windowed(key: string) {
		const observed = observedPoints(key);
		const last = observed.at(-1);
		if (!last) return [];
		const cutoff = Date.parse(last.ts) - range * 86_400_000;
		return observed.filter((p) => Date.parse(p.ts) >= cutoff);
	}

	function movement(key: string): { delta: number; days: number } | null {
		const window = windowed(key);
		if (window.length < 2) return null;
		const first = window[0];
		const last = window[window.length - 1];
		return {
			delta: last.v - first.v,
			days: Math.max(1, Math.round((Date.parse(last.ts) - Date.parse(first.ts)) / 86_400_000))
		};
	}

	// History rarely reaches back the full range yet, so say what was actually plotted.
	let span = $derived(movement(selected)?.days ?? 0);

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
		<div class="figures-wrap">
			<!-- Outside .figures so the grid's own clipping does not cut them in half. -->
			<img class="perch perch--users" src="/star-creature.png" alt="" aria-hidden="true" />
			<img class="perch perch--views" src="/star-creature-blue.png" alt="" aria-hidden="true" />
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
		</div>

		<section class="panel">
			<div class="panel__head">
				<h2 style="color: {active.color}">{active.label}</h2>
				<div class="panel__controls">
					{#if span && span < range}
						<span class="muted">only {span}d observed</span>
					{/if}
					<RangePicker options={RANGES} bind:value={range} label="Chart range" />
				</div>
			</div>
			{#key `${selected}-${range}`}
				<div in:fade={{ duration: 160 }}>
					<Chart {points} color={active.color} />
				</div>
			{/key}
		</section>

	{:else}
		<ErrorState code="API error" actionLabel="Refresh" />
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

	.figures-wrap {
		position: relative;
		margin: var(--space-l) 0;
	}

	.perch {
		position: absolute;
		height: auto;
		pointer-events: none;
	}

	.perch--users {
		left: 0;
		top: 0;
		width: 5rem;
		transform: translate(-25%, -53%);
	}

	/* This art fills its canvas, so it hangs off the bottom-right corner as drawn. */
	.perch--views {
		right: 0;
		bottom: 0;
		width: 4rem;
		transform: translate(-20%, 90%);
	}

	.figures {
		display: grid;
		grid-template-columns: repeat(4, 1fr);
		gap: 1px;
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
		text-align: left;
		cursor: pointer;
		transition: background 0.15s ease;
	}

	.figure:hover {
		background: var(--color-overlay-light-soft);
	}

	.figure--on {
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

	.panel__controls {
		display: flex;
		align-items: center;
		gap: var(--space-s);
	}

	.panel__head span {
		font-size: var(--font-size-xs);
	}


	@media (max-width: 720px) {
		.figures {
			grid-template-columns: repeat(2, 1fr);
		}
	}
</style>
