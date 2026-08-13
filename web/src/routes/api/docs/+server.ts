import { redirect } from '@sveltejs/kit';
import { statsUrl } from '$lib/server/stats';
import type { RequestHandler } from './$types';

export const GET: RequestHandler = () => {
	const api = new URL(statsUrl('/'));
	// The OpenAPI page sits at the service root, one level above the /v1 prefix.
	api.pathname = api.pathname.replace(/\/v1\/?$/, '/') + 'docs';
	redirect(307, api.toString().replace(/\/\/docs$/, '/docs'));
};
