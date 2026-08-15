import { error } from '@sveltejs/kit';
import { stats, statsOrNull, StatsError } from '$lib/server/stats';
import { PROJECT_TILES } from '$lib/metrics';
import type {
	ProjectDevlogsResponse,
	ProjectDoc,
	ProjectShipsResponse,
	Series
} from '$lib/types';
import type { PageServerLoad } from './$types';

const WINDOW_DAYS = 30;
/** Six rows of the two-column grid. */
const DEVLOG_SIZE = 12;
const DEVLOG_SORTS = ['posted_at', 'duration_seconds', 'likes', 'comments', 'views'];

export const load: PageServerLoad = async ({ fetch, url, params }) => {
	if (!/^\d+$/.test(params.id)) error(404, 'a project is identified by its number');

	const asked = url.searchParams.get('sort');
	const sort = asked && DEVLOG_SORTS.includes(asked) ? asked : DEVLOG_SORTS[0];
	const logPage = Math.max(1, Math.floor(Number(url.searchParams.get('logs')) || 1));

	let project: ProjectDoc;
	try {
		project = await stats<ProjectDoc>(fetch, `/projects/${params.id}`);
	} catch (cause) {
		// Only a project we do not hold is a 404; a silent API is not.
		if (cause instanceof StatsError && cause.status === 404) {
			error(404, `Project ${params.id} is not tracked here`);
		}
		error(503, 'the stats API did not answer');
	}

	const start = new Date(Date.now() - WINDOW_DAYS * 86_400_000).toISOString();

	const [history, ships, devlogs] = await Promise.all([
		statsOrNull<Series>(fetch, `/projects/${project._id}/history`, {
			metrics: PROJECT_TILES.join(','),
			interval: '1h',
			fill: 'locf',
			start
		}),
		statsOrNull<ProjectShipsResponse>(fetch, `/projects/${project._id}/ships`),
		statsOrNull<ProjectDevlogsResponse>(fetch, `/projects/${project._id}/devlogs`, {
			limit: DEVLOG_SIZE,
			offset: (logPage - 1) * DEVLOG_SIZE,
			sort
		})
	]);

	return {
		project,
		history,
		ships,
		devlogs,
		windowDays: WINDOW_DAYS,
		sort,
		logPage,
		logSize: DEVLOG_SIZE
	};
};
