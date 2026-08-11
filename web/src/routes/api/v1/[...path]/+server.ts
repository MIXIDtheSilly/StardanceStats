import { error } from '@sveltejs/kit';
import { statsUrl } from '$lib/server/stats';
import type { RequestHandler } from './$types';

/** Read-only passthrough so browser code never needs the API's address or CORS. */
export const GET: RequestHandler = async ({ params, url, fetch }) => {
	if (!params.path) throw error(404, 'no endpoint given');

	const query = Object.fromEntries(url.searchParams);
	const upstream = await fetch(statsUrl(`/${params.path}`, query), {
		headers: { accept: 'application/json' }
	});

	return new Response(upstream.body, {
		status: upstream.status,
		headers: {
			'content-type': upstream.headers.get('content-type') ?? 'application/json',
			'cache-control': upstream.headers.get('cache-control') ?? 'public, max-age=60'
		}
	});
};
