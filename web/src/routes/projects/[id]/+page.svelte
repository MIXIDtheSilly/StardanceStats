<script lang="ts">
	import { fade } from 'svelte/transition';
	import { goto } from '$app/navigation';
	import { page as nav } from '$app/state';
	import Avatar from '$components/Avatar.svelte';
	import Carousel from '$components/Carousel.svelte';
	import Chart from '$components/Chart.svelte';
	import GithubMark from '$components/GithubMark.svelte';
	import Icon from '$components/Icon.svelte';
	import Perch from '$components/Perch.svelte';
	import RangePicker from '$components/RangePicker.svelte';
	import StarMark from '$components/StarMark.svelte';
	import {
		PROJECT_TILES,
		projectMetric,
		metricValue,
		formatMetric,
		exactMetric
	} from '$lib/metrics';
	import { compact, full, relative, signed } from '$lib/format';
	import type { DevlogDoc, DevlogMedia, ShipDoc } from '$lib/types';
	import type { PageData } from './$types';

	let { data }: { data: PageData } = $props();

	const RANGES = [1, 3, 7, 14, 30];
	const TILES = PROJECT_TILES.map((key) => projectMetric(key));

	const ORDERS = [
		{ key: 'posted_at', label: 'Newest' },
		{ key: 'duration_seconds', label: 'Longest' },
		{ key: 'likes', label: 'Most liked' },
		{ key: 'comments', label: 'Most discussed' },
		{ key: 'views', label: 'Most viewed' }
	];

	let selected = $state('stardust_total');
	let range = $state(7);
	let bannerBroken = $state(false);
	let brokenMedia = $state<string[]>([]);
	let opened = $state<DevlogDoc | null>(null);
	let sheet = $state<HTMLDialogElement | null>(null);

	// showModal is what brings the backdrop, the focus trap and Escape with it.
	$effect(() => {
		if (!sheet) return;
		if (opened && !sheet.open) sheet.showModal();
		if (!opened && sheet.open) sheet.close();
	});

	let project = $derived(data.project);
	let series = $derived(data.history?.series ?? {});
	let ships = $derived([...(data.ships?.items ?? [])].sort(newestFirst));
	let devlogs = $derived(data.devlogs?.items ?? []);
	let logTotal = $derived(data.devlogs?.total ?? 0);
	let firstLog = $derived((data.devlogs?.offset ?? 0) + 1);
	let lastLog = $derived((data.devlogs?.offset ?? 0) + devlogs.length);
	let moreLogs = $derived(lastLog < logTotal);
	let active = $derived(projectMetric(selected));
	let points = $derived(windowed(selected));

	function observedPoints(key: string) {
		return (series[key] ?? []).filter((p) => Number.isFinite(p.v));
	}

	// Measured off the newest point, not off now: buckets are hourly and can lag.
	function windowed(key: string) {
		const observed = observedPoints(key);
		const last = observed.at(-1);
		if (!last) return [];
		const cutoff = Date.parse(last.ts) - range * 86_400_000;
		return observed.filter((p) => Date.parse(p.ts) >= cutoff);
	}

	function movement(key: string): { delta: number; days: number } | null {
		const window = windowed(key);
		if (window.length < 2) return null;
		const first = window[0];
		const last = window[window.length - 1];
		return {
			delta: last.v - first.v,
			days: Math.max(1, Math.round((Date.parse(last.ts) - Date.parse(first.ts)) / 86_400_000))
		};
	}

	let span = $derived(movement(selected)?.days ?? 0);

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

	function hours(seconds: number | null | undefined): string {
		if (typeof seconds !== 'number') return '--';
		return `${(seconds / 3600).toFixed(1)}h`;
	}

	function newestFirst(a: ShipDoc, b: ShipDoc): number {
		const first = Date.parse(a.shipped_at ?? '');
		const second = Date.parse(b.shipped_at ?? '');
		if (Number.isNaN(first) || Number.isNaN(second)) {
			return (b.ship_number ?? 0) - (a.ship_number ?? 0);
		}
		return second - first;
	}

	// A ship pays out only once its review closes, so an empty payout is a state, not a zero.
	function payout(ship: ShipDoc): string {
		return typeof ship.payout === 'number' ? full(ship.payout) : 'pending';
	}

	function attachments(log: DevlogDoc): DevlogMedia[] {
		return (log.media ?? []).filter((item) => !brokenMedia.includes(item.url));
	}

	let clipped = $state<Record<number, boolean>>({});

	function clamped(node: HTMLElement, id: number) {
		const measure = () => (clipped[id] = node.scrollHeight > node.clientHeight + 2);
		measure();
		// The column halves at 900px, which changes what fits in the clamp.
		const observer = new ResizeObserver(measure);
		observer.observe(node);
		return { destroy: () => observer.disconnect() };
	}

	function expandable(log: DevlogDoc, shots: DevlogMedia[]): boolean {
		return clipped[log._id] === true || shots.length > 0;
	}

	// Rows crawled before the parser kept whole posts still have their 280-character preview.
	function words(log: DevlogDoc): string | null {
		return log.body ?? log.body_preview ?? null;
	}

	function devlogHref(log: DevlogDoc): string {
		return `https://stardance.hackclub.com/projects/${project._id}/devlogs/${log._id}`;
	}

	let started = $derived(project.created_at_estimate ?? project.first_seen);
