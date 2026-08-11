export interface Freshness {
	data_as_of: string | null;
	stale: boolean;
}

export interface Point {
	ts: string;
	v: number;
	d?: number;
	filled?: boolean;
	synthetic?: boolean;
}

export interface Series {
	interval: string;
	fill: string;
	buckets: number;
	observed_buckets: number;
	from: string | null;
	to: string | null;
	series: Record<string, Point[]>;
	entity?: Record<string, unknown>;
	note?: string;
}

export type GlobalTotals = Record<string, number | null>;

export interface GlobalResponse extends Freshness {
	scope: string;
	totals: GlobalTotals;
}

export interface MetaResponse {
	counts: Record<string, number>;
	metrics: Record<string, string[]>;
	coverage: {
		users_complete: number;
		users_partial: number;
		projects_listed: number;
		projects_tracked: number;
		projects_crawled: number;
		users_listed: number;
		users_tracked: number;
		users_crawled: number;
		threads_read: number;
		threads_pending: number;
	};
	data_source: string;
	caveats: string[];
}

export interface HealthResponse {
	status: 'ok' | 'degraded';
	mongo: boolean;
	last_crawl: string | null;
	stale: boolean;
	errors_last_hour: number;
	queue: Record<string, number>;
	sitemap: {
		last_checked: string | null;
		last_synced: string | null;
		counts: Record<string, number> | null;
	};
	now: string;
}
