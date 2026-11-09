<script lang="ts">
	import { onMount } from 'svelte';
	import { api, type RootSummary } from '../lib/api';
	import { href } from '../lib/router.svelte';

	// The roots of the merge: every section the plugins write into, each opened
	// the same way, node by node.

	let roots = $state<RootSummary[] | null>(null);
	let error = $state('');

	onMount(() => {
		api.roots().then((r) => (roots = r)).catch((e) => (error = e.message));
	});
</script>

<h1>Tree</h1>
<p class="muted small">What the plugins write, merged: each root of the tree as it runs, and who writes it.</p>

{#if error}
	<p class="muted">{error}</p>
{:else if roots}
	<table class="grid">
		<thead><tr><th>Root</th><th>Entries</th><th>Written by</th></tr></thead>
		<tbody>
			{#each roots as r (r.name)}
				<tr>
					<td class="mono"><a href={href('tree', r.name)}>{r.name}</a></td>
					<td>{r.count}</td>
					<td>{#each r.plugins as p (p)}<span class="badge">{p}</span>{/each}</td>
				</tr>
			{/each}
		</tbody>
	</table>
{:else}
	<p class="muted">Loading...</p>
{/if}
