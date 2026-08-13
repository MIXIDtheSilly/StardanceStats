import { statsOrNull } from '$lib/server/stats';
import { DEFAULT_METRIC, isMetric } from '$lib/metrics';
import type { LeaderboardResponse } from '$lib/types';
import type { PageServerLoad } from './$types';

const PAGE_SIZES = [25, 50, 100, 200];
const DEFAULT_SIZE = 50;

export const load: PageServerLoad = async ({ fetch, url }) => {
	const asked = url.searchParams.get('metric');
	const metric = isMetric(asked) ? asked! : DEFAULT_METRIC;
	const page = Math.max(1, Math.floor(Number(url.searchParams.get('page')) || 1));
	const wanted = Math.floor(Number(url.searchParams.get('size')) || DEFAULT_SIZE);
	const size = PAGE_SIZES.includes(wanted) ? wanted : DEFAULT_SIZE;

	const board = await statsOrNull<LeaderboardResponse>(fetch, '/leaderboard', {
		metric,
		limit: size,
		offset: (page - 1) * size
	});

	return { board, metric, page, size, sizes: PAGE_SIZES };
};
