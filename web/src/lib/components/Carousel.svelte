<script lang="ts">
	import Icon from '$components/Icon.svelte';
	import type { DevlogMedia } from '$lib/types';

	interface Props {
		items: DevlogMedia[];
		label?: string;
		onbroken?: (url: string) => void;
	}

	let { items, label = 'Attachments', onbroken }: Props = $props();

	let track = $state<HTMLUListElement | null>(null);
	let at = $state(0);

	// Media drops out of the list when it fails to load, so the slide can outlive its cell.
	let index = $derived(Math.min(at, Math.max(0, items.length - 1)));

	function show(next: number) {
		if (!track || !items.length) return;
		const wrapped = (next + items.length) % items.length;
		// Wrapping flies past every slide in between, so only a neighbour is worth animating.
		const near = Math.abs(wrapped - index) === 1;
		const smooth = near && !matchMedia('(prefers-reduced-motion: reduce)').matches;
		track.scrollTo({ left: wrapped * track.clientWidth, behavior: smooth ? 'smooth' : 'auto' });
		at = wrapped;
	}

	function follow(event: Event & { currentTarget: HTMLElement }) {
		const width = event.currentTarget.clientWidth;
		if (width) at = Math.round(event.currentTarget.scrollLeft / width);
	}
</script>

<div class="reel" role="group" aria-label={label}>
	<ul class="reel__track" bind:this={track} onscroll={follow}>
		{#each items as item (item.url)}
			<li class="reel__cell">
				{#if item.kind === 'video'}
					<!-- svelte-ignore a11y_media_has_caption -->
					<video
						class="reel__media"
						src={item.url}
						poster={item.poster}
						preload="metadata"
						onerror={() => onbroken?.(item.url)}
					></video>
				{:else}
					<img
						class="reel__media"
						src={item.url}
						alt=""
						loading="lazy"
						decoding="async"
						referrerpolicy="no-referrer"
						onerror={() => onbroken?.(item.url)}
					/>
				{/if}
			</li>
		{/each}
	</ul>

	{#if items.length > 1}
		<button
			class="reel__step reel__step--back"
			type="button"
			onclick={() => show(index - 1)}
			aria-label="Previous attachment"
		>
			<Icon name="chevron-right" turn={2} size="1.1rem" />
		</button>
		<button
			class="reel__step reel__step--next"
			type="button"
			onclick={() => show(index + 1)}
			aria-label="Next attachment"
		>
			<Icon name="chevron-right" size="1.1rem" />
		</button>

		<ol class="reel__dots">
			{#each items as item, spot (item.url)}
				<li>
					<button
						class="reel__dot"
						class:reel__dot--on={spot === index}
						type="button"
						onclick={() => show(spot)}
						aria-label="Show attachment {spot + 1} of {items.length}"
						aria-current={spot === index ? 'true' : undefined}
					></button>
				</li>
			{/each}
		</ol>
	{/if}
</div>

<style>
	.reel {
		position: relative;
		min-height: 0;
		border: 1px solid var(--color-space-surface-faint);
		border-radius: var(--radius);
		background: var(--color-set-2-bg);
		overflow: hidden;
	}

	.reel__track {
		display: flex;
		height: 100%;
		list-style: none;
		overflow-x: auto;
		overflow-y: hidden;
		/* Snapping is what makes a touch swipe land on a slide rather than between two. */
		scroll-snap-type: x mandatory;
		scrollbar-width: none;
		overscroll-behavior-x: contain;
	}

	.reel__track::-webkit-scrollbar {
		display: none;
	}

	.reel__cell {
		flex: 0 0 100%;
		min-width: 0;
		height: 100%;
		scroll-snap-align: center;
	}

	.reel__media {
		display: block;
		width: 100%;
		height: 100%;
		object-fit: cover;
	}

	.reel__step {
		position: absolute;
		top: 0;
		bottom: 0;
		width: 28%;
		display: flex;
		align-items: center;
		padding: 0 var(--space-xs);
		border: none;
		color: var(--color-space-text);
		/* The shade thins out before the middle, so the chevron carries its own edge. */
		filter: drop-shadow(0 1px 2px var(--color-shadow-deep));
		opacity: 0;
		cursor: pointer;
		transition: opacity 0.18s ease;
	}

	.reel__step--back {
		left: 0;
		justify-content: flex-start;
		background: linear-gradient(to right, var(--color-shade-edge), transparent 65%);
	}

	.reel__step--next {
		right: 0;
		justify-content: flex-end;
		background: linear-gradient(to left, var(--color-shade-edge), transparent 65%);
	}

	/* Focus-visible, not focus-within: a click would hold the controls up after the pointer left. */
	.reel:hover .reel__step,
	.reel:has(:focus-visible) .reel__step,
	.reel:hover .reel__dots,
	.reel:has(:focus-visible) .reel__dots {
		opacity: 1;
	}

	.reel__step:focus-visible {
		outline-offset: -3px;
		opacity: 1;
	}

	.reel__dots {
		position: absolute;
		left: 50%;
		bottom: var(--space-xs);
		transform: translateX(-50%);
		display: flex;
		align-items: center;
		gap: var(--space-xxxs);
		padding: var(--space-xxxs) var(--space-xxs);
		border-radius: var(--radius-pill);
		background: var(--color-backdrop);
		list-style: none;
		opacity: 0;
		transition: opacity 0.18s ease;
	}

	.reel__dot {
		display: block;
		width: 6px;
		height: 6px;
		padding: 0;
		border: none;
		border-radius: var(--radius-pill);
		background: var(--color-space-text);
		opacity: 0.45;
		cursor: pointer;
		transition:
			opacity 0.18s ease,
			width 0.18s ease;
	}

	.reel__dot:hover {
		opacity: 0.75;
	}

	.reel__dot--on,
	.reel__dot--on:hover {
		width: 14px;
		opacity: 1;
	}

	@media (prefers-reduced-motion: reduce) {
		.reel__step,
		.reel__dots,
		.reel__dot {
			transition: none;
		}
	}
</style>
