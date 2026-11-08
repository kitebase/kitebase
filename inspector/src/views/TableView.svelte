<script lang="ts">
	import { onMount } from 'svelte';
	import { api, type ColumnInfo, type TableInfo } from '../lib/api';
	import { href } from '../lib/router.svelte';
	import Snippet from './Snippet.svelte';

	// A table as it runs, and where each piece of it was written: for a column,
	// every plugin that declares it, and the chain of types it takes the rest from.

	let { name, column }: { name: string; column?: string } = $props();

	let table = $state<TableInfo | null>(null);
	let error = $state('');

	onMount(() => {
		api.table(name).then((t) => (table = t)).catch((e) => (error = e.message));
	});

	const selected = $derived(table?.columns.find((c) => c.name === column) ?? null);

	const plugins = (c: ColumnInfo) => [...new Set(c.layers.map((l) => l.plugin))];

	function show(value: unknown): string {
		return typeof value === 'string' ? value : JSON.stringify(value);
	}
</script>

<p class="small"><a href={href('tables')}>Tables</a> / <span class="mono">{name}</span></p>

{#if error}
	<p class="muted">{error}</p>
{:else if table}
	<h1 class="mono">{table.name}</h1>
	<p>
		{#if table.attributes.label}<strong>{table.attributes.label}</strong>{/if}
		declared by {#each table.plugins as p (p)}<span class="badge accent">{p}</span>{/each}
		{#if table.attributes.mixins}mixins {#each table.attributes.mixins as string[] as m (m)}<span class="badge">{m}</span>{/each}{/if}
	</p>

	<div class="split">
		<div class="cols">
			<table class="grid">
				<thead><tr><th>Column</th><th>Type</th><th>Written by</th><th>Null</th><th></th></tr></thead>
				<tbody>
					{#each table.columns as c (c.name)}
						<tr class="clickable" class:selected={c.name === column}
							onclick={() => (location.hash = href('tables', name, c.name))}>
							<td class="mono"><strong>{c.name}</strong></td>
							<td class="mono small">{c.type ?? ''}</td>
							<td>
								{#each plugins(c) as p (p)}<span class="badge">{p}</span>{/each}
								{#if c.origin.kind === 'mixin'}<span class="small muted">from {c.origin.type}</span>{/if}
							</td>
							<td class="small">{c.attributes.nullable === false ? 'no' : ''}</td>
							<td class="small muted">
								{#if c.attributes.primary_key}key{/if}
								{#if c.attributes.foreign_key}&rarr; {show((c.attributes.foreign_key as Record<string, unknown>).target)}{/if}
								{#if c.attributes.virtual}virtual{/if}
							</td>
						</tr>
					{/each}
				</tbody>
			</table>

			<h2>Who points at it</h2>
			<div class="small">
				<p><span class="muted">Foreign keys:</span>
					{#each table.used_by.foreign_keys as fk (fk)}
						<a class="mono fk" href={href('tables', fk.split('.')[0], fk.split('.')[1])}>{fk}</a>
					{:else}<span class="faint">none</span>{/each}</p>
				<p><span class="muted">Views:</span> <span class="mono">{table.used_by.views.join(', ') || '-'}</span></p>
				<p><span class="muted">Pages:</span> <span class="mono">{table.used_by.pages.join(', ') || '-'}</span></p>
			</div>

			<h2>The table, as each plugin writes it</h2>
			{#each table.layers as l, i (i)}<Snippet layer={l} />{/each}
		</div>

		<div class="detail">
			{#if selected}
				<h2 class="mono" style="margin-top: 0">{name}.{selected.name}</h2>
				<h3>As it runs</h3>
				<table class="grid">
					<tbody>
						{#each Object.entries(selected.attributes) as [k, v] (k)}
							<tr><td class="mono small">{k}</td><td class="mono small">{show(v)}</td></tr>
						{/each}
					</tbody>
				</table>

				<h3>Where it is written</h3>
				{#if selected.origin.kind === 'mixin'}
					<p class="small muted">A column of the composite type <span class="mono">{selected.origin.type}</span>, which the table takes as a mixin.</p>
				{/if}
				{#each selected.layers as l, i (i)}<Snippet layer={l} />{:else}<p class="small faint">Not found in the plugins' files.</p>{/each}

				{#if selected.types.length}
					<h3>The types it takes the rest from</h3>
					<p class="small muted">
						{#each selected.types as t, i (t.name)}{#if i} &rarr; {/if}<span class="mono">{t.name}</span>{#if t.builtin} <span class="faint">(SQLAlchemy)</span>{/if}{/each}
					</p>
					{#each selected.types as t (t.name)}
						{#each t.layers as l, i (i)}<Snippet layer={l} />{/each}
					{/each}
				{/if}
			{:else}
				<p class="muted small">Choose a column to see where it is written, plugin by plugin, and the types it inherits from.</p>
			{/if}
		</div>
	</div>
{:else}
	<p class="muted">Loading...</p>
{/if}

<style>
	.split { display: flex; gap: 1.5rem; align-items: flex-start; }
	.cols { flex: 1 1 50%; min-width: 0; }
	.detail { flex: 1 1 50%; min-width: 0; position: sticky; top: 1rem; max-height: calc(100vh - 2rem); overflow-y: auto; }
	.detail h3 { margin-top: 1.1rem; }
	.fk { margin-right: .6rem; }
</style>
