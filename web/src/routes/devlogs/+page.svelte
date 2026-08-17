<script lang="ts">
	import { untrack } from 'svelte';
	import { fade } from 'svelte/transition';
	import { goto } from '$app/navigation';
	import { page as nav } from '$app/state';
	import Avatar from '$components/Avatar.svelte';
	import Carousel from '$components/Carousel.svelte';
	import ErrorState from '$components/ErrorState.svelte';
	import Icon from '$components/Icon.svelte';
	import Perch from '$components/Perch.svelte';
	import StarMark from '$components/StarMark.svelte';
	import { compact, full, relative } from '$lib/format';
	import { firstMatch, highlight } from '$lib/highlight';
	import type { DevlogCard, DevlogMedia } from '$lib/types';
	import type { PageData } from './$types';

	let { data }: { data: PageData } = $props();

	/** Value is `sort` alone, or `sort:asc` where the ranking runs the other way. */
	const ORDERS = [
		{ value: 'relevance', label: 'Best match', searchOnly: true },
		{ value: 'posted_at', label: 'Newest' },
		{ value: 'posted_at:asc', label: 'Oldest' },
		{ value: 'duration_seconds', label: 'Longest' },
		{ value: 'duration_seconds:asc', label: 'Shortest' },
		{ value: 'likes', label: 'Most liked' },
		{ value: 'comments', label: 'Most discussed' },
		{ value: 'views', label: 'Most viewed' },
		{ value: 'reposts', label: 'Most reposted' }
	];

	const FIGURES = [
		{ key: 'duration_seconds', icon: 'clock', label: 'Hours logged' },
		{ key: 'likes', icon: 'heart', label: 'Likes' },
		{ key: 'comments', icon: 'message-circle', label: 'Comments' },
		{ key: 'views', icon: 'eye', label: 'Views' },
		{ key: 'reposts', icon: 'repeat-2', label: 'Reposts' }
	];

	const SPANS = [1, 3, 7, 14, 30];

	/** The API refuses anything shorter. */
	const MIN_QUERY = 2;

	const LEAD = 200;

	let brokenMedia = $state<string[]>([]);
	let clipped = $state<Record<number, boolean>>({});
	let opened = $state<DevlogCard | null>(null);
	let sheet = $state<HTMLDialogElement | null>(null);

	// showModal is what brings the backdrop, the focus trap and Escape with it.
	$effect(() => {
		if (!sheet) return;
		if (opened && !sheet.open) sheet.showModal();
		if (!opened && sheet.open) sheet.close();
	});

	let feed = $derived(data.feed);
	let logs = $derived(feed?.items ?? []);
	let total = $derived(feed?.total ?? 0);
	let first = $derived((feed?.offset ?? 0) + 1);
	let last = $derived((feed?.offset ?? 0) + logs.length);
	let hasNext = $derived(last < total);
	let ranked = $derived(data.sort !== 'posted_at');
	let choice = $derived(data.order === 'asc' ? `${data.sort}:asc` : data.sort);
	let orders = $derived(ORDERS.filter((option) => data.q || !option.searchOnly));

	let query = $state(untrack(() => data.q));
	// The search this page asked for last, which tells our own navigation from a back button.
	let mine = untrack(() => data.q);

	$effect(() => {
		if (data.q === mine) return;
		mine = data.q;
		query = data.q;
	});

	$effect(() => {
		const wanted = query.trim();
		const sending = wanted.length >= MIN_QUERY ? wanted : '';
		if (sending === data.q) return;

		const timer = setTimeout(() => {
			mine = sending;
			goto(href({ q: sending || null, page: null }), { keepFocus: true, noScroll: true });
		}, 280);
		return () => clearTimeout(timer);
	});

	function href(next: Record<string, string | null>): string {
		const url = new URL(nav.url);
		for (const [key, value] of Object.entries(next)) {
			if (value === null) url.searchParams.delete(key);
			else url.searchParams.set(key, value);
		}
		return `${url.pathname}${url.search}`;
	}

	function jump(url: string) {
		goto(url, { noScroll: true, keepFocus: true });
	}

	function day(iso: string | null | undefined): string {
		if (!iso) return 'unknown';
		const date = new Date(iso);
		return Number.isNaN(date.getTime())
			? 'unknown'
			: date.toLocaleDateString('en', { month: 'short', day: 'numeric', year: 'numeric' });
	}

	function reading(log: DevlogCard, key: string): string {
		const value = log[key as keyof DevlogCard];
		if (typeof value !== 'number') return '--';
		return key === 'duration_seconds' ? `${(value / 3600).toFixed(1)}h` : compact(value);
	}

	function attachments(log: DevlogCard): DevlogMedia[] {
		return (log.media ?? []).filter((item) => !brokenMedia.includes(item.url));
	}

	function clamped(node: HTMLElement, id: number) {
		const measure = () => (clipped[id] = node.scrollHeight > node.clientHeight + 2);
		measure();
		// The grid drops a column as the page narrows, changing what fits in the clamp.
		const observer = new ResizeObserver(measure);
		observer.observe(node);
		return { destroy: () => observer.disconnect() };
	}

	function expandable(log: DevlogCard, shots: DevlogMedia[]): boolean {
		return clipped[log._id] === true || shots.length > 0;
	}

	// Rows crawled before the parser kept whole posts still have their 280-character preview.
	function words(log: DevlogCard): string | null {
		return log.body ?? log.body_preview ?? null;
	}

	/** What the card shows: a match deep in a long post would sit under the clamp. */
	function excerpt(log: DevlogCard): { text: string; cut: boolean } {
		const text = words(log) ?? '';
		const at = firstMatch(text, data.q);
		if (at < LEAD) return { text, cut: false };
		const from = text.lastIndexOf(' ', at - LEAD / 2) + 1;
		return { text: text.slice(from || at), cut: true };
	}

	function devlogHref(log: DevlogCard): string {
		return `https://stardance.hackclub.com/projects/${log.project_id}/devlogs/${log._id}`;
	}
