<script lang="ts">
	import { fade } from 'svelte/transition';
	import { goto } from '$app/navigation';
	import { page as nav } from '$app/state';
	import Avatar from '$components/Avatar.svelte';
	import Icon from '$components/Icon.svelte';
	import ErrorState from '$components/ErrorState.svelte';
	import {
		PEOPLE_METRICS,
		METRIC_GROUPS,
		metric,
		metricValue,
		formatMetric,
		exactMetric
	} from '$lib/metrics';
	import { full, relative } from '$lib/format';
	import type { UserDoc } from '$lib/types';
	import type { PageData } from './$types';

	let { data }: { data: PageData } = $props();

	interface Found {
		user_id: number;
		username: string | null;
		avatar_url: string | null;
		/** Where they stand in the ranking on show, or null if it does not hold them. */
		rank: number | null;
		stats: Record<string, number | null>;
		totals: Record<string, number | null>;
	}

	let query = $state('');
	/** People the API found, for the ones this page does not hold. */
	let hits = $state<Found[]>([]);

	let board = $derived(data.board);
	let active = $derived(metric(data.metric));
	let items = $derived(board?.items ?? []);

	let needle = $derived(query.trim().replace(/^@/, '').toLowerCase());
	let searching = $derived(needle.length > 0);

	// Ranks 1 to 3 get the podium, so the list picks up where it leaves off.
	let onFirstPage = $derived(data.page === 1 && (board?.offset ?? 0) === 0);
	let podium = $derived(onFirstPage && !searching ? items.slice(0, 3) : []);
	let rows = $derived(
		searching
			? items.filter((p) => (p.username ?? '').toLowerCase().includes(needle))
			: onFirstPage
				? items.slice(3)
				: items
	);

	let elsewhere = $derived(hits.filter((h) => !rows.some((p) => p.user_id === h.user_id)));

	let total = $derived(board?.total ?? 0);
	let firstRank = $derived((board?.offset ?? 0) + 1);
	let lastRank = $derived((board?.offset ?? 0) + items.length);
	let hasNext = $derived(lastRank < total);

	// What someone types is rarely on the page they are looking at, so ask the API too.
	$effect(() => {
		const wanted = needle;
		// Read here as well as used below, so switching metric re-ranks what was found.
		const ranking = data.metric;
		hits = [];
		if (wanted.length < 2) return;

		let dropped = false;
		const timer = setTimeout(async () => {
			const found = await lookup(wanted, ranking);
			if (!dropped) hits = found;
		}, 260);

		return () => {
			dropped = true;
			clearTimeout(timer);
		};
	});

	/** Search where the API offers it, exact handle where it does not yet. */
	async function lookup(wanted: string, ranking: string): Promise<Found[]> {
		const query = `q=${encodeURIComponent(wanted)}&limit=8&metric=${encodeURIComponent(ranking)}`;
		try {
			const search = await fetch(`/api/v1/users/search?${query}`);
			if (search.ok) return (await search.json()).items ?? [];

			const exact = await fetch(`/api/v1/users/${encodeURIComponent(wanted)}`);
			if (!exact.ok) return [];
			const user: UserDoc = await exact.json();
			return [
				{
					user_id: user._id,
					username: user.username,
					avatar_url: user.avatar_url ?? null,
					rank: null,
					stats: user.stats ?? {},
					totals: user.totals ?? {}
				}
			];
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

	function personHref(username: string | null, id: number): string {
		return `/people/${encodeURIComponent(username ?? String(id))}`;
	}

	function jump(url: string) {
		goto(url, { noScroll: true, keepFocus: true });
	}

	function openFirst(event: SubmitEvent) {
		event.preventDefault();
		const first = rows[0] ?? elsewhere[0];
		if (first) goto(personHref(first.username, first.user_id));
	}
</script>

<svelte:head>
	<title>People | Stardance Stats</title>
</svelte:head>

<div class="page">
	<header class="head">
		<h1>People</h1>

		{#if board}
			<!-- The API stamps a page with its stalest row, not with the newest crawl. -->
			<span class="muted" title="The stalest row on this page sets the stamp.">
				oldest row {relative(board.data_as_of)}
			</span>
		{/if}
	</header>

	{#if board}
		{#key `${data.metric}-${data.page}-${data.size}`}
			<div in:fade={{ duration: 160 }}>
				{#if podium.length}
					<section class="podium" aria-label="Top three">
						{#each podium as person (person.user_id)}
							<a
								class="seat"
								class:seat--first={person.rank === 1}
								style="--accent: {active.accent}"
								href={personHref(person.username, person.user_id)}
							>
								<span class="seat__rank tabular">{person.rank}</span>
								<Avatar src={person.avatar_url} name={person.username} size="3.25rem" />
								<span class="seat__name">{person.username ?? person.user_id}</span>
								<span class="seat__value tabular" title={exactMetric(active, person.value)}>
									{formatMetric(active, person.value)}
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
								placeholder="search a handle"
								aria-label="Search people by handle"
								autocomplete="off"
								spellcheck="false"
							/>
						</form>

						<label class="picker">
							<span class="picker__label">Ranked by</span>
							<span class="picker__field">
								<select
									value={data.metric}
									onchange={(event) => jump(href({ metric: event.currentTarget.value, page: null }))}
								>
									{#each METRIC_GROUPS as group (group)}
										<optgroup label={group}>
											{#each PEOPLE_METRICS.filter((m) => m.group === group) as m (m.key)}
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
						<span>Person</span>
						<span class="right" title={active.blurb}>{active.label}</span>
					</div>

					{#if rows.length}
						<ol class="board__list">
							{#each rows as person (person.user_id)}
								<li>
									<a class="row" href={personHref(person.username, person.user_id)}>
										<span class="row__rank tabular">{person.rank}</span>
										<span class="row__who">
											<Avatar src={person.avatar_url} name={person.username} size="1.75rem" />
											<span class="row__name">{person.username ?? person.user_id}</span>
										</span>
										<span
											class="row__value tabular"
											title={exactMetric(active, person.value)}
										>
											{formatMetric(active, person.value)}
										</span>
									</a>
								</li>
							{/each}
						</ol>
					{:else if !searching}
						<p class="empty muted">Nobody on this page. Try an earlier one.</p>
					{:else if !elsewhere.length}
						<p class="empty muted">No handle like that.</p>
					{/if}

					{#if elsewhere.length}
						<p class="board__note">Found by search, off this page</p>
						{#each elsewhere as person (person.user_id)}
							<a class="row row--found" href={personHref(person.username, person.user_id)}>
								<span class="row__rank tabular" title={person.rank ? 'Rank in this ranking' : 'Not in this ranking'}>
									{person.rank ?? '·'}
								</span>
								<span class="row__who">
									<Avatar src={person.avatar_url} name={person.username} size="1.75rem" />
									<span class="row__name">{person.username}</span>
								</span>
								<span class="row__value tabular">
									{formatMetric(active, metricValue(active, person))}
								</span>
							</a>
						{/each}
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
				{:else}
					past the end of {full(total)}
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

	.head span {
		font-size: var(--font-size-xs);
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

	/* Sits over the plain border so the top-left corner lights up and fades along both edges. */
	.seat--first::before {
		content: '';
		position: absolute;
		inset: -1px;
		border: 1px solid var(--accent);
		border-radius: calc(var(--radius) + 1px);
		mask-image: radial-gradient(85% 85% at 0% 0%, #000 10%, transparent 70%);
		pointer-events: none;
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
		margin-top: var(--space-xxs);
		font-weight: 700;
		font-size: var(--font-size-m);
		overflow-wrap: anywhere;
	}

	.seat__value {
		font-size: var(--font-size-xxl);
		font-weight: 700;
		line-height: 1.15;
		color: var(--accent);
	}

	.board {
		border: 1px solid var(--color-space-surface-faint);
		border-radius: var(--radius);
		overflow: hidden;
	}

	.board__bar {
		display: flex;
		align-items: center;
		gap: var(--space-m);
		flex-wrap: wrap;
		padding: var(--space-s) var(--space-m);
		border-bottom: 1px solid var(--color-space-surface-faint);
	}

	.find {
		position: relative;
		display: flex;
		align-items: center;
		flex: 1 1 12rem;
		max-width: 22rem;
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
		border-color: var(--color-brand-lilac);
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

	.row--found {
		border-bottom: 1px solid var(--color-space-surface-faint);
		background: var(--color-brand-lilac-soft);
	}

	.row__rank {
		font-size: var(--font-size-s);
		color: var(--color-set-1-fg-secondary);
	}

	.row__who {
		display: flex;
		align-items: center;
		gap: var(--space-s);
		min-width: 0;
	}

	.row__name {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.board__note {
		padding: var(--space-xs) var(--space-m);
		border-top: 1px solid var(--color-space-surface-faint);
		font-size: var(--font-size-xs);
		text-transform: uppercase;
		letter-spacing: 0.08em;
		color: var(--color-set-1-fg-secondary);
	}

	.row:hover .row__name {
		color: var(--color-brand-lilac);
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
		.board__bar {
			flex-wrap: wrap;
		}

		.find {
			max-width: none;
		}

		.board__head,
		.row {
			grid-template-columns: 2rem 1fr auto;
			padding: var(--space-xs) var(--space-s);
		}

		.board__note {
			padding: var(--space-xs) var(--space-s);
		}
	}
</style>
