import { compact, full } from './format';

export type MetricKind = 'count' | 'hours' | 'rate' | 'multiplier';

export interface MetricDef {
	key: string;
	label: string;
	group: string;
	accent: string;
	kind: MetricKind;
	/** Which half of the user document carries the current value. */
	field: 'totals' | 'stats';
	/** totals.* we add up from crawled rows; stats.* is the number their profile prints. */
	source: 'computed' | 'profile';
	/** Only snapshotted metrics can be drawn over time. */
	chartable: boolean;
	blurb: string;
}

export const PEOPLE_METRICS: MetricDef[] = [
	{
		key: 'ship_stardust',
		label: 'Stardust',
		group: 'Stardust',
		accent: 'var(--color-brand-yellow)',
		kind: 'count',
		field: 'totals',
		source: 'computed',
		chartable: true,
		blurb:
			'Paid out for ships we have crawled. Achievements, missions and manual grants are not public, so this is a floor.'
	},
	{
		key: 'estimated_total_stardust',
		label: 'Est. total',
		group: 'Stardust',
		accent: 'var(--color-brand-cream)',
		kind: 'count',
		field: 'totals',
		source: 'computed',
		chartable: false,
		blurb: 'Ship payouts plus what their unpaid hours would earn at the rate they usually draw.'
	},
	{
		key: 'stardust_per_paid_hour',
		label: 'Per paid hour',
		group: 'Stardust',
		accent: 'var(--color-brand-orange)',
		kind: 'rate',
		field: 'totals',
		source: 'computed',
		chartable: false,
		blurb: 'Stardust earned for every hour a ship was actually paid for.'
	},
	{
		key: 'best_multiplier',
		label: 'Best multiplier',
		group: 'Stardust',
		accent: 'var(--color-brand-peach)',
		kind: 'multiplier',
		field: 'totals',
		source: 'computed',
		chartable: true,
		blurb: 'The highest payout multiplier any one of their ships has drawn.'
	},
	{
		key: 'hours',
		label: 'Hours',
		group: 'Time',
		accent: 'var(--color-brand-peach)',
		kind: 'hours',
		field: 'totals',
		source: 'computed',
		chartable: true,
		blurb: 'Every hour logged across the projects we hold for them.'
	},
	{
		key: 'shipped_hours',
		label: 'Shipped hours',
		group: 'Time',
		accent: 'var(--color-brand-orange)',
		kind: 'hours',
		field: 'totals',
		source: 'computed',
		chartable: true,
		blurb: 'Hours belonging to a project that has shipped at least once.'
	},
	{
		key: 'paid_hours',
		label: 'Paid hours',
		group: 'Time',
		accent: 'var(--color-brand-cream)',
		kind: 'hours',
		field: 'totals',
		source: 'computed',
		chartable: true,
		blurb: 'Hours a ship has been paid for. The rest are still waiting on a review.'
	},
	{
		key: 'projects',
		label: 'Projects',
		group: 'Output',
		accent: 'var(--color-brand-blue)',
		kind: 'count',
		field: 'stats',
		source: 'profile',
		chartable: true,
		blurb: 'Projects their profile lists.'
	},
	{
		key: 'devlogs',
		label: 'Devlogs',
		group: 'Output',
		accent: 'var(--color-brand-lilac)',
		kind: 'count',
		field: 'stats',
		source: 'profile',
		chartable: true,
		blurb: 'Devlogs their profile counts.'
	},
	{
		key: 'ships',
		label: 'Ships',
		group: 'Output',
		accent: 'var(--color-brand-salmon)',
		kind: 'count',
		field: 'stats',
		source: 'profile',
		chartable: true,
		blurb: 'Ships their profile counts, accepted or not yet reviewed.'
	},
	{
		key: 'likes_received',
		label: 'Likes',
		group: 'Reception',
		accent: 'var(--color-brand-salmon)',
		kind: 'count',
		field: 'totals',
		source: 'computed',
		chartable: true,
		blurb: 'Likes their devlogs have collected.'
	},
	{
		key: 'views_received',
		label: 'Views',
		group: 'Reception',
		accent: 'var(--color-brand-mint)',
		kind: 'count',
		field: 'totals',
		source: 'computed',
		chartable: true,
		blurb: 'Views their devlogs have collected.'
	},
	{
		key: 'followers',
		label: 'Followers',
		group: 'Reception',
		accent: 'var(--color-brand-mint)',
		kind: 'count',
		field: 'stats',
		source: 'profile',
		chartable: true,
		blurb: 'People following them.'
	},
	{
		key: 'following',
		label: 'Following',
		group: 'Reception',
		accent: 'var(--color-brand-lilac)',
		kind: 'count',
		field: 'stats',
		source: 'profile',
		chartable: true,
		blurb: 'People they follow.'
	},
	{
		key: 'comments_received',
		label: 'Comments received',
		group: 'Comments',
		accent: 'var(--color-brand-blue)',
		kind: 'count',
		field: 'totals',
		source: 'computed',
		chartable: true,
		blurb: 'Comments other people have left under their devlogs.'
	},
	{
		key: 'comments_sent',
		label: 'Comments written',
		group: 'Comments',
		accent: 'var(--color-brand-mint)',
		kind: 'count',
		field: 'totals',
		source: 'computed',
		chartable: true,
		blurb:
			'Comments they have written, from the threads we have read. This trails the true number until the thread queue drains.'
	}
];

export const METRIC_GROUPS = ['Stardust', 'Time', 'Output', 'Reception', 'Comments'];

export const DEFAULT_METRIC = 'ship_stardust';

/** Tiles on a profile, in reading order. Every one of these is chartable. */
export const PROFILE_TILES = [
	'ship_stardust',
	'hours',
	'ships',
	'devlogs',
	'likes_received',
	'views_received',
	'followers',
	'comments_received'
];

const BY_KEY = new Map(PEOPLE_METRICS.map((m) => [m.key, m]));

export function metric(key: string | null | undefined): MetricDef {
	return BY_KEY.get(key ?? '') ?? BY_KEY.get(DEFAULT_METRIC)!;
}

export function isMetric(key: string | null | undefined): boolean {
	return BY_KEY.has(key ?? '');
}

export function metricValue(def: MetricDef, user: UserLike): number | null {
	const bag = def.field === 'stats' ? user.stats : user.totals;
	const value = bag?.[def.key];
	return typeof value === 'number' ? value : null;
}

interface UserLike {
	stats?: Record<string, number | null> | null;
	totals?: Record<string, number | null> | null;
}

export function formatMetric(def: MetricDef, value: number | null | undefined): string {
	if (value === null || value === undefined || !Number.isFinite(value)) return '--';
	switch (def.kind) {
		case 'hours':
			return Math.abs(value) < 10_000 ? value.toFixed(1) : compact(value);
		case 'rate':
			return value.toFixed(1);
		case 'multiplier':
			return `${value.toFixed(2)}×`;
		default:
			return compact(value);
	}
}

/** The unrounded reading, for a title attribute. */
export function exactMetric(def: MetricDef, value: number | null | undefined): string {
	if (value === null || value === undefined || !Number.isFinite(value)) return 'not known';
	return def.kind === 'count' ? full(value) : String(value);
}
