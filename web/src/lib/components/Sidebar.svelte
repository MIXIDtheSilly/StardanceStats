<script lang="ts">
	import { page } from '$app/state';
	import { relative } from '$lib/format';

	interface Props {
		lastCrawl?: string | null;
	}

	let { lastCrawl = null }: Props = $props();

	const TABS = [
		{ label: 'Global', href: '/global', accent: 'var(--color-brand-mint)', ready: true },
		{ label: 'People', href: '/people', accent: 'var(--color-brand-lilac)', ready: true },
		{ label: 'Projects', href: '/projects', accent: 'var(--color-brand-blue)', ready: true },
		{ label: 'Devlogs', href: '/devlogs', accent: 'var(--color-brand-yellow)', ready: true },
		{ label: 'Ask', href: '/ask', accent: 'var(--color-brand-salmon)', ready: false }
	];

	let current = $derived(page.url.pathname);

	function within(href: string): boolean {
		return current === href || current.startsWith(`${href}/`);
	}
</script>

<aside class="side">
	<a class="side__brand" href="/global" aria-label="Stardance Stats">
		<img src="/banner.png" alt="Stardance Stats" width="1400" height="592" />
	</a>

	<nav class="tabs" aria-label="Sections">
		{#each TABS as tab (tab.href)}
			{#if tab.ready}
				<a
					class="tab"
					class:tab--on={within(tab.href)}
					href={tab.href}
					style="--accent: {tab.accent}"
					aria-current={within(tab.href) ? 'page' : undefined}
				>
					{tab.label}
				</a>
			{:else}
				<span class="tab tab--soon" style="--accent: {tab.accent}">
					{tab.label}
				</span>
			{/if}
		{/each}
	</nav>

	<footer class="side__foot">
		{#if lastCrawl}
			<span>last crawl {relative(lastCrawl)}</span>
		{/if}
	</footer>
</aside>

<style>
	.side {
		display: flex;
		flex-direction: column;
		gap: var(--space-l);
		flex: 0 0 var(--side-width);
		width: var(--side-width);
		padding: var(--space-l) var(--space-m);
		border-right: 1px solid var(--color-space-surface-faint);
		position: sticky;
		top: 0;
		align-self: flex-start;
		height: 100vh;
	}

	.side__brand {
		display: flex;
		justify-content: center;
		padding: var(--space-xs) 0 var(--space-s);
	}

	.side__brand img {
		display: block;
		width: 100%;
		max-width: 9.5rem;
		height: auto;
	}

	.tabs {
		display: flex;
		flex-direction: column;
		gap: var(--space-xxs);
	}

	.tab {
		display: flex;
		align-items: baseline;
		gap: var(--space-xs);
		padding: var(--space-xs) var(--space-s);
		border-radius: var(--radius);
		font-size: var(--font-size-l);
		font-weight: 700;
		color: var(--color-space-text-muted);
		transition: color 0.15s ease;
	}

	a.tab:hover {
		color: var(--color-space-text);
	}

	.tab--on {
		color: var(--accent);
	}

	.tab--soon {
		color: var(--color-set-1-fg-secondary);
		opacity: 0.6;
		cursor: default;
	}

	.side__foot {
		display: flex;
		flex-direction: column;
		gap: var(--space-xxs);
		margin-top: auto;
		font-size: var(--font-size-xs);
		color: var(--color-set-1-fg-secondary);
	}

	.side__foot > * {
		padding: var(--space-xxs) var(--space-s);
	}

	@media (max-width: 860px) {
		.side {
			flex: none;
			width: 100%;
			height: auto;
			position: static;
			align-self: auto;
			flex-direction: row;
			align-items: center;
			gap: var(--space-m);
			flex-wrap: wrap;
			padding: var(--space-s) var(--space-m);
			border-right: none;
			border-bottom: 1px solid var(--color-space-surface-faint);
		}

		.side__brand {
			padding: 0;
		}

		.side__brand img {
			max-width: 7.25rem;
		}

		.tabs {
			flex-direction: row;
			flex-wrap: wrap;
			gap: var(--space-xxs);
		}

		.tab {
			font-size: var(--font-size-m);
			padding: var(--space-xxs) var(--space-xs);
		}

		.side__foot {
			flex-direction: row;
			gap: var(--space-s);
			margin-top: 0;
			margin-left: auto;
		}
	}

	@media (max-width: 520px) {
		.side__foot {
			width: 100%;
			margin-left: 0;
		}
	}
</style>
