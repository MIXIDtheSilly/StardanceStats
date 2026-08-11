import { statsOrNull } from '$lib/server/stats';
import type { GlobalResponse, HealthResponse, MetaResponse, Series } from '$lib/types';
import type { PageServerLoad } from './$types';

const WINDOW_DAYS = 30;

const SPARK_METRICS = [
	'users',
	'projects',
	'devlogs',
	'ships',
	'hours',
	'stardust_paid',
	'likes',
	'views'
];

export const load: PageServerLoad = async ({ fetch }) => {
	const start = new Date(Date.now() - WINDOW_DAYS * 86_400_000).toISOString();

	const [totals, history, meta, health] = await Promise.all([
		statsOrNull<GlobalResponse>(fetch, '/global'),
		statsOrNull<Series>(fetch, '/global/history', {
			metrics: SPARK_METRICS.join(','),
			interval: '1d',
			fill: 'locf',
			start
		}),
		statsOrNull<MetaResponse>(fetch, '/meta'),
		statsOrNull<HealthResponse>(fetch, '/health')
	]);

	return { totals, history, meta, health, windowDays: WINDOW_DAYS };
};
