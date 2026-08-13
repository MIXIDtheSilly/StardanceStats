<script lang="ts">
	interface Props {
		/** A file under static/icons. Bare names take .svg; give the extension for anything else. */
		name: string;
		size?: string;
		/** Quarter turns clockwise, so one chevron serves all four directions. */
		turn?: 0 | 1 | 2 | 3;
		/** Give this only when the icon carries meaning no nearby text repeats. */
		label?: string;
	}

	let { name, size = '1em', turn = 0, label }: Props = $props();

	let file = $derived(name.includes('.') ? name : `${name}.svg`);
	// The svg files hard-code a white stroke, so they mask. A full-colour file cannot.
	let painted = $derived(file.endsWith('.svg'));
</script>

{#if painted}
	<span
		class="icon"
		style="--icon: url('/icons/{file}'); --size: {size}; --turn: {turn}"
		role={label ? 'img' : undefined}
		aria-label={label}
		aria-hidden={label ? undefined : 'true'}
	></span>
{:else}
	<img
		class="icon icon--raster"
		src="/icons/{file}"
		style="--size: {size}; --turn: {turn}"
		alt={label ?? ''}
		aria-hidden={label ? undefined : 'true'}
		loading="lazy"
		decoding="async"
	/>
{/if}

<style>
	.icon {
		display: inline-block;
		flex: none;
		width: var(--size);
		height: var(--size);
		vertical-align: -0.125em;
		background-color: currentColor;
		-webkit-mask: var(--icon) center / contain no-repeat;
		mask: var(--icon) center / contain no-repeat;
		/* Measured: this is where the eye reads an icon as centred on the cap height. */
		translate: 0 0.06em;
		rotate: calc(var(--turn) * 90deg);
	}

	.icon--raster {
		background-color: transparent;
		-webkit-mask: none;
		mask: none;
		object-fit: contain;
	}
</style>
