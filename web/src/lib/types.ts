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

export interface LeaderboardItem {
	rank: number;
	user_id: number;
	username: string | null;
	avatar_url: string | null;
	value: number | null;
	complete: boolean;
}

export interface LeaderboardResponse extends Freshness {
	metric: string;
	source: string;
	total: number;
	limit: number;
	offset: number;
	items: LeaderboardItem[];
}

export interface Coverage {
	projects_listed: number;
	projects_seen: number;
	projects_reported: number;
	devlogs_seen: number;
	devlogs_reported: number;
	ships_seen: number;
	ships_reported: number;
	projects_missing: number[];
	complete: boolean;
}

export interface UserDoc extends Freshness {
	_id: number;
	username: string;
	previous_usernames?: string[];
	avatar_url?: string | null;
	banner_url?: string | null;
	bio?: string | null;
	joined_at?: string | null;
	first_seen?: string | null;
	last_crawled?: string | null;
	hidden?: boolean;
	project_ids?: number[];
	stats: Record<string, number | null>;
	totals: Record<string, number | null>;
	coverage?: Coverage;
}

export interface ProjectSummary {
	_id: number;
	title: string;
	description?: string | null;
	banner_url?: string | null;
	demo_url?: string | null;
	repo_url?: string | null;
	is_super_star?: boolean;
	is_hardware?: boolean;
	created_at_estimate?: string | null;
	first_seen?: string | null;
	/** When a crawl last saw one of its numbers move. */
	last_changed?: string | null;
	last_crawled?: string | null;
	owner_id?: number;
	owner_username?: string | null;
	members?: string[];
	stats: Record<string, number | null>;
}

export interface UserProjectsResponse extends Freshness {
	user_id: number;
	username: string;
	total: number;
	items: ProjectSummary[];
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
	/** Both only present with ?deep=1; they read the whole frontier. */
	errors_last_hour?: number;
	queue?: Record<string, number>;
	sitemap: {
		last_checked: string | null;
		last_synced: string | null;
		counts: Record<string, number> | null;
	};
	now: string;
}
