<script lang="ts">
	import { compact } from '$lib/format';
	import type { Point } from '$lib/types';

	interface Props {
		points: Point[];
		color?: string;
	}

	let { points = [], color = 'var(--color-brand-blue)' }: Props = $props();

	const H = 340;
	const PAD = { top: 20, right: 16, bottom: 32, left: 64 };

	// One user unit per CSS pixel, so axis labels keep their size instead of scaling with the box.
	let measured = $state(0);
	let W = $derived(Math.max(320, measured || 820));

	let plot = $derived.by(() => {
		const values = points.filter((p) => Number.isFinite(p.v));
		if (values.length < 2) return null;

		const nums = values.map((p) => p.v);
		const rawMin = Math.min(...nums);
		const rawMax = Math.max(...nums);
		const pad = (rawMax - rawMin || Math.abs(rawMax) || 1) * 0.12;
		const min = rawMin - pad;
		const max = rawMax + pad;

		const innerW = W - PAD.left - PAD.right;
		const innerH = H - PAD.top - PAD.bottom;
		const x = (i: number) => PAD.left + (i / (values.length - 1)) * innerW;
		const y = (v: number) => PAD.top + (1 - (v - min) / (max - min)) * innerH;

		const coords = values.map((p, i) => `${x(i).toFixed(1)},${y(p.v).toFixed(1)}`);
		const ticks = [0, 0.25, 0.5, 0.75, 1].map((t) => {
			const v = max - t * (max - min);
			return { y: PAD.top + t * innerH, label: compact(v) };
		});

		// Hourly buckets repeat the same date all the way across a short window.
		const spanMs = Date.parse(values.at(-1)!.ts) - Date.parse(values[0].ts);
		const label = (iso: string) =>
			spanMs <= 2 * 86_400_000
				? new Date(iso).toLocaleTimeString('en', { hour: 'numeric', minute: '2-digit' })
				: new Date(iso).toLocaleDateString('en', { month: 'short', day: 'numeric' });

		// A two-point window puts the midpoint on top of the first, so collapse the repeats.
		const marks = [...new Set([0, Math.floor((values.length - 1) / 2), values.length - 1])];

		return {
			line: `M${coords.join('L')}`,
			area: `M${x(0)},${H - PAD.bottom} L${coords.join('L')} L${x(values.length - 1)},${H - PAD.bottom} Z`,
			ticks,
			last: { x: x(values.length - 1), y: y(values.at(-1)!.v) },
			xLabels: marks.map((i, n) => ({
				x: x(i),
				text: label(values[i].ts),
				anchor: n === 0 ? 'start' : n === marks.length - 1 ? 'end' : 'middle'
			}))
		};
	});
</script>

<div class="chart-box" bind:clientWidth={measured}>
{#if plot}
	<svg class="chart" viewBox="0 0 {W} {H}" height={H} role="img" aria-label="Time series">
		{#each plot.ticks as tick (tick.y)}
			<line x1={PAD.left} x2={W - PAD.right} y1={tick.y} y2={tick.y} class="grid" />
			<text x={PAD.left - 12} y={tick.y + 4} class="axis" text-anchor="end">{tick.label}</text>
		{/each}

		<path d={plot.area} fill={color} opacity="0.1" />
		<path d={plot.line} fill="none" stroke={color} stroke-width="2" stroke-linejoin="round" />
		<circle cx={plot.last.x} cy={plot.last.y} r="3.5" fill={color} />

		{#each plot.xLabels as label, i (i)}
			<text x={label.x} y={H - 8} class="axis" text-anchor={label.anchor}>{label.text}</text>
		{/each}
	</svg>
{:else}
	<p class="chart-empty muted">Not enough history yet. Two snapshots make a line.</p>
{/if}
</div>

<style>
	.chart-box {
		width: 100%;
	}

	.chart {
		display: block;
		width: 100%;
	}

	.grid {
		stroke: var(--color-space-surface-faint);
		stroke-width: 1;
	}

	.axis {
		fill: var(--color-set-1-fg-secondary);
		font-size: 13px;
		font-family: var(--font-text);
	}

	.chart-empty {
		padding: var(--space-xxl) 0;
		text-align: center;
		font-size: var(--font-size-s);
	}
</style>
