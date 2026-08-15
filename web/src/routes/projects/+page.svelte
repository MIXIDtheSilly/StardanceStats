<script lang="ts">
	import { fade } from 'svelte/transition';
	import { goto } from '$app/navigation';
	import { page as nav } from '$app/state';
	import Icon from '$components/Icon.svelte';
	import StarMark from '$components/StarMark.svelte';
	import ErrorState from '$components/ErrorState.svelte';
	import {
		PROJECT_METRICS,
		PROJECT_METRIC_GROUPS,
		projectMetric,
		metricValue,
		formatMetric,
		exactMetric
	} from '$lib/metrics';
	import { compact, full, relative } from '$lib/format';
	import type { ProjectCard } from '$lib/types';
	import type { PageData } from './$types';

	let { data }: { data: PageData } = $props();

	const ASIDE = [
		{ key: 'total_hours', icon: 'clock', label: 'Hours', size: '0.85rem' },
		{ key: 'devlogs', icon: 'notepad-text', label: 'Devlogs', size: '0.85rem' }
	];

	let query = $state('');
	let hits = $state<ProjectCard[]>([]);
	let looking = $state(false);
	let broken = $state<number[]>([]);

	let board = $derived(data.board);
	let active = $derived(projectMetric(data.metric));
	let items = $derived(board?.items ?? []);

	let needle = $derived(query.trim().toLowerCase());
	let searching = $derived(needle.length > 0);

	let onFirstPage = $derived(data.page === 1 && (board?.offset ?? 0) === 0);
	let podium = $derived(onFirstPage && !searching ? items.slice(0, 3) : []);
	// This page's own matches only stand in until the API answers, which searches every project.
	let found = $derived.by(() => {
		const list = looking
			? items.filter((p) => (p.title ?? '').toLowerCase().includes(needle))
			: hits;
		return [...list].sort((a, b) => (a.rank ?? Infinity) - (b.rank ?? Infinity));
	});

	let rows = $derived(searching ? found : onFirstPage ? items.slice(3) : items);

	let total = $derived(board?.total ?? 0);
	let firstRank = $derived((board?.offset ?? 0) + 1);
	let lastRank = $derived((board?.offset ?? 0) + items.length);
	let hasNext = $derived(lastRank < total);

	$effect(() => {
		const wanted = needle;
		// Read here as well as used below, so switching metric re-ranks what was found.
		const ranking = data.metric;
		// Held in a local: reading `looking` back here would make this effect depend on its own write.
		const asked = wanted.length > 0;
		hits = [];
		looking = asked;
		if (!asked) return;

		let dropped = false;
		const timer = setTimeout(async () => {
			const answer = await lookup(wanted, ranking);
			if (dropped) return;
			hits = answer;
			looking = false;
		}, 260);

		return () => {
			dropped = true;
			clearTimeout(timer);
		};
	});

	async function lookup(wanted: string, ranking: string): Promise<ProjectCard[]> {
		const search = `q=${encodeURIComponent(wanted)}&limit=${data.size}&metric=${encodeURIComponent(ranking)}`;
		try {
			const answer = await fetch(`/api/v1/projects/search?${search}`);
			if (!answer.ok) return [];
			return (await answer.json()).items ?? [];
		} catch {
			// A miss is the common case here, and it needs no telling.
			return [];
		}
	}

	function href(next: Record<string, string | null>): string {
		const url = new URL(nav.url);
		for (const [key, value] of Object.entries(next)) {
			if (value === null) url.searchParams.delete(key);
			else url.searchParams.set(key, value);
		}
		return `${url.pathname}${url.search}`;
	}

	function projectHref(id: number): string {
		return `/projects/${id}`;
	}

	function jump(url: string) {
		goto(url, { noScroll: true, keepFocus: true });
	}

	function openFirst(event: SubmitEvent) {
		event.preventDefault();
		const first = rows[0];
		if (first) goto(projectHref(first.project_id));
	}

	function banner(project: ProjectCard): string | null {
		if (!project.banner_url || broken.includes(project.project_id)) return null;
		return project.banner_url;
	}

	function stat(project: ProjectCard, key: string): number | null {
		const value = project.stats?.[key];
		return typeof value === 'number' ? value : null;
	}
