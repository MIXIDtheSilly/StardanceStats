const COMPACT = new Intl.NumberFormat('en', { notation: 'compact', maximumFractionDigits: 1 });
const FULL = new Intl.NumberFormat('en');

export function compact(value: number | null | undefined): string {
	if (value === null || value === undefined || !Number.isFinite(value)) return '--';
	return Math.abs(value) < 10_000 ? FULL.format(Math.round(value)) : COMPACT.format(value);
}

export function full(value: number | null | undefined): string {
	if (value === null || value === undefined || !Number.isFinite(value)) return '--';
	return FULL.format(Math.round(value));
}

export function signed(value: number | null | undefined): string {
	if (value === null || value === undefined || !Number.isFinite(value) || value === 0) return '';
	return `${value > 0 ? '+' : '-'}${compact(Math.abs(value))}`;
}

export function percent(part: number, whole: number): string {
	if (!whole) return '--';
	return `${Math.round((part / whole) * 100)}%`;
}

export function relative(iso: string | null | undefined, now = Date.now()): string {
	if (!iso) return 'never';
	const then = Date.parse(iso);
	if (Number.isNaN(then)) return 'unknown';

	const seconds = Math.round((now - then) / 1000);
	if (seconds < 60) return 'just now';
	const minutes = Math.round(seconds / 60);
	if (minutes < 60) return `${minutes}m ago`;
	const hours = Math.round(minutes / 60);
	if (hours < 24) return `${hours}h ago`;
	const days = Math.round(hours / 24);
	if (days < 30) return `${days}d ago`;
	return `${Math.round(days / 30)}mo ago`;
}

export function isoDate(iso: string | null | undefined): string {
	if (!iso) return '';
	const date = new Date(iso);
	return Number.isNaN(date.getTime()) ? '' : date.toISOString().slice(0, 10);
}