</script>

<svelte:head>
	<title>Devlogs | Stardance Stats</title>
</svelte:head>

<div class="page">
	<header class="head">
		<h1>Devlogs</h1>

		{#if feed?.data_as_of}
			<span class="muted" title="The stalest card on this page sets the stamp.">
				oldest card {relative(feed.data_as_of)}
			</span>
		{/if}
	</header>

	{#if feed}
		<div class="bar">
			<form class="find" onsubmit={(event) => event.preventDefault()} role="search">
				<span class="find__icon"><Icon name="search" size="0.9rem" /></span>
				<input
					type="search"
					bind:value={query}
					placeholder="search the text of a devlog"
					aria-label="Search devlogs by their words"
					autocomplete="off"
					spellcheck="false"
				/>
			</form>

			<div class="spans" role="group" aria-label="Posted within">
				<!-- Anchored to the group, so it keeps its seat on "All time" as the bar rewraps. -->
				<Perch
					art={data.days === null ? 'guest_star_3' : 'step-review--outline'}
					at="top-left"
					w="3rem"
					x="25%"
					y="-67%"
				/>
				<a class="chip" class:chip--on={data.days === null} href={href({ days: null, page: null })}>
					All time
				</a>
				{#each SPANS as span (span)}
					<a
						class="chip"
						class:chip--on={data.days === span}
						href={href({ days: String(span), page: null })}
					>
						{span}d
					</a>
				{/each}
			</div>

			<label class="picker">
				<span class="picker__label">Sort</span>
				<span class="picker__field">
					<select
						value={choice}
						onchange={(event) => jump(href({ sort: event.currentTarget.value, page: null }))}
					>
						{#each orders as option (option.value)}
							<option value={option.value}>{option.label}</option>
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
					<span class="picker__chevron">
						<Icon name="chevron-right" turn={1} size="0.8rem" />
					</span>
				</span>
			</label>
		</div>

		{#if logs.length}
			{#key `${data.q}-${data.sort}-${data.order}-${data.days}-${data.page}-${data.size}`}
				<ul class="logs" in:fade={{ duration: 160 }}>
					{#each logs as log (log._id)}
						{@const shots = attachments(log)}
						{@const post = excerpt(log)}
						<li class="log" class:log--text={!shots.length}>
							<div class="log__head">
								{#if log.username}
									<a class="log__who" href="/people/{log.username}">
										<Avatar src={log.avatar_url} name={log.username} size="1.4rem" />
										<span class="log__handle">{log.username}</span>
									</a>
								{:else}
									<span class="log__handle muted">author unknown</span>
								{/if}

								{#if ranked && log.rank}
									<span class="log__rank tabular">#{log.rank}</span>
								{/if}

								<a
									class="log__link"
									href={devlogHref(log)}
									target="_blank"
									rel="noreferrer noopener"
									aria-label="Open this devlog on Stardance"
								>
									<Icon name="external-link" size="0.85rem" />
								</a>
							</div>

							<div class="log__where">
								<a class="log__project" href="/projects/{log.project_id}">
									{log.project_title ?? `project ${log.project_id}`}
								</a>
								{#if log.is_super_star}
									<StarMark size={11} />
								{/if}
								<span class="log__when">{day(log.posted_at)}</span>
							</div>

							<div class="log__figures">
								{#each FIGURES as figure (figure.key)}
									<span
										class="log__figure"
										class:log__figure--on={data.sort === figure.key}
										title={figure.label}
									>
										<Icon name={figure.icon} size="0.85rem" />
										<span class="tabular">{reading(log, figure.key)}</span>
									</span>
								{/each}
							</div>

							{#if post.text}
								<p
									class="log__body"
									class:log__body--faded={clipped[log._id]}
									class:log__body--cut={post.cut}
									use:clamped={log._id}
								>
									<!-- highlight() escapes; that is what lets the marks through. -->
									{@html highlight(post.text, data.q)}
								</p>
							{/if}

							{#if shots.length}
								<div class="shots">
									<Carousel
										items={shots}
										label="Attachments on this devlog"
										onbroken={(url) => (brokenMedia = [...brokenMedia, url])}
									/>
								</div>
							{/if}

							{#if expandable(log, shots)}
								<button
									class="log__more"
									class:log__more--tight={clipped[log._id]}
									type="button"
									onclick={() => (opened = log)}
								>
									See more <Icon name="expand" size="0.75rem" />
								</button>
							{/if}
						</li>
					{/each}
				</ul>
			{/key}
		{:else}
			<p class="empty muted">
				{#if total}
					No devlogs on this page. Try an earlier one.
				{:else if data.q}
					No devlog says “{data.q}”.
				{:else}
					Nothing matches those filters yet.
				{/if}
			</p>
		{/if}

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
				{#if logs.length}
					{full(first)}–{full(last)} of {full(total)}
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

<!-- Clicking the backdrop is a click on the dialog itself, which its own panel swallows. -->
<!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_noninteractive_element_interactions -->
<dialog
	class="sheet"
	bind:this={sheet}
	onclose={() => (opened = null)}
	onclick={(event) => {
		if (event.target === sheet) opened = null;
	}}
>
	{#if opened}
		{@const shots = attachments(opened)}
		<article class="sheet__panel">
			<header class="sheet__head">
				<div class="sheet__who">
					{#if opened.username}
						<a class="log__who" href="/people/{opened.username}">
							<Avatar src={opened.avatar_url} name={opened.username} size="1.6rem" />
							<span class="log__handle">{opened.username}</span>
						</a>
					{/if}
					<a
						class="log__link"
						href={devlogHref(opened)}
						target="_blank"
						rel="noreferrer noopener"
						aria-label="Open this devlog on Stardance"
					>
						<Icon name="external-link" size="0.9rem" />
					</a>
				</div>

				<div class="sheet__meta">
					<a class="log__project" href="/projects/{opened.project_id}">
						{opened.project_title ?? `project ${opened.project_id}`}
					</a>
					<span class="log__when">{day(opened.posted_at)}</span>
				</div>

				<button class="sheet__close" type="button" onclick={() => (opened = null)} aria-label="Close">
					<Icon name="close" size="0.9rem" />
				</button>

				<div class="log__figures">
					{#each FIGURES as figure (figure.key)}
						<span class="log__figure" title={figure.label}>
							<Icon name={figure.icon} size="0.85rem" />
							<span class="tabular">{reading(opened, figure.key)}</span>
						</span>
					{/each}
				</div>
			</header>

			<div class="sheet__body">
				{#if words(opened)}
					<p class="sheet__text">{@html highlight(words(opened) ?? '', data.q)}</p>
				{/if}

				{#each shots as item, index (item.url)}
					{#if item.kind === 'video'}
						<!-- svelte-ignore a11y_media_has_caption -->
						<video class="sheet__shot" src={item.url} poster={item.poster} controls></video>
					{:else}
						<a
							href={item.url}
							target="_blank"
							rel="noreferrer noopener"
							aria-label="Open attachment {index + 1} of {shots.length} at full size"
						>
							<img
								class="sheet__shot"
								src={item.url}
								alt=""
								decoding="async"
								referrerpolicy="no-referrer"
							/>
						</a>
					{/if}
				{/each}
			</div>
		</article>
	{/if}
</dialog>

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

	.bar {
		display: flex;
		align-items: center;
		gap: var(--space-s) var(--space-m);
		flex-wrap: wrap;
		padding: var(--space-m) 0;
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

	.spans {
		position: relative;
		display: flex;
		gap: var(--space-xxs);
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

	.logs {
		list-style: none;
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(22rem, 1fr));
		gap: var(--space-s);
	}

	/* One height for every card, so text length clips rather than stretching the grid. */
	.log {
		display: flex;
		flex-direction: column;
		height: 24rem;
		padding: var(--space-s) var(--space-m);
		border: 1px solid var(--color-space-surface-faint);
		border-radius: var(--radius);
		overflow: hidden;
	}

	.log__head {
		display: flex;
		align-items: center;
		gap: var(--space-s);
	}

	.log__who {
		display: inline-flex;
		align-items: center;
		gap: var(--space-xs);
		min-width: 0;
		margin-right: auto;
	}

	.log__handle {
		font-size: var(--font-size-s);
		font-weight: 700;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.log__who:hover .log__handle {
		color: var(--color-brand-lilac);
	}

	.log__rank {
		font-size: var(--font-size-xs);
		font-weight: 700;
		color: var(--color-brand-yellow);
	}

	.log__link {
		display: inline-flex;
		color: var(--color-set-1-fg-secondary);
	}

	.log__link:hover {
		color: var(--color-brand-blue);
	}

	.log__where {
		display: flex;
		align-items: center;
		gap: var(--space-xs);
		margin-top: var(--space-xxs);
		font-size: var(--font-size-xs);
		color: var(--color-set-1-fg-secondary);
		min-width: 0;
	}

	.log__project {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		color: var(--color-space-text-muted);
	}

	.log__project:hover {
		color: var(--color-brand-blue);
	}

	.log__when {
		margin-left: auto;
		white-space: nowrap;
	}

	.log__figures {
		display: flex;
		flex-wrap: wrap;
		gap: var(--space-xxs) var(--space-s);
		margin-top: var(--space-xs);
		font-size: var(--font-size-xs);
		color: var(--color-set-1-fg-secondary);
	}

	.log__figure {
		display: inline-flex;
		align-items: center;
		gap: var(--space-xxs);
	}

	.log__figure--on {
		font-weight: 700;
		color: var(--color-brand-yellow);
	}

	.log__body {
		margin-top: var(--space-xs);
		font-size: var(--font-size-s);
		line-height: 1.5;
		color: var(--color-space-text-muted);
		/* The parser keeps the post's own breaks; this is what renders them. */
		white-space: pre-line;
		max-height: 6em;
		overflow: hidden;
	}

	.log--text .log__body {
		flex: 1;
		max-height: none;
		min-height: 0;
	}

	/* Global: the marks are injected as HTML, which carries no scoping class. */
	.log__body :global(mark),
	.sheet__text :global(mark) {
		padding: 0 2px;
		border-radius: 3px;
		background: var(--color-brand-yellow-soft);
		color: var(--color-brand-yellow);
	}

	.log__body--faded {
		-webkit-mask-image: linear-gradient(to bottom, #000 55%, transparent 98%);
		mask-image: linear-gradient(to bottom, #000 55%, transparent 98%);
	}

	.log__body--cut {
		-webkit-mask-image: linear-gradient(to bottom, transparent 0, #000 12%);
		mask-image: linear-gradient(to bottom, transparent 0, #000 12%);
	}

	/* One gradient carrying both edges: two mask-images would replace, not stack. */
	.log__body--cut.log__body--faded {
		-webkit-mask-image: linear-gradient(to bottom, transparent 0, #000 12%, #000 55%, transparent 98%);
		mask-image: linear-gradient(to bottom, transparent 0, #000 12%, #000 55%, transparent 98%);
	}

	.shots {
		display: grid;
		margin-top: var(--space-s);
		flex: 1;
		min-height: 0;
	}

	.log__more {
		align-self: flex-start;
		display: inline-flex;
		align-items: center;
		gap: var(--space-xxs);
		margin-top: var(--space-xs);
		padding: 0;
		border: none;
		background: none;
		font-size: var(--font-size-xs);
		font-weight: 700;
		color: var(--color-brand-blue);
		cursor: pointer;
	}

	.log__more--tight {
		margin-top: var(--space-xxs);
	}

	.log__more:hover {
		color: var(--color-space-text);
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

	.sheet {
		width: min(64rem, 92vw);
		max-height: 88vh;
		margin: 4vh auto auto;
		padding: 0;
		border: 1px solid var(--color-space-surface-soft);
		border-radius: var(--radius);
		background: var(--color-space-bg);
		color: var(--color-space-text);
		overflow: hidden;
	}

	.sheet::backdrop {
		background: var(--color-backdrop);
		backdrop-filter: blur(2px);
	}

	.sheet__panel {
		display: flex;
		flex-direction: column;
		max-height: 88vh;
	}

	.sheet__head {
		display: grid;
		grid-template-columns: auto 1fr auto;
		align-items: center;
		gap: var(--space-xs) var(--space-m);
		padding: var(--space-m);
		border-bottom: 1px solid var(--color-space-surface-faint);
	}

	.sheet__who {
		display: flex;
		align-items: center;
		gap: var(--space-s);
	}

	.sheet__meta {
		display: flex;
		align-items: baseline;
		gap: var(--space-m);
		min-width: 0;
		font-size: var(--font-size-xs);
		color: var(--color-set-1-fg-secondary);
	}

	.sheet__meta .log__when {
		margin-left: 0;
	}

	.sheet__head .log__figures {
		grid-column: 1 / -1;
		margin-top: 0;
	}

	.sheet__close {
		display: inline-flex;
		padding: var(--space-xxs);
		border: none;
		border-radius: var(--radius);
		background: none;
		color: var(--color-set-1-fg-secondary);
		cursor: pointer;
	}

	.sheet__close:hover {
		background: var(--color-overlay-light-soft);
		color: var(--color-space-text);
	}

	.sheet__body {
		display: flex;
		flex-direction: column;
		gap: var(--space-m);
		padding: var(--space-m);
		overflow-y: auto;
	}

	.sheet__text {
		font-size: var(--font-size-m);
		line-height: 1.6;
		color: var(--color-space-text-muted);
		white-space: pre-line;
	}

	.sheet__shot {
		display: block;
		width: 100%;
		height: auto;
		border: 1px solid var(--color-space-surface-faint);
		border-radius: var(--radius);
		background: var(--color-set-2-bg);
	}

	@media (max-width: 560px) {
		.logs {
			grid-template-columns: minmax(0, 1fr);
		}

		.find {
			max-width: none;
		}

		.spans {
			width: 100%;
		}

		.sheet__head {
			grid-template-columns: 1fr auto;
		}

		.sheet__meta {
			grid-column: 1 / -1;
		}
	}
</style>
