<script lang="ts">
	interface Props {
		options: number[];
		value: number;
		label?: string;
	}

	let { options, value = $bindable(), label = 'Range' }: Props = $props();

	let index = $derived(Math.max(0, options.indexOf(value)));
</script>

<div
	class="range"
	role="group"
	aria-label={label}
	style="--count: {options.length}; --index: {index}"
>
	<!-- The thumb keeps square corners; the track's overflow rounds it off at either end. -->
	<span class="range__thumb" aria-hidden="true"></span>
	{#each options as option (option)}
		<button
			type="button"
			class="range__opt"
			class:range__opt--on={option === value}
			aria-pressed={option === value}
			onclick={() => (value = option)}
		>
			{option}d
		</button>
	{/each}
</div>

<style>
	.range {
		position: relative;
		display: grid;
		grid-template-columns: repeat(var(--count), minmax(2.5rem, 1fr));
		background: var(--color-space-bg);
		border: 1px solid var(--color-space-surface-faint);
		border-radius: var(--radius);
		overflow: hidden;
	}

	.range__thumb {
		position: absolute;
		top: 0;
		bottom: 0;
		left: 0;
		width: calc(100% / var(--count));
		background: var(--color-space-text);
		transform: translateX(calc(var(--index) * 100%));
		transition: transform 0.28s cubic-bezier(0.22, 1, 0.36, 1);
	}

	.range__opt {
		position: relative;
		padding: var(--space-xxs) var(--space-s);
		background: none;
		border: none;
		font-size: var(--font-size-xs);
		font-weight: 700;
		letter-spacing: 0.04em;
		color: var(--color-space-text-muted);
		cursor: pointer;
		transition: color 0.2s ease;
	}

	.range__opt:hover {
		color: var(--color-space-text);
	}

	.range__opt--on,
	.range__opt--on:hover {
		color: var(--color-space-bg);
	}

	/* The track clips a normal focus ring, so draw it inside the button. */
	.range__opt:focus-visible {
		outline-offset: -3px;
	}

	@media (prefers-reduced-motion: reduce) {
		.range__thumb {
			transition: none;
		}
	}
</style>
