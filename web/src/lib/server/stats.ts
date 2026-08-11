import { env } from '$env/dynamic/private';

export type QueryValue = string | number | boolean | null | undefined;

function base(): string {
	return (env.STATS_API ?? 'http://127.0.0.1:8471/v1').replace(/\/+$/, '');
}

export function statsUrl(path: string, query: Record<string, QueryValue> = {}): string {
	const url = new URL(base() + (path.startsWith('/') ? path : `/${path}`));
	for (const [key, value] of Object.entries(query)) {
		if (value !== undefined && value !== null) url.searchParams.set(key, String(value));
	}
	return url.toString();
}

export class StatsError extends Error {
	constructor(
		readonly status: number,
		message: string
	) {
		super(message);
	}
}

export async function stats<T>(
	fetcher: typeof fetch,
	path: string,
	query: Record<string, QueryValue> = {}
): Promise<T> {
	const response = await fetcher(statsUrl(path, query), {
		headers: { accept: 'application/json' }
	});
	if (!response.ok) {
		const detail = await response.text().catch(() => '');
		throw new StatsError(response.status, detail || response.statusText);
	}
	return (await response.json()) as T;
}

/** Endpoints the page can render without: a cold database 404s rather than failing the load. */
export async function statsOrNull<T>(
	fetcher: typeof fetch,
	path: string,
	query: Record<string, QueryValue> = {}
): Promise<T | null> {
	try {
		return await stats<T>(fetcher, path, query);
	} catch {
		return null;
	}
}
