<script lang="ts">
	import { page } from '$app/state';
</script>

<svelte:head>
	<title>{page.status} | Stardance Stats</title>
</svelte:head>

<div class="container">
	<section class="card error">
		<span class="eyebrow">Error {page.status}</span>
		<h1>{page.error?.message ?? 'Something went wrong'}</h1>
		<a class="error__home" href="/">Back to the pulse</a>
	</section>
</div>

<style>
	.error {
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		gap: var(--space-s);
		margin-top: var(--space-xxl);
	}

	h1 {
		font-size: var(--font-size-xxl);
	}

	.error__home {
		padding: var(--space-xs) var(--space-m);
		border-radius: var(--radius-pill);
		background: var(--color-brand-highlight);
		color: var(--color-set-1-bg);
		font-weight: 700;
		font-size: var(--font-size-s);
	}

	.error__home:hover {
		color: var(--color-set-1-bg);
		opacity: 0.88;
	}
</style>
