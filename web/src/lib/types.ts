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

export interface Mission {
	slug: string;
	name?: string | null;
	shipped?: boolean;
	sections_done?: number | null;
	sections_total?: number | null;
}

export interface ProjectCard {
	/** Null when the ranking on show does not hold it. */
	rank: number | null;
	project_id: number;
	title: string | null;
	description?: string | null;
	banner_url?: string | null;
	demo_url?: string | null;
	repo_url?: string | null;
	owner_id?: number | null;
	owner_username?: string | null;
	owner_avatar_url?: string | null;
	members: string[];
	is_super_star: boolean;
	is_hardware: boolean;
	mission?: Mission | null;
	value: number | null;
	stats: Record<string, number | null>;
	created_at_estimate?: string | null;
	first_seen?: string | null;
	last_changed?: string | null;
	/** Search results only: the title matched exactly. */
	exact?: boolean;
}

export interface ProjectBoardResponse extends Freshness {
	metric: string;
	source: string;
	total: number;
	limit: number;
	offset: number;
	items: ProjectCard[];
}

export interface ProjectSearchResponse extends Freshness {
	query: string;
	metric: string;
	total: number;
	items: ProjectCard[];
}

export interface ProjectDoc extends ProjectSummary, Freshness {
	is_hardware?: boolean;
	mission?: Mission | null;
	owner_avatar_url?: string | null;
	super_star_at?: string | null;
	super_star_by?: string | null;
	super_star_note?: string | null;
}

/** Rows crawled before the parser read attachments have none. */
export interface DevlogMedia {
	kind: 'image' | 'video';
	url: string;
	/** Videos only, and only when the card renders a still. */
	poster?: string;
}

export interface DevlogDoc {
	_id: number;
	project_id: number;
	post_id?: number;
	username?: string | null;
	user_id?: number | null;
	posted_at?: string | null;
	duration_seconds?: number | null;
	likes?: number | null;
	comments?: number | null;
	views?: number | null;
	reposts?: number | null;
	/** The whole post. Rows crawled before we kept it have only `body_preview`. */
	body?: string | null;
	body_preview?: string | null;
	media?: DevlogMedia[] | null;
}

export interface ProjectDevlogsResponse extends Freshness {
	project_id: number;
	total: number;
	limit: number;
	offset: number;
	items: DevlogDoc[];
}

export interface ShipDoc {
	_id: number;
	project_id: number;
	ship_number?: number | null;
	username?: string | null;
	shipped_at?: string | null;
	devlogs_at_ship?: number | null;
	hours_at_ship?: number | null;
	multiplier?: number | null;
	/** Null until the review closes. */
	payout?: number | null;
	payout_hours?: number | null;
	status?: string | null;
	mission?: string | null;
	mission_slug?: string | null;
	body?: string | null;
}

export interface ProjectShipsResponse extends Freshness {
	project_id: number;
	total: number;
	stardust_total: number;
	items: ShipDoc[];
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