</script>

<svelte:head>
	<title>{project.title} | Stardance Stats</title>
</svelte:head>

<div class="page">
	<a class="back" href="/projects">
		<Icon name="chevron-right" turn={2} size="0.9rem" /> Projects
	</a>

	<header class="hero">
		{#if project.banner_url && !bannerBroken}
			<img
				class="hero__banner"
				src={project.banner_url}
				alt=""
				aria-hidden="true"
				referrerpolicy="no-referrer"
				onerror={() => (bannerBroken = true)}
			/>
		{/if}

		<div class="hero__body">
			<div class="hero__who">
				<div class="hero__title">
					<h1>{project.title}</h1>
					{#if project.is_super_star}
						<span class="badge" title={project.super_star_note ?? 'Marked a Super Star by the Stardance team'}>
							<StarMark size={12} />
							Super Star
						</span>
					{/if}
					{#if project.is_hardware}
						<span class="badge badge--soft">hardware</span>
					{/if}
					{#if project.mission?.slug}
						<span class="badge badge--mission" title="On the {project.mission.name ?? project.mission.slug} mission">
							{project.mission.name ?? project.mission.slug}
						</span>
					{/if}
				</div>

				{#if project.owner_username}
					<a class="hero__owner" href="/people/{project.owner_username}">
						<Avatar src={project.owner_avatar_url} name={project.owner_username} size="1.5rem" />
						{project.owner_username}
					</a>
				{/if}

				{#if project.description}
					<p class="hero__desc">{project.description}</p>
				{/if}

				<p class="hero__meta">
					<span title="Read off its oldest devlog, or when we first saw it.">
						started {day(started)}
					</span>
					{#if (project.members?.length ?? 0) > 1}
						<span>{project.members?.length} members</span>
					{/if}
					{#if project.last_changed}
						<span title="When we last saw one of its numbers move.">
							moved {relative(project.last_changed)}
						</span>
					{/if}
					<span>crawled {relative(project.last_crawled)}</span>
				</p>

				<p class="hero__links">
					{#if project.demo_url}
						<a href={project.demo_url} target="_blank" rel="noreferrer noopener">
							<Icon name="globe" size="0.85rem" /> demo
						</a>
					{/if}
					{#if project.repo_url}
						<a href={project.repo_url} target="_blank" rel="noreferrer noopener">
							<GithubMark size={14} /> code
						</a>
					{/if}
				</p>
			</div>

			<a
				class="hero__out"
				href="https://stardance.hackclub.com/projects/{project._id}"
				target="_blank"
				rel="noreferrer noopener"
			>
				on Stardance
				<Icon name="external-link" size="0.85rem" />
			</a>
		</div>
	</header>

	<div class="figures-wrap">
		<Perch art="star-creature-blue" at="top-right" w="3.5rem" x="20%" y="-55%" turn="10deg" />
		<section class="figures">
			{#each TILES as tile (tile.key)}
				{@const moved = movement(tile.key)}
				{@const value = metricValue(tile, project)}
				<button
					class="figure"
					class:figure--on={selected === tile.key}
					style="--accent: {tile.accent}"
					onclick={() => (selected = tile.key)}
				>
					<span class="figure__label">{tile.label}</span>
					<span class="figure__value tabular" title={exactMetric(tile, value)}>
						{formatMetric(tile, value)}
					</span>
					<span class="figure__delta tabular" class:down={(moved?.delta ?? 0) < 0}>
						{!moved
							? ''
							: moved.delta === 0
								? 'flat'
								: `${signed(moved.delta)} · ${moved.days}d`}
					</span>
				</button>
			{/each}
		</section>
	</div>

	<section class="panel">
		<div class="panel__head">
			<h2 style="color: {active.accent}">{active.label}</h2>
			<div class="panel__controls">
				{#if span && span < range}
					<span class="muted">only {span}d observed</span>
				{/if}
				<RangePicker options={RANGES} bind:value={range} label="Chart range" />
			</div>
		</div>
		{#key `${selected}-${range}`}
			<div in:fade={{ duration: 160 }}>
				<Chart {points} color={active.accent} />
			</div>
		{/key}
	</section>

	<section class="work">
		<div class="work__head">
			<h2>Ships</h2>
			<span class="muted">
				{full(ships.length)} shipped · {full(data.ships?.stardust_total ?? 0)} Stardust rated
			</span>
		</div>

		{#if ships.length}
			<div class="table-wrap">
				<table class="table">
					<thead>
						<tr>
							<th>#</th>
							<th>Shipped</th>
							<th class="right">Hours</th>
							<th class="right">Multiplier</th>
							<th class="right">Payout</th>
							<th>Status</th>
						</tr>
					</thead>
					<tbody>
						{#each ships as ship (ship._id)}
							<tr>
								<td class="tabular">{ship.ship_number ?? '·'}</td>
								<td>{day(ship.shipped_at)}</td>
								<td class="right tabular">
									{ship.hours_at_ship == null ? '--' : ship.hours_at_ship.toFixed(1)}
								</td>
								<td class="right tabular">
									{ship.multiplier == null ? '--' : `${ship.multiplier.toFixed(2)}×`}
								</td>
								<td class="right tabular" class:pending={ship.payout == null}>{payout(ship)}</td>
								<td class="status">
									{ship.status ?? 'unknown'}
									{#if ship.mission_slug}
										<span class="badge badge--soft">{ship.mission_slug}</span>
									{/if}
								</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		{:else}
			<p class="muted">No ships on its timeline yet.</p>
		{/if}
	</section>

	<section class="work">
		<div class="work__head">
			<h2>Devlogs</h2>
			<div class="work__controls">
				<span class="muted">{full(logTotal)} crawled</span>
				<label class="picker">
					<span class="picker__label">Sort</span>
					<span class="picker__field">
						<select
							value={data.sort}
							onchange={(event) => jump(href({ sort: event.currentTarget.value, logs: null }))}
						>
							{#each ORDERS as option (option.key)}
								<option value={option.key}>{option.label}</option>
							{/each}
						</select>
						<span class="picker__chevron">
							<Icon name="chevron-right" turn={1} size="0.8rem" />
						</span>
					</span>
				</label>
			</div>
		</div>

		{#if devlogs.length}
			{#key `${data.sort}-${data.logPage}`}
				<ul class="logs" in:fade={{ duration: 160 }}>
					{#each devlogs as log (log._id)}
						{@const shots = attachments(log)}
						<li class="log" class:log--text={!shots.length}>
							<div class="log__head">
								<a
									class="log__link"
									href={devlogHref(log)}
									target="_blank"
									rel="noreferrer noopener"
									aria-label="Open this devlog on Stardance"
								>
									<Icon name="external-link" size="0.85rem" />
								</a>
								<span class="log__when">{day(log.posted_at)}</span>
								<span class="log__figures">
									<span title="Hours logged"><Icon name="clock" size="0.85rem" /> {hours(log.duration_seconds)}</span>
									<span title="Likes"><Icon name="heart" size="0.85rem" /> {compact(log.likes)}</span>
									<span title="Comments"><Icon name="message-circle" size="0.85rem" /> {compact(log.comments)}</span>
									<span title="Views"><Icon name="eye" size="0.85rem" /> {compact(log.views)}</span>
								</span>
							</div>
							{#if words(log)}
								<p
									class="log__body"
									class:log__body--faded={clipped[log._id]}
									use:clamped={log._id}
								>
									{words(log)}
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
				{logTotal ? 'No devlogs on this page. Try a newer one.' : 'No devlogs crawled for it yet.'}
			</p>
		{/if}

		{#if logTotal}
			<nav class="pager" aria-label="Devlog pages">
				{#if data.logPage > 1}
					<a class="pager__step" href={href({ logs: String(data.logPage - 1) })} data-sveltekit-noscroll>
						<Icon name="chevron-right" turn={2} size="0.9rem" /> newer
					</a>
				{:else}
					<span class="pager__step pager__step--off">
						<Icon name="chevron-right" turn={2} size="0.9rem" /> newer
					</span>
				{/if}

				<span class="pager__count tabular">
					{#if devlogs.length}
						{full(firstLog)}–{full(lastLog)} of {full(logTotal)}
					{:else}
						past the end of {full(logTotal)}
					{/if}
				</span>

				{#if moreLogs}
					<a class="pager__step" href={href({ logs: String(data.logPage + 1) })} data-sveltekit-noscroll>
						older <Icon name="chevron-right" size="0.9rem" />
					</a>
				{:else}
					<span class="pager__step pager__step--off">
						older <Icon name="chevron-right" size="0.9rem" />
					</span>
				{/if}
			</nav>
		{/if}
	</section>
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
					<a
						class="log__link"
						href={devlogHref(opened)}
						target="_blank"
						rel="noreferrer noopener"
						aria-label="Open this devlog on Stardance"
					>
						<Icon name="external-link" size="0.9rem" />
					</a>
					<span class="log__when">{day(opened.posted_at)}</span>
				</div>

				<span class="log__figures">
					<span title="Hours logged"><Icon name="clock" size="0.85rem" /> {hours(opened.duration_seconds)}</span>
					<span title="Likes"><Icon name="heart" size="0.85rem" /> {compact(opened.likes)}</span>
					<span title="Comments"><Icon name="message-circle" size="0.85rem" /> {compact(opened.comments)}</span>
					<span title="Views"><Icon name="eye" size="0.85rem" /> {compact(opened.views)}</span>
				</span>

				<button class="sheet__close" type="button" onclick={() => (opened = null)} aria-label="Close">
					<Icon name="close" size="0.9rem" />
				</button>
			</header>

			<div class="sheet__body">
				{#if words(opened)}
					<p class="sheet__text">{words(opened)}</p>
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

	.back {
		display: inline-flex;
		align-items: center;
		gap: var(--space-xs);
		margin-bottom: var(--space-m);
		font-size: var(--font-size-s);
		font-weight: 700;
		color: var(--color-set-1-fg-secondary);
	}

	.hero {
		position: relative;
		border: 1px solid var(--color-space-surface-faint);
		border-radius: var(--radius);
		overflow: hidden;
	}

	.hero__banner {
		position: absolute;
		inset: 0;
		width: 100%;
		height: 100%;
		object-fit: cover;
		opacity: 0.3;
		filter: saturate(0.7);
		/* Held back from the left, where the title and description sit. */
		mask-image: linear-gradient(to right, transparent 25%, black 80%);
	}

	.hero__body {
		position: relative;
		display: flex;
		align-items: flex-start;
		gap: var(--space-m);
		padding: var(--space-l);
		background: linear-gradient(to right, var(--color-space-bg) 30%, transparent);
	}

	.hero__who {
		flex: 1;
		min-width: 0;
		display: flex;
		flex-direction: column;
		gap: var(--space-xxs);
	}

	.hero__title {
		display: flex;
		align-items: center;
		gap: var(--space-s);
		flex-wrap: wrap;
	}

	.hero__title h1 {
		font-size: var(--font-size-xxl);
		overflow-wrap: anywhere;
	}

	.hero__owner {
		display: inline-flex;
		align-items: center;
		gap: var(--space-xs);
		align-self: flex-start;
		font-size: var(--font-size-s);
		font-weight: 700;
		color: var(--color-space-text-muted);
	}

	.hero__owner:hover {
		color: var(--color-brand-blue);
	}

	.hero__desc {
		max-width: 52rem;
		font-size: var(--font-size-s);
		color: var(--color-space-text-muted);
	}

	.hero__meta {
		display: flex;
		flex-wrap: wrap;
		gap: var(--space-xxs) var(--space-m);
		margin-top: var(--space-xxs);
		font-size: var(--font-size-xs);
		color: var(--color-set-1-fg-secondary);
	}

	.hero__meta span + span::before {
		content: '·';
		padding-right: var(--space-m);
	}

	.hero__links {
		display: flex;
		gap: var(--space-m);
		font-size: var(--font-size-xs);
	}

	.hero__links a {
		display: inline-flex;
		align-items: center;
		gap: var(--space-xxs);
		color: var(--color-set-1-fg-secondary);
	}

	.hero__links a:hover {
		color: var(--color-brand-blue);
	}

	.hero__out {
		flex: none;
		display: inline-flex;
		align-items: center;
		gap: var(--space-xs);
		padding: var(--space-xs) var(--space-m);
		border-radius: var(--radius-pill);
		background: var(--color-brand-highlight);
		font-size: var(--font-size-s);
		font-weight: 700;
		color: var(--color-set-1-bg);
		white-space: nowrap;
	}

	.hero__out:hover {
		color: var(--color-set-1-bg);
		background: var(--color-brand-highlight-secondary);
	}

	/* Stardance paints this pill with a mesh gradient; these four stops stand in for it. */
	.badge {
		display: inline-flex;
		align-items: center;
		gap: var(--space-xxs);
		padding: 2px var(--space-s);
		border-radius: var(--radius-pill);
		background:
			radial-gradient(120% 150% at 0% 0%, var(--color-brand-yellow) 0%, transparent 55%),
			radial-gradient(120% 150% at 100% 0%, var(--color-brand-salmon) 0%, transparent 55%),
			radial-gradient(130% 160% at 100% 100%, var(--color-brand-blue) 0%, transparent 60%),
			var(--color-brand-lilac);
		font-size: var(--font-size-xs);
		font-weight: 700;
		color: var(--color-set-1-bg);
		white-space: nowrap;
	}

	.badge--soft {
		padding: 1px var(--space-xs);
		background: var(--color-overlay-light-soft);
		font-weight: 400;
		color: var(--color-set-1-fg-secondary);
	}

	.badge--mission {
		/* Text alone sits shorter than the Super Star badge beside it, which carries a 12px mark. */
		padding-block: 4px;
		background: var(--color-space-accent-soft);
		color: var(--color-space-accent);
	}

	.figures-wrap {
		position: relative;
		margin: var(--space-l) 0;
	}

	.figures {
		display: grid;
		grid-template-columns: repeat(4, 1fr);
		gap: 1px;
		background: var(--color-space-surface-faint);
		border: 1px solid var(--color-space-surface-faint);
		border-radius: var(--radius);
		overflow: hidden;
	}

	.figure {
		display: flex;
		flex-direction: column;
		gap: 2px;
		padding: var(--space-m);
		background: var(--color-space-bg);
		border: none;
		text-align: left;
		cursor: pointer;
		transition: background 0.15s ease;
	}

	.figure:hover,
	.figure--on {
		background: var(--color-overlay-light-soft);
	}

	.figure__label {
		font-size: var(--font-size-xs);
		text-transform: uppercase;
		letter-spacing: 0.08em;
		color: var(--color-set-1-fg-secondary);
	}

	.figure__value {
		font-size: var(--font-size-xxl);
		font-weight: 700;
		line-height: 1.15;
	}

	.figure--on .figure__value {
		color: var(--accent);
	}

	.figure__delta {
		font-size: var(--font-size-xs);
		color: var(--color-brand-mint);
		min-height: 1em;
	}

	.figure__delta.down {
		color: var(--color-brand-salmon);
	}

	.panel {
		padding: var(--space-l);
		border: 1px solid var(--color-space-surface-faint);
		border-radius: var(--radius);
	}

	.panel__head {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: var(--space-m);
		margin-bottom: var(--space-m);
	}

	.panel__head h2 {
		font-size: var(--font-size-l);
	}

	.panel__controls {
		display: flex;
		align-items: center;
		gap: var(--space-s);
	}

	.panel__head span {
		font-size: var(--font-size-xs);
	}

	.work {
		margin-top: var(--space-xl);
	}

	.work__head {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: var(--space-m);
		flex-wrap: wrap;
		padding-bottom: var(--space-s);
		border-bottom: 1px solid var(--color-space-surface-faint);
	}

	.work__head h2 {
		font-size: var(--font-size-l);
	}

	.work__head span {
		font-size: var(--font-size-xs);
	}

	.work__controls {
		display: flex;
		align-items: center;
		gap: var(--space-m);
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

	.picker__chevron {
		position: absolute;
		right: var(--space-s);
		display: inline-flex;
		color: var(--color-set-1-fg-secondary);
		pointer-events: none;
	}

	.table-wrap {
		margin-top: var(--space-m);
		overflow-x: auto;
	}

	.table {
		width: 100%;
		border-collapse: collapse;
		font-size: var(--font-size-s);
		white-space: nowrap;
	}

	.table th {
		padding: var(--space-xs) var(--space-s);
		text-align: left;
		font-size: var(--font-size-xs);
		text-transform: uppercase;
		letter-spacing: 0.08em;
		font-weight: 400;
		color: var(--color-set-1-fg-secondary);
		border-bottom: 1px solid var(--color-space-surface-faint);
	}

	.table td {
		padding: var(--space-xs) var(--space-s);
		border-bottom: 1px solid var(--color-space-surface-faint);
	}

	.table tbody tr:hover {
		background: var(--color-overlay-light-soft);
	}

	/* Scoped past `.table th`, which would otherwise keep a numeric heading left of its column. */
	.table .right {
		text-align: right;
	}

	.pending {
		color: var(--color-set-1-fg-secondary);
	}

	.status {
		display: flex;
		align-items: center;
		gap: var(--space-xs);
	}

	.logs {
		margin-top: var(--space-m);
		list-style: none;
		display: grid;
		grid-template-columns: repeat(2, minmax(0, 1fr));
		gap: var(--space-s);
	}

	/* One height for every card, so text length clips rather than stretching the grid. */
	.log {
		display: flex;
		flex-direction: column;
		height: 23rem;
		padding: var(--space-s) var(--space-m);
		border: 1px solid var(--color-space-surface-faint);
		border-radius: var(--radius);
		overflow: hidden;
	}

	.log__head {
		display: flex;
		align-items: baseline;
		gap: var(--space-s) var(--space-m);
		flex-wrap: wrap;
	}

	.log__link {
		display: inline-flex;
		color: var(--color-set-1-fg-secondary);
	}

	.log__link:hover {
		color: var(--color-brand-blue);
	}

	.log__when {
		font-size: var(--font-size-xs);
		color: var(--color-set-1-fg-secondary);
	}

	.log__figures {
		display: flex;
		gap: var(--space-m);
		margin-left: auto;
		font-size: var(--font-size-xs);
		color: var(--color-set-1-fg-secondary);
	}

	.log__figures span {
		display: inline-flex;
		align-items: center;
		gap: var(--space-xxs);
	}

	.shots {
		display: grid;
		margin-top: var(--space-s);
		flex: 1;
		min-height: 0;
	}

	.log__body {
		margin-top: var(--space-xxs);
		max-width: 62rem;
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

	.log__body--faded {
		-webkit-mask-image: linear-gradient(to bottom, #000 55%, transparent 98%);
		mask-image: linear-gradient(to bottom, #000 55%, transparent 98%);
	}

	.empty {
		padding: var(--space-xl) 0;
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

	.sheet__close {
		grid-column: 3;
		grid-row: 1;
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

	@media (max-width: 900px) {
		.logs {
			grid-template-columns: minmax(0, 1fr);
		}
	}

	@media (max-width: 640px) {
		.panel {
			padding: var(--space-m);
		}

		.panel__head,
		.panel__controls {
			flex-wrap: wrap;
		}

		.panel__controls {
			width: 100%;
			justify-content: space-between;
		}

		.log__figures {
			margin-left: 0;
			width: 100%;
		}

		.sheet__head {
			grid-template-columns: 1fr auto;
		}

		.sheet__head .log__figures {
			grid-column: 1 / -1;
		}
	}

	@media (max-width: 720px) {
		.figures {
			grid-template-columns: repeat(2, 1fr);
		}

		.hero__body {
			flex-wrap: wrap;
		}

		.hero__who {
			flex-basis: 100%;
		}

		.hero__out {
			order: 3;
		}

		.hero__banner {
			opacity: 0.15;
		}
	}
</style>
