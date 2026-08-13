<script lang="ts">
	interface Props {
		src?: string | null;
		name?: string | null;
		size?: string;
	}

	let { src = null, name = null, size = '2rem' }: Props = $props();

	// Remembering which URL failed lets the fallback reset by itself when the row is reused.
	let failedSrc = $state<string | null>(null);
	let broken = $derived(!src || failedSrc === src);
	let initial = $derived((name ?? '').trim().charAt(0).toUpperCase() || '?');
</script>

<span class="avatar" class:avatar--bare={broken} style="--size: {size}">
	{#if broken}
		<span aria-hidden="true">{initial}</span>
	{:else}
		<img
			src={src}
			alt=""
			loading="lazy"
			decoding="async"
			referrerpolicy="no-referrer"
			onerror={() => (failedSrc = src)}
		/>
	{/if}
</span>

<style>
	.avatar {
		display: grid;
		place-items: center;
		flex: 0 0 var(--size);
		width: var(--size);
		height: var(--size);
		border-radius: var(--radius-pill);
		overflow: hidden;
		background: var(--color-set-2-bg);
	}

	.avatar--bare {
		font-size: calc(var(--size) * 0.42);
		font-weight: 700;
		color: var(--color-space-text-muted);
		letter-spacing: 0;
	}

	img {
		width: 100%;
		height: 100%;
		object-fit: cover;
		display: block;
	}
</style>
