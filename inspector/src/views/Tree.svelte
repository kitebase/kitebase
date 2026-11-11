<script lang="ts">
	import { onMount } from 'svelte';
	import { api, type RootSummary } from '../lib/api';
	import { href } from '../lib/router.svelte';

	// The roots of the merge: every section the plugins write into, each opened
	// the same way, node by node, with who reads it and what its nodes show
	// beyond the tree. A root nobody reads stands out.

	let roots = $state<RootSummary[] | null>(null);
	let error = $state('');

	onMount(() => {
		api.roots().then((r) => (roots = r)).catch((e) => (error = e.message));
	});
</script>

<h1>Tree</h1>
<p class="muted small">What the plugins write, merged: each root of the tree as it runs, who writes it, who reads it, and what its nodes show.</p>

{#if error}
	<p class="muted">{error}</p>
{:else if roots}
	<table class="grid">
		<thead><tr><th>Root</th><th>Entries</th><th>Written by</th><th>Read by</th><th>Shows</th></tr></thead>
		<tbody>
			{#each roots as r (r.name)}
				<tr>
					<td class="mono"><a href={href('tree', r.name)}>{r.name}</a></td>
					<td>{r.count}</td>
					<td>{#each r.plugins as p (p)}<span class="badge">{p}</span>{/each}</td>
					<td class="mono small">
						{#if r.owner}{r.owner}{:else}<span class="nobody">nobody reads it</span>{/if}
					</td>
					<td class="small muted">
						{[r.resolved && 'as the client receives it', r.detail && 'a detail'].filter(Boolean).join(', ')}
					</td>
				</tr>
			{/each}
		</tbody>
	</table>
{:else}
	<p class="muted">Loading...</p>
{/if}

<style>
	.nobody { padding: .1rem .4rem; border-radius: 4px; background: var(--hit); }
</style>
