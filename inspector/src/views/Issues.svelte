<script lang="ts">
	import { onMount } from 'svelte';
	import { api, type Issue, type Severity } from '../lib/api';

	let issues = $state<Issue[] | null>(null);
	let error = $state('');
	let query = $state('');
	let shown = $state<Record<Severity, boolean>>({ error: true, warning: true, info: false });

	onMount(() => {
		api.issues().then((i) => (issues = i)).catch((e) => (error = e.message));
	});

	const count = (s: Severity) => issues?.filter((i) => i.severity === s).length ?? 0;

	const filtered = $derived(
		(issues ?? []).filter((i) => {
			if (!shown[i.severity]) return false;
			const q = query.trim().toLowerCase();
			return !q || `${i.code} ${i.path} ${i.message} ${i.plugin ?? ''}`.toLowerCase().includes(q);
		})
	);
</script>

<h1>Issues</h1>
<p class="muted small">What <code>kitebase check</code> says of the application as it is now.</p>

{#if error}
	<p class="muted">{error}</p>
{:else if issues}
	<div class="bar">
		{#each ['error', 'warning', 'info'] as const as s (s)}
			<label><input type="checkbox" bind:checked={shown[s]} /> {s} ({count(s)})</label>
		{/each}
		<input class="search" placeholder="Filter by code, path, message or plugin" bind:value={query} />
	</div>
	<table class="grid">
		<thead><tr><th></th><th>Code</th><th>Where</th><th>Message</th><th>Plugin</th></tr></thead>
		<tbody>
			{#each filtered as i, n (n)}
				<tr>
					<td><span class="badge {i.severity}">{i.severity}</span></td>
					<td class="mono small">{i.code}</td>
					<td class="mono small">{i.path}</td>
					<td class="small">{i.message}</td>
					<td>{#if i.plugin}<span class="badge">{i.plugin}</span>{/if}</td>
				</tr>
			{/each}
		</tbody>
	</table>
{:else}
	<p class="muted">Loading...</p>
{/if}

<style>
	.bar { display: flex; gap: 1rem; align-items: center; margin-bottom: .75rem; }
	.bar label { white-space: nowrap; }
</style>