</script>

<svelte:head>
	<title>Projects | Stardance Stats</title>
</svelte:head>

<div class="page">
	<header class="head">
		<h1>Projects</h1>

		{#if board?.data_as_of}
			<span class="muted" title="The stalest row on this page sets the stamp.">
				oldest row {relative(board.data_as_of)}
			</span>
		{/if}
	</header>

	{#if board}
		{#key `${data.metric}-${data.page}-${data.size}-${data.superStar}-${data.hardware}`}
			<div in:fade={{ duration: 160 }}>
				{#if podium.length}
					<section class="podium" aria-label="Top three">
						{#each podium as project (project.project_id)}
							<a
								class="seat"
								class:seat--first={project.rank === 1}
								style="--accent: {active.accent}"
								href={projectHref(project.project_id)}
							>
								{#if banner(project)}
									<img
										class="seat__banner"
										src={banner(project)}
										alt=""
										loading="lazy"
										decoding="async"
										referrerpolicy="no-referrer"
										onerror={() => (broken = [...broken, project.project_id])}
									/>
								{/if}

								<span class="seat__rank tabular">{project.rank}</span>
								<span class="seat__name">
									{project.title ?? project.project_id}
									{#if project.is_super_star}
										<StarMark size={13} />
									{/if}
								</span>
								{#if project.owner_username}
									<span class="seat__owner">by {project.owner_username}</span>
								{/if}
								<span class="seat__value tabular" title={exactMetric(active, project.value)}>
									{formatMetric(active, project.value)}
								</span>
							</a>
						{/each}
					</section>
				{/if}

				<section class="board">
					<div class="board__bar">
						<form class="find" onsubmit={openFirst} role="search">
							<span class="find__icon"><Icon name="search" size="0.9rem" /></span>
							<input
								type="search"
								bind:value={query}
								placeholder="search a title"
								aria-label="Search projects by title"
								autocomplete="off"
								spellcheck="false"
							/>
						</form>

						<a
							class="chip"
							class:chip--on={data.superStar}
							href={href({ super: data.superStar ? null : '1', page: null })}
						>
							<StarMark size={12} /> Super Stars
						</a>

						<a
							class="chip"
							class:chip--on={data.hardware}
							href={href({ hardware: data.hardware ? null : '1', page: null })}
						>
							Hardware
						</a>

						<label class="picker">
							<span class="picker__label">Ranked by</span>
							<span class="picker__field">
								<select
									value={data.metric}
									onchange={(event) => jump(href({ metric: event.currentTarget.value, page: null }))}
								>
									{#each PROJECT_METRIC_GROUPS as group (group)}
										<optgroup label={group}>
											{#each PROJECT_METRICS.filter((m) => m.group === group) as m (m.key)}
												<option value={m.key}>{m.label}</option>
											{/each}
										</optgroup>
									{/each}
								</select>
								<span class="picker__chevron">
									<Icon name="chevron-right" turn={1} size="0.8rem" />
								</span>
							</span>
						</label>

						<label class="picker picker--small">
							<span class="picker__label">Show</span>
							<span class="picker__field">
								<select
									value={String(data.size)}
									onchange={(event) => jump(href({ size: event.currentTarget.value, page: null }))}
								>
									{#each data.sizes as size (size)}
										<option value={String(size)}>{size}</option>
									{/each}
								</select>
								<span class="picker__chevron"><Icon name="chevron-right" turn={1} size="0.8rem" /></span>
							</span>
						</label>
					</div>

					<div class="board__head" aria-hidden="true">
						<span>#</span>
						<span>Project</span>
						<span class="right" title={active.blurb}>{active.label}</span>
					</div>

					{#snippet row(project: ProjectCard)}
						{@const value = metricValue(active, project)}
						<a class="row" href={projectHref(project.project_id)}>
							<span class="row__rank tabular" title={project.rank ? undefined : 'Not in this ranking'}>
								{project.rank ?? '·'}
							</span>

							<span class="row__what">
								{#if banner(project)}
									<img
										class="thumb"
										src={banner(project)}
										alt=""
										loading="lazy"
										decoding="async"
										referrerpolicy="no-referrer"
										onerror={() => (broken = [...broken, project.project_id])}
									/>
								{:else}
									<span class="thumb thumb--blank" aria-hidden="true"></span>
								{/if}

								<span class="row__lines">
									<span class="row__title">
										{project.title ?? project.project_id}
										{#if project.is_super_star}
											<StarMark size={12} />
										{/if}
									</span>
									<span class="row__by">
										{project.owner_username ?? 'owner unknown'}
										{#each ASIDE as item (item.key)}
											<span class="row__aside" title="{item.label}: {full(stat(project, item.key))}">
												<Icon name={item.icon} size={item.size} label={item.label} />
												<span class="tabular">{compact(stat(project, item.key))}</span>
											</span>
										{/each}
									</span>
								</span>
							</span>

							<span class="row__value tabular" title={exactMetric(active, value)}>
								{formatMetric(active, value)}
							</span>
						</a>
					{/snippet}

					{#if rows.length}
						<ol class="board__list">
							{#each rows as project (project.project_id)}
								<li>{@render row(project)}</li>
							{/each}
						</ol>
					{:else if searching}
						<!-- Held back until the lookup lands, so a half-typed title is not a miss. -->
						{#if !looking}
							<p class="empty muted">No title like that.</p>
						{/if}
					{:else}
						<p class="empty muted">
							{total ? 'No projects on this page. Try an earlier one.' : 'Nothing matches those filters.'}
						</p>
					{/if}
				</section>
			</div>
		{/key}

		<nav class="pager" aria-label="Pagination">
			{#if data.page > 1}
				<a class="pager__step" href={href({ page: String(data.page - 1) })}>
					<Icon name="chevron-right" turn={2} size="0.9rem" /> previous
				</a>
			{:else}
				<span class="pager__step pager__step--off">
					<Icon name="chevron-right" turn={2} size="0.9rem" /> previous
				</span>
			{/if}

			<span class="pager__count tabular">
				{#if items.length}
					{full(firstRank)}–{full(lastRank)} of {full(total)}
				{:else if total}
					past the end of {full(total)}
				{:else}
					nothing to page through
				{/if}
			</span>

			{#if hasNext}
				<a class="pager__step" href={href({ page: String(data.page + 1) })}>
					next <Icon name="chevron-right" size="0.9rem" />
				</a>
			{:else}
				<span class="pager__step pager__step--off">
					next <Icon name="chevron-right" size="0.9rem" />
				</span>
			{/if}
		</nav>
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
		font-size: var(--font-size-xs);
	}

	.picker {
		display: flex;
		align-items: center;
		gap: var(--space-xs);
	}

	.picker__label {
		font-size: var(--font-size-xs);
		text-transform: uppercase;
		letter-spacing: 0.08em;
		color: var(--color-set-1-fg-secondary);
	}

	.picker__field {
		position: relative;
		display: inline-flex;
		align-items: center;
	}

	.picker select {
		appearance: none;
		/* Without this the native dropdown opens white on a dark page. */
		color-scheme: dark;
		padding: var(--space-xxs) var(--space-xl) var(--space-xxs) var(--space-s);
		background: var(--color-space-bg);
		border: 1px solid var(--color-space-surface-faint);
		border-radius: var(--radius);
		font-size: var(--font-size-s);
		font-weight: 700;
		cursor: pointer;
	}

	.picker select:hover {
		border-color: var(--color-space-surface-soft);
	}

	.picker__chevron {
		position: absolute;
		right: var(--space-s);
		display: inline-flex;
		color: var(--color-set-1-fg-secondary);
		pointer-events: none;
	}

	.picker--small select {
		font-size: var(--font-size-xs);
		padding-right: var(--space-l);
	}

	.picker--small .picker__chevron {
		right: var(--space-xs);
	}

	.chip {
		display: inline-flex;
		align-items: center;
		gap: var(--space-xxs);
		padding: var(--space-xxs) var(--space-s);
		border: 1px solid var(--color-space-surface-faint);
		border-radius: var(--radius-pill);
		font-size: var(--font-size-xs);
		font-weight: 700;
		color: var(--color-set-1-fg-secondary);
		white-space: nowrap;
	}

	.chip:hover {
		border-color: var(--color-space-surface-soft);
		color: var(--color-space-text);
	}

	/* The hover pair too, or `.chip:hover` outweighs this and drains a live filter of its colour. */
	.chip--on,
	.chip--on:hover {
		border-color: var(--color-brand-embed);
		background: var(--color-brand-embed-soft);
		color: var(--color-brand-embed);
	}

	.podium {
		margin-top: var(--space-l);
		display: grid;
		grid-template-columns: repeat(3, 1fr);
		gap: var(--space-s);
		margin-bottom: var(--space-l);
	}

	.seat {
		position: relative;
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		gap: var(--space-xxs);
		padding: var(--space-m);
		border: 1px solid var(--color-space-surface-faint);
		border-radius: var(--radius);
		transition:
			border-color 0.15s ease,
			background 0.15s ease;
	}

	.seat:hover {
		background: var(--color-overlay-light-soft);
		border-color: var(--color-space-surface-soft);
		color: inherit;
	}

	.seat__banner {
		position: absolute;
		inset: 0;
		width: 100%;
		height: 100%;
		object-fit: cover;
		opacity: 0.25;
		filter: saturate(0.7);
		/* Rounded here rather than clipped on the seat, which would cut the lit corner off. */
		border-radius: inherit;
		mask-image: linear-gradient(to right, transparent 25%, black 90%);
	}

	.seat--first::before {
		content: '';
		position: absolute;
		inset: -1px;
		border: 1px solid var(--accent);
		border-radius: calc(var(--radius) + 1px);
		mask-image: radial-gradient(85% 85% at 0% 0%, #000 10%, transparent 70%);
		pointer-events: none;
		/* The banner comes later in the DOM, so the ring needs lifting over it. */
		z-index: 1;
	}

	.seat > span {
		position: relative;
	}

	.seat__rank {
		font-family: var(--font-display);
		font-size: var(--font-size-xl);
		color: var(--color-set-1-fg-secondary);
	}

	.seat--first .seat__rank {
		color: var(--accent);
	}

	.seat__name {
		display: flex;
		align-items: center;
		gap: var(--space-xxs);
		margin-top: var(--space-xxs);
		font-weight: 700;
		font-size: var(--font-size-m);
		overflow-wrap: anywhere;
	}

	.seat__owner {
		font-size: var(--font-size-xs);
		color: var(--color-set-1-fg-secondary);
	}

	.seat__value {
		margin-top: var(--space-xxs);
		font-size: var(--font-size-xxl);
		font-weight: 700;
		line-height: 1.15;
		color: var(--accent);
	}

	/* Unclipped, so a hovered thumbnail can grow past its row; the row rounds its own corners. */
	.board {
		border: 1px solid var(--color-space-surface-faint);
		border-radius: var(--radius);
	}

	.board > .board__list:last-child li:last-child .row,
	.board > .row:last-child {
		border-bottom-left-radius: calc(var(--radius) - 1px);
		border-bottom-right-radius: calc(var(--radius) - 1px);
	}

	.board__bar {
		display: flex;
		align-items: center;
		gap: var(--space-s) var(--space-m);
		flex-wrap: wrap;
		padding: var(--space-s) var(--space-m);
		border-bottom: 1px solid var(--color-space-surface-faint);
	}

	.find {
		position: relative;
		display: flex;
		align-items: center;
		flex: 1 1 12rem;
		max-width: 20rem;
		margin-right: auto;
	}

	.find__icon {
		position: absolute;
		left: var(--space-s);
		display: inline-flex;
		color: var(--color-set-1-fg-secondary);
		pointer-events: none;
	}

	.find input {
		width: 100%;
		padding: var(--space-xxs) var(--space-s) var(--space-xxs) var(--space-xl);
		background: var(--color-space-bg);
		border: 1px solid var(--color-space-surface-faint);
		border-radius: var(--radius-pill);
		font-size: var(--font-size-s);
	}

	.find input::placeholder {
		color: var(--color-set-1-fg-secondary);
	}

	/* The stock clear button is drawn dark, which all but disappears on this background. */
	.find input::-webkit-search-cancel-button {
		filter: invert(1);
		opacity: 0.45;
		cursor: pointer;
	}

	.find input:focus {
		outline: none;
		border-color: var(--color-brand-blue);
	}

	.board__head,
	.row {
		display: grid;
		grid-template-columns: 2.75rem 1fr auto;
		align-items: center;
		gap: var(--space-s);
		padding: var(--space-xs) var(--space-m);
	}

	.board__head {
		background: var(--color-overlay-light-soft);
		font-size: var(--font-size-xs);
		text-transform: uppercase;
		letter-spacing: 0.08em;
		color: var(--color-set-1-fg-secondary);
	}

	.board__list {
		list-style: none;
	}

	.board__list li + li .row {
		border-top: 1px solid var(--color-space-surface-faint);
	}

	.row {
		transition: background 0.12s ease;
	}

	.row:hover {
		background: var(--color-overlay-light-soft);
		color: inherit;
	}

	.row__rank {
		font-size: var(--font-size-s);
		color: var(--color-set-1-fg-secondary);
	}

	.row__what {
		display: flex;
		align-items: center;
		gap: var(--space-s);
		min-width: 0;
	}

	.thumb {
		flex: none;
		width: 3.5rem;
		aspect-ratio: 16 / 9;
		object-fit: cover;
		border-radius: 4px;
		background: var(--color-set-2-bg);
		/* Anchored at its left edge, which keeps the grown preview inside the board. */
		position: relative;
		transform-origin: center left;
		transition:
			transform 0.22s cubic-bezier(0.22, 1, 0.36, 1),
			box-shadow 0.22s cubic-bezier(0.22, 1, 0.36, 1);
	}

	.thumb:hover {
		z-index: 3;
		transform: scale(3.5);
		/* Small numbers: the transform multiplies the shadow along with everything else. */
		box-shadow: 0 2px 6px var(--color-shadow-deep);
	}

	.thumb--blank {
		background: linear-gradient(135deg, var(--color-set-2-bg), var(--color-set-1-bg));
	}

	.row__lines {
		display: flex;
		flex-direction: column;
		min-width: 0;
	}

	.row__title {
		display: flex;
		align-items: center;
		gap: var(--space-xxs);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.row:hover .row__title {
		color: var(--color-brand-blue);
	}

	.row__by {
		display: flex;
		align-items: center;
		gap: var(--space-s);
		font-size: var(--font-size-xs);
		color: var(--color-set-1-fg-secondary);
		white-space: nowrap;
	}

	.row__aside {
		display: inline-flex;
		align-items: center;
		gap: var(--space-xxs);
	}

	.row__value {
		font-weight: 700;
		text-align: right;
	}

	.right {
		text-align: right;
	}

	.empty {
		padding: var(--space-xxl) 0;
		text-align: center;
		font-size: var(--font-size-s);
	}

	.pager {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: var(--space-m);
		margin-top: var(--space-m);
		font-size: var(--font-size-s);
	}

	.pager__step {
		display: inline-flex;
		align-items: center;
		gap: var(--space-xs);
		font-weight: 700;
		color: var(--color-space-text-muted);
	}

	.pager__step--off {
		opacity: 0.3;
	}

	.pager__count {
		color: var(--color-set-1-fg-secondary);
		font-size: var(--font-size-xs);
	}

	@media (max-width: 860px) {
		.podium {
			grid-template-columns: 1fr;
		}
	}

	@media (max-width: 560px) {
		.find {
			max-width: none;
		}

		.board__head,
		.row {
			grid-template-columns: 2rem 1fr auto;
			padding: var(--space-xs) var(--space-s);
		}

		.row__aside {
			display: none;
		}

		.thumb {
			display: none;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.thumb {
			transition: none;
		}
	}
</style>
