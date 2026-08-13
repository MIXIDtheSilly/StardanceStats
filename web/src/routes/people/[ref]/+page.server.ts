import { error } from '@sveltejs/kit';
import { stats, statsOrNull, StatsError } from '$lib/server/stats';
import { PROFILE_TILES } from '$lib/metrics';
import type { Series, UserDoc, UserProjectsResponse } from '$lib/types';
import type { PageServerLoad } from './$types';

const WINDOW_DAYS = 30;

export const load: PageServerLoad = async ({ fetch, params }) => {
	let user: UserDoc;
	try {
		user = await stats<UserDoc>(fetch, `/users/${encodeURIComponent(params.ref)}`);
	} catch (cause) {
		// Only a handle we do not hold is a 404; a silent API is not.
		if (cause instanceof StatsError && cause.status === 404) {
			error(404, `Nobody called ${params.ref} is tracked here`);
		}
		error(503, 'the stats API did not answer');
	}

	const start = new Date(Date.now() - WINDOW_DAYS * 86_400_000).toISOString();

	// The id resolves straight away, so the follow-ups skip the handle lookup.
	const [history, projects] = await Promise.all([
		statsOrNull<Series>(fetch, `/users/${user._id}/history`, {
			metrics: PROFILE_TILES.join(','),
			interval: '1h',
			fill: 'locf',
			start
		}),
		statsOrNull<UserProjectsResponse>(fetch, `/users/${user._id}/projects`)
	]);

	return { user, history, projects, windowDays: WINDOW_DAYS };
};
