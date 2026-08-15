import { statsOrNull } from '$lib/server/stats';
import { DEFAULT_PROJECT_METRIC, isProjectMetric } from '$lib/metrics';
import type { ProjectBoardResponse } from '$lib/types';
import type { PageServerLoad } from './$types';

const PAGE_SIZES = [25, 50, 100, 200];
const DEFAULT_SIZE = 50;

export const load: PageServerLoad = async ({ fetch, url }) => {
	const asked = url.searchParams.get('metric');
	const metric = isProjectMetric(asked) ? asked! : DEFAULT_PROJECT_METRIC;
	const page = Math.max(1, Math.floor(Number(url.searchParams.get('page')) || 1));
	const wanted = Math.floor(Number(url.searchParams.get('size')) || DEFAULT_SIZE);
	const size = PAGE_SIZES.includes(wanted) ? wanted : DEFAULT_SIZE;
	const superStar = url.searchParams.get('super') === '1';
	const hardware = url.searchParams.get('hardware') === '1';

	const board = await statsOrNull<ProjectBoardResponse>(fetch, '/projects', {
		metric,
		limit: size,
		offset: (page - 1) * size,
		// Omitted rather than sent false, so the query string stays the plain case.
		super_star: superStar || undefined,
		hardware: hardware || undefined
	});

	return { board, metric, page, size, sizes: PAGE_SIZES, superStar, hardware };
};
