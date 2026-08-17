<script lang="ts">
	import { fade } from 'svelte/transition';
	import Avatar from '$components/Avatar.svelte';
	import Perch from '$components/Perch.svelte';
	import GithubMark from '$components/GithubMark.svelte';
	import Icon from '$components/Icon.svelte';
	import StarMark from '$components/StarMark.svelte';
	import Chart from '$components/Chart.svelte';
	import RangePicker from '$components/RangePicker.svelte';
	import { PROFILE_TILES, metric, metricValue, formatMetric, exactMetric } from '$lib/metrics';
	import { compact, full, relative, signed } from '$lib/format';
	import type { ProjectSummary } from '$lib/types';
	import type { PageData } from './$types';

	let { data }: { data: PageData } = $props();

	const RANGES = [1, 3, 7, 14, 30];
	const TILES = PROFILE_TILES.map((key) => metric(key));

	let selected = $state('ship_stardust');
	let range = $state(7);

	let user = $derived(data.user);
	let series = $derived(data.history?.series ?? {});
	// The API hands them back by Stardust; newest first is the more useful order here.
	let projects = $derived(
		[...(data.projects?.items ?? [])].sort((a, b) => started(b) - started(a))
	);
	let active = $derived(metric(selected));
	let points = $derived(windowed(selected));

	function observedPoints(key: string) {
		return (series[key] ?? []).filter((p) => Number.isFinite(p.v));
	}

	// Buckets are hourly, so the window is measured off the newest point rather than counted out.
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

	function day(iso: string | null | undefined): string {
		if (!iso) return 'unknown';
		const date = new Date(iso);
		return Number.isNaN(date.getTime())
			? 'unknown'
			: date.toLocaleDateString('en', { month: 'short', day: 'numeric', year: 'numeric' });
	}

	// created_at_estimate is read off the oldest devlog, so it falls back to when we first saw it.
	function started(project: ProjectSummary): number {
		const stamp = project.created_at_estimate ?? project.first_seen;
		const parsed = stamp ? Date.parse(stamp) : NaN;
		return Number.isNaN(parsed) ? 0 : parsed;
	}

	function stat(project: { stats: Record<string, number | null> }, key: string): number | null {
		const value = project.stats?.[key];
		return typeof value === 'number' ? value : null;
	}

	// Stardust is a painted jar, not a line glyph, so it needs the larger size.
	const CARD_STATS = [
		{ key: 'stardust_total', icon: 'stardust.png', label: 'Stardust', size: '1.25rem' },
		{ key: 'total_hours', icon: 'clock', label: 'Hours', size: '0.9rem' },
		{ key: 'devlogs', icon: 'notepad-text', label: 'Devlogs', size: '0.9rem' },
		{ key: 'likes', icon: 'heart', label: 'Likes', size: '0.9rem' },
		{ key: 'views', icon: 'eye', label: 'Views', size: '0.9rem' }
	];

	// Banners are hotlinked, so one that will not load drops out of its card.
	let brokenBanners = $state<number[]>([]);
</script>

<svelte:head>
	<title>{user.username} | Stardance Stats</title>
</svelte:head>

