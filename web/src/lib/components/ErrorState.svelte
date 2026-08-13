<script lang="ts">
	interface Props {
		/** The large centred line: a status code, or a short phrase. */
		code: string;
		detail?: string;
		actionLabel: string;
		/** Given, the action is a link; without it the action reloads the page. */
		href?: string;
	}

	let { code, detail, actionLabel, href }: Props = $props();
</script>

<section class="state">
	<p class="state__code">{code}</p>
	{#if detail}
		<p class="state__detail muted">{detail}</p>
	{/if}
	{#if href}
		<a class="state__action" {href}>{actionLabel}</a>
	{:else}
		<button class="state__action" type="button" onclick={() => location.reload()}>
			{actionLabel}
		</button>
	{/if}
</section>

<style>
	.state {
		flex: 1;
		min-height: 60vh;
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		gap: var(--space-m);
		text-align: center;
	}

	.state__code {
		font-family: var(--font-display);
		/* One rule for "404" and for a phrase, so neither overflows a phone. */
		font-size: clamp(2.5rem, 9vw, 4.5rem);
		line-height: 1;
		font-weight: 700;
	}

	.state__detail {
		max-width: 32rem;
		font-size: var(--font-size-s);
	}

	.state__action {
		padding: var(--space-xs) var(--space-l);
		border: 0;
		border-radius: var(--radius-pill);
		background: var(--color-brand-highlight);
		color: var(--color-set-1-bg);
		font-weight: 700;
		font-size: var(--font-size-s);
		cursor: pointer;
	}

	.state__action:hover {
		color: var(--color-set-1-bg);
		opacity: 0.88;
	}
</style>
