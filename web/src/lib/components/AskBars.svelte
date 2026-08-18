<script lang="ts">
	import { compact } from '$lib/format';

	interface Props {
		rows: { label: string; value: number }[];
		color?: string;
	}

	let { rows = [], color = 'var(--color-brand-salmon)' }: Props = $props();

	// Bars run from zero, so a shared top is the only honest scale.
	let top = $derived(Math.max(...rows.map((row) => Math.abs(row.value)), 0) || 1);

	function width(value: number): string {
		return `${Math.max((Math.abs(value) / top) * 100, 0.5)}%`;
	}
</script>

<ul class="bars">
	{#each rows as row, index (index)}
		<li class="bar">
			<span class="bar__label" title={row.label}>{row.label}</span>
			<span class="bar__track">
				<span class="bar__fill" style="width: {width(row.value)}; background: {color}"></span>
			</span>
			<span class="bar__value tabular">{compact(row.value)}</span>
		</li>
	{/each}
</ul>

<style>
	.bars {
		list-style: none;
		display: flex;
		flex-direction: column;
		gap: var(--space-xxs);
	}

	.bar {
		display: grid;
		grid-template-columns: minmax(6rem, 12rem) 1fr auto;
		align-items: center;
		gap: var(--space-s);
	}

	.bar__label {
		font-size: var(--font-size-s);
		color: var(--color-space-text-muted);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.bar__track {
		display: block;
		height: 1.1rem;
		border-radius: var(--radius-pill);
		background: var(--color-overlay-light-soft);
		overflow: hidden;
	}

	.bar__fill {
		display: block;
		height: 100%;
		border-radius: var(--radius-pill);
		opacity: 0.85;
	}

	.bar__value {
		font-size: var(--font-size-s);
		font-weight: 700;
		white-space: nowrap;
	}

	@media (max-width: 560px) {
		.bar {
			grid-template-columns: minmax(4.5rem, 8rem) 1fr auto;
			gap: var(--space-xs);
		}
	}
</style>