<div class="page">
	<a class="back" href="/people">
		<Icon name="chevron-right" turn={2} size="0.9rem" /> People
	</a>

	<header class="hero">
		{#if user.banner_url}
			<img class="hero__banner" src={user.banner_url} alt="" aria-hidden="true" />
		{/if}

		<div class="hero__body">
			<Avatar src={user.avatar_url} name={user.username} size="4.5rem" />

			<div class="hero__who">
				<h1>{user.username}</h1>
				{#if user.previous_usernames?.length}
					<p class="hero__former muted">previously {user.previous_usernames.join(', ')}</p>
				{/if}
				{#if user.bio}
					<p class="hero__bio">{user.bio}</p>
				{/if}
				<p class="hero__meta">
					<span>joined {day(user.joined_at)}</span>
					<span>{full(user.stats?.projects ?? projects.length)} projects</span>
					{#if user.stats?.following != null}
						<span>{full(user.stats.following)} following</span>
					{/if}
					<span>crawled {relative(user.last_crawled)}</span>
				</p>
			</div>

			<a
				class="hero__out"
				href="https://stardance.hackclub.com/@{user.username}"
				target="_blank"
				rel="noreferrer noopener"
			>
				on Stardance
				<Icon name="external-link" size="0.85rem" />
			</a>
		</div>
	</header>

	<div class="figures-wrap">
		<Perch art="guest_star_2" at="bottom-right" w="4rem" x="-15%" y="80%" turn="6deg" />
		<section class="figures">
			{#each TILES as tile (tile.key)}
				{@const moved = movement(tile.key)}
				{@const value = metricValue(tile, user)}
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
			<h2>Projects</h2>
			<span class="muted">{full(projects.length)} crawled</span>
		</div>

		{#if projects.length}
			<ul class="cards">
				{#each projects as project (project._id)}
					<li class="card2">
						{#if project.banner_url && !brokenBanners.includes(project._id)}
							<img
								class="card2__banner"
								src={project.banner_url}
								alt=""
								loading="lazy"
								decoding="async"
								referrerpolicy="no-referrer"
								onerror={() => (brokenBanners = [...brokenBanners, project._id])}
							/>
						{/if}

						<div class="card2__body">
							<div class="card2__top">
								<a class="card2__title" href="/projects/{project._id}">
									{project.title}
								</a>
								{#if project.is_super_star}
									<span class="badge" title="Marked a Super Star by the Stardance team">
										<StarMark size={12} />
										Super Star
									</span>
								{/if}
								{#if project.owner_id !== user._id}
									<span class="badge badge--soft">member</span>
								{/if}
							</div>

							<ul class="card2__stats">
								{#each CARD_STATS as item (item.key)}
									<li title="{item.label}: {full(stat(project, item.key))}">
										<Icon name={item.icon} size={item.size} label={item.label} />
										<span class="tabular">{compact(stat(project, item.key))}</span>
									</li>
								{/each}
							</ul>

							{#if project.description}
								<p class="card2__desc">{project.description}</p>
							{/if}

							<div class="card2__foot">
								{#if project.last_changed}
									<span
										class="card2__when"
										title="When we last saw one of its numbers move, not the project's own timestamp."
									>
										Last updated {relative(project.last_changed)}
									</span>
								{/if}

								{#if project.demo_url || project.repo_url}
									<p class="card2__links">
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
								{/if}
							</div>
						</div>
					</li>
				{/each}
			</ul>
		{:else}
			<p class="muted">No projects crawled for them yet.</p>
		{/if}
	</section>
</div>

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

	/* The banner is decoration, so it stays behind a wash rather than competing with the text. */
	.hero__banner {
		position: absolute;
		inset: 0;
		width: 100%;
		height: 100%;
		object-fit: cover;
		opacity: 0.3;
		filter: saturate(0.7);
		/* Held back from the left so it never sits under the name and bio. */
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

	.hero__who h1 {
		font-size: var(--font-size-xxl);
		overflow-wrap: anywhere;
	}

	.hero__former {
		font-size: var(--font-size-xs);
	}

	.hero__bio {
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
		padding-bottom: var(--space-s);
		border-bottom: 1px solid var(--color-space-surface-faint);
	}

	.work__head h2 {
		font-size: var(--font-size-l);
	}

	.work__head span {
		font-size: var(--font-size-xs);
	}

	.cards {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(20rem, 1fr));
		gap: var(--space-s);
		margin-top: var(--space-m);
		list-style: none;
	}

	.card2 {
		display: flex;
		flex-direction: column;
		border: 1px solid var(--color-space-surface-faint);
		border-radius: var(--radius);
		overflow: hidden;
	}

	.card2__banner {
		width: 100%;
		aspect-ratio: 16 / 9;
		object-fit: cover;
		display: block;
		background: var(--color-set-2-bg);
		border-bottom: 1px solid var(--color-space-surface-faint);
	}

	.card2__body {
		flex: 1;
		display: flex;
		flex-direction: column;
		gap: var(--space-xs);
		padding: var(--space-m);
	}

	.card2__top {
		display: flex;
		align-items: center;
		gap: var(--space-xs);
		flex-wrap: wrap;
	}

	.card2__title {
		font-weight: 700;
		overflow-wrap: anywhere;
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

	.card2__desc {
		font-size: var(--font-size-s);
		color: var(--color-space-text-muted);
	}

	.card2__stats {
		display: flex;
		flex-wrap: wrap;
		gap: var(--space-s) var(--space-m);
		list-style: none;
	}

	.card2__stats li {
		display: inline-flex;
		align-items: center;
		gap: var(--space-xxs);
		font-size: var(--font-size-s);
		font-weight: 700;
		color: var(--color-set-1-fg-secondary);
	}

	.card2__foot {
		display: flex;
		align-items: center;
		justify-content: space-between;
		flex-wrap: wrap;
		gap: var(--space-xs) var(--space-m);
		/* Pinned down so cards of unequal description length still line up. */
		margin-top: auto;
		padding-top: var(--space-xs);
	}

	.card2__when {
		font-size: var(--font-size-xs);
		color: var(--color-space-text-muted);
	}

	.card2__links {
		display: flex;
		gap: var(--space-m);
		font-size: var(--font-size-xs);
	}

	.card2__links a {
		display: inline-flex;
		align-items: center;
		gap: var(--space-xxs);
		color: var(--color-set-1-fg-secondary);
	}

	.card2__links a:hover {
		color: var(--color-brand-blue);
	}

	@media (max-width: 640px) {
		.panel {
			padding: var(--space-m);
		}

		/* The range picker has a floor of its own, so give it a line to itself. */
		.panel__head,
		.panel__controls {
			flex-wrap: wrap;
		}

		.panel__controls {
			width: 100%;
			justify-content: space-between;
		}
	}

	@media (max-width: 720px) {
		.figures {
			grid-template-columns: repeat(2, 1fr);
		}

		.hero__body {
			flex-wrap: wrap;
		}

		.hero__out {
			order: 3;
		}
	}
</style>
