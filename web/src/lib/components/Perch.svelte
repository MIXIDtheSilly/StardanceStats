<script lang="ts">
	interface Props {
		art:
			| 'star-creature'
			| 'star-creature-blue'
			| 'guest_star_2'
			| 'guest_star_3'
			| 'step-project-info--outline'
			| 'step-review--outline'
			| 'step-ship--outline';
		at?: 'top-left' | 'top-right' | 'bottom-left' | 'bottom-right';
		w?: string;
		/** Nudge off the corner, as a share of its own size. */
		x?: string;
		y?: string;
		turn?: string;
		flip?: boolean;
	}

	let {
		art,
		at = 'top-left',
		w = '4.5rem',
		x = '0%',
		y = '0%',
		turn = '0deg',
		flip = false
	}: Props = $props();
</script>

<img
	class="perch perch--{at}"
	src="/{art}.png"
	alt=""
	aria-hidden="true"
	loading="lazy"
	decoding="async"
	style="--w: {w}; --x: {x}; --y: {y}; --turn: {turn}; --flip: {flip ? -1 : 1}"
/>

<style>
	.perch {
		position: absolute;
		width: var(--w);
		height: auto;
		transform: translate(var(--x), var(--y)) rotate(var(--turn)) scaleX(var(--flip));
		pointer-events: none;
		user-select: none;
		/* Over the borders it perches on, under the thumbnails that grow on hover at 3. */
		z-index: 1;
	}

	.perch--top-left {
		left: 0;
		top: 0;
	}

	.perch--top-right {
		right: 0;
		top: 0;
	}

	.perch--bottom-left {
		left: 0;
		bottom: 0;
	}

	.perch--bottom-right {
		right: 0;
		bottom: 0;
	}

	@media (max-width: 640px) {
		.perch {
			display: none;
		}
	}
</style>
