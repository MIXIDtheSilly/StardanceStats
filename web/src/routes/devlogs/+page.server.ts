import { statsOrNull } from '$lib/server/stats';
import type { DevlogFeedResponse } from '$lib/types';
import type { PageServerLoad } from './$types';

const SORTS = [
	'posted_at',
	'duration_seconds',
	'likes',
	'comments',
	'views',
	'reposts',
	'relevance'
];
const WINDOWS = [1, 3, 7, 14, 30];
const PAGE_SIZES = [12, 24, 48];
const DEFAULT_SIZE = 24;
/** The API refuses anything shorter. */
const MIN_QUERY = 2;

export const load: PageServerLoad = async ({ fetch, url }) => {
	const typed = (url.searchParams.get('q') ?? '').trim().slice(0, 96);
	const q = typed.length >= MIN_QUERY ? typed : null;

	// One control, two parameters: "posted_at:asc" is oldest first.
	const [asked, way] = (url.searchParams.get('sort') ?? '').split(':');
	const known = SORTS.includes(asked) ? asked : null;
	// The API refuses relevance without a search to rank.
	const sort = known === 'relevance' && !q ? 'posted_at' : (known ?? (q ? 'relevance' : 'posted_at'));
	const order = way === 'asc' && sort !== 'relevance' ? 'asc' : 'desc';

	const page = Math.max(1, Math.floor(Number(url.searchParams.get('page')) || 1));
	const wanted = Math.floor(Number(url.searchParams.get('size')) || DEFAULT_SIZE);
	const size = PAGE_SIZES.includes(wanted) ? wanted : DEFAULT_SIZE;
	const askedDays = Math.floor(Number(url.searchParams.get('days')) || 0);
	const days = WINDOWS.includes(askedDays) ? askedDays : null;

	const feed = await statsOrNull<DevlogFeedResponse>(fetch, '/devlogs', {
		sort,
		limit: size,
		offset: (page - 1) * size,
		q: q ?? undefined,
		order: order === 'asc' ? 'asc' : undefined,
		days: days ?? undefined
	});

	return { feed, q: q ?? '', sort, order, page, size, sizes: PAGE_SIZES, days, windows: WINDOWS };
};
