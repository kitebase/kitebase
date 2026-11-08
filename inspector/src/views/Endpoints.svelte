<script lang="ts">
	import { onMount } from 'svelte';
	import { api, editorLink, type EndpointInfo } from '../lib/api';

	let endpoints = $state<[string, EndpointInfo][] | null>(null);
	let error = $state('');
	let open = $state<string | null>(null);
	let query = $state('');

	onMount(() => {
		api.endpoints().then((e) => (endpoints = Object.entries(e))).catch((e) => (error = e.message));
	});

	const undocumented = $derived((endpoints ?? []).filter(([, e]) => !e.doc).length);
	const filtered = $derived(
		(endpoints ?? []).filter(([name, e]) => {
			const q = query.trim().toLowerCase();
			return !q || `${name} ${e.module} ${e.docstring}`.toLowerCase().includes(q);
		})
	);
</script>

<h1>Endpoints</h1>
{#if error}
	<p class="muted">{error}</p>
{:else if endpoints}
	<p class="muted small">
		{endpoints.length} operations{#if undocumented}, <span class="badge warning">{undocumented} without a docstring</span>{/if}.
		A row opens the whole docstring.
	</p>
	<input class="search" placeholder="Filter by name, module or docstring" bind:value={query} />
	<table class="grid" style="margin-top: .75rem">
		<thead><tr><th>Operation</th><th>Module</th><th>Description</th><th>Source</th></tr></thead>
		<tbody>
			{#each filtered as [name, e] (name)}
				<tr class="clickable" class:selected={open === name} onclick={() => (open = open === name ? null : name)}>
					<td class="mono"><strong>{name}</strong></td>
					<td class="mono small muted">{e.module}</td>
					<td class="small">{#if e.doc}{e.doc}{:else}<span class="badge warning">no docstring</span>{/if}</td>
					<td class="small">
						{#if e.file}<a href={editorLink(e.file, e.line)} onclick={(ev) => ev.stopPropagation()}>{e.file.split('/').slice(-2).join('/')}:{e.line}</a>{/if}
					</td>
				</tr>
				{#if open === name && e.docstring}
					<tr><td colspan="4"><pre class="doc">{e.docstring}</pre></td></tr>
				{/if}
			{/each}
		</tbody>
	</table>
{:else}
	<p class="muted">Loading...</p>
{/if}

<style>
	.doc { margin: 0; padding: .5rem .75rem; background: var(--code); border-radius: 4px; white-space: pre-wrap; }
</style>
