import { statsOrNull } from '$lib/server/stats';
import type { GlobalResponse, Series } from '$lib/types';
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

	const [totals, history] = await Promise.all([
		statsOrNull<GlobalResponse>(fetch, '/global'),
		// The collector rolls up hourly, so 1h is one bucket per update: the finest the API offers.
		statsOrNull<Series>(fetch, '/global/history', {
			metrics: SPARK_METRICS.join(','),
			interval: '1h',
			fill: 'locf',
			start
		})
	]);

	return { totals, history, windowDays: WINDOW_DAYS };
};
