<script lang="ts">
	import { page } from '$app/state';
	import ErrorState from '$components/ErrorState.svelte';
</script>

<svelte:head>
	<title>{page.status} | Stardance Stats</title>
</svelte:head>

<div class="container">
	{#if page.status === 404}
		<ErrorState code="404" actionLabel="Return" href="/" />
	{:else}
		<ErrorState code="API error" actionLabel="Refresh" />
	{/if}
</div>

<style>
	.container {
		display: flex;
	}
</style>
