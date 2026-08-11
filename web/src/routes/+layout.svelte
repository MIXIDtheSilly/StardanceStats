<script lang="ts">
	import '$lib/styles/app.css';
	import Sidebar from '$components/Sidebar.svelte';
	import type { LayoutData } from './$types';

	let { children, data }: { children: import('svelte').Snippet; data: LayoutData } = $props();
</script>

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
