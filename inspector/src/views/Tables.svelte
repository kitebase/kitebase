<script lang="ts">
	import { onMount } from 'svelte';
	import { api, type TableSummary } from '../lib/api';
	import { href } from '../lib/router.svelte';

	let tables = $state<TableSummary[] | null>(null);
	let error = $state('');
	let query = $state('');

	onMount(() => {
		api.tables().then((t) => (tables = t)).catch((e) => (error = e.message));
	});

	const filtered = $derived(
		(tables ?? []).filter((t) => {
			const q = query.trim().toLowerCase();
			return !q || `${t.name} ${t.label ?? ''} ${t.plugins.join(' ')}`.toLowerCase().includes(q);
		})
	);
</script>

<h1>Tables</h1>
{#if error}
	<p class="muted">{error}</p>
{:else if tables}
	<input class="search" placeholder="Filter by name, label or plugin" bind:value={query} />
	<table class="grid" style="margin-top: .75rem">
		<thead><tr><th>Table</th><th>Label</th><th>Declared by</th><th>Mixins</th><th>Columns</th></tr></thead>
		<tbody>
			{#each filtered as t (t.name)}
				<tr>
					<td class="mono"><a href={href('tables', t.name)}>{t.name}</a></td>
					<td>{t.label ?? ''}</td>
					<td>{#each t.plugins as p (p)}<span class="badge">{p}</span>{/each}</td>
					<td>{#each t.mixins as m (m)}<span class="badge">{m}</span>{/each}</td>
					<td>{t.columns}</td>
				</tr>
			{/each}
		</tbody>
	</table>
{:else}
	<p class="muted">Loading...</p>
{/if}
