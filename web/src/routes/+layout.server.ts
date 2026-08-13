import { statsOrNull } from '$lib/server/stats';
import type { HealthResponse } from '$lib/types';
import type { LayoutServerLoad } from './$types';

export const load: LayoutServerLoad = async ({ fetch, setHeaders }) => {
	const health = await statsOrNull<HealthResponse>(fetch, '/health');
	setHeaders({ 'cache-control': 'public, max-age=60' });
	return { lastCrawl: health?.last_crawl ?? null };
};
