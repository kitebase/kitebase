<script lang="ts">
	import type { AppSummary } from '../lib/api';

	let { summary }: { summary: AppSummary | null } = $props();
</script>

{#if summary}
	<h1>{summary.app.name ?? 'Application'}</h1>
	<p class="muted">{summary.app.description ?? ''} <span class="faint mono">{summary.root}</span></p>

	<div class="counts">
		{#each Object.entries(summary.counts) as [what, n] (what)}
			<div class="card"><div class="n">{n}</div><div class="small muted">{what}</div></div>
		{/each}
	</div>

	<h2>Plugins, in load order</h2>
	<table class="grid">
		<thead><tr><th>Plugin</th><th>Depends on</th><th>Data files</th><th>Python</th><th>Directory</th></tr></thead>
		<tbody>
			{#each summary.plugins as p (p.name)}
				<tr>
					<td><strong>{p.name}</strong> <span class="faint small">{p.version ?? ''}</span>
						{#if p.description}<div class="small muted">{p.description}</div>{/if}</td>
					<td>{#each p.depends_on as d (d)}<span class="badge">{d}</span>{/each}</td>
					<td class="mono small">{p.data_files.join(', ')}</td>
					<td class="mono small">{p.sources.join(', ')}</td>
					<td class="mono small faint">{p.dir}</td>
				</tr>
			{/each}
		</tbody>
	</table>
{:else}
	<p class="muted">Loading...</p>
{/if}

<style>
	.counts { display: flex; gap: .75rem; flex-wrap: wrap; margin: 1rem 0; }
	.counts .card { min-width: 7rem; }
	.n { font-size: 1.4rem; font-weight: 600; }
</style>
