<script lang="ts">
	import '$lib/styles/app.css';
	import Sidebar from '$components/Sidebar.svelte';
	import { page } from '$app/state';
	import type { LayoutData } from './$types';

	let { children, data }: { children: import('svelte').Snippet; data: LayoutData } = $props();

	const DESCRIPTION = 'A better way to browse Stardance projects!';

	// Crawlers need an absolute URL, and origin keeps it right in dev as well as prod.
	const banner = $derived(new URL('/SDS_banner_compressed.png', page.url.origin).href);
</script>

<svelte:head>
	<meta name="description" content={DESCRIPTION} />

	<meta property="og:type" content="website" />
	<meta property="og:site_name" content="Stardance Stats" />
	<meta property="og:title" content="Stardance Stats" />
	<meta property="og:description" content={DESCRIPTION} />
	<meta property="og:url" content={page.url.href} />
	<meta property="og:image" content={banner} />
	<meta property="og:image:width" content="956" />
	<meta property="og:image:height" content="601" />

	<meta name="twitter:card" content="summary_large_image" />
	<meta name="twitter:title" content="Stardance Stats" />
	<meta name="twitter:description" content={DESCRIPTION} />
	<meta name="twitter:image" content={banner} />
</svelte:head>

<div class="shell">
	<Sidebar lastCrawl={data.lastCrawl} />
	<main>
		{@render children()}
	</main>
</div>

<style>
	.shell {
		--side-width: 14rem;
		display: flex;
		align-items: stretch;
		min-height: 100vh;
	}

	main {
		flex: 1;
		min-width: 0;
		display: flex;
		justify-content: center;
		padding: var(--space-xl) var(--space-xl) var(--space-xxxl);
	}

	@media (max-width: 1100px) {
		main {
			padding: var(--space-l) var(--space-l) var(--space-xxl);
		}
	}

	@media (max-width: 860px) {
		.shell {
			flex-direction: column;
		}

		main {
			padding: var(--space-l) var(--space-m) var(--space-xxl);
		}
	}
</style>
