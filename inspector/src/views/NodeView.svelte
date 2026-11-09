<script lang="ts">
	import { onMount } from 'svelte';
	import { api, type NodeChild, type NodeInfo } from '../lib/api';
	import { href } from '../lib/router.svelte';
	import Snippet from './Snippet.svelte';

	// A node of the merged tree, as it runs and as each plugin writes it: its
	// children to go down, the lines of every file that writes it beside them.

	let { path }: { path: string } = $props();

	let node = $state<NodeInfo | null>(null);
	let error = $state('');
	let whole = $state(false);

	onMount(() => {
		api.node(path).then((n) => (node = n)).catch((e) => (error = e.message));
	});

	// A table has a view of its own, with the types of its columns
	const table = $derived(node?.path.match(/^tables\.([^.[]+)/)?.[1] ?? null);

	function summary(c: NodeChild): string {
		if (c.kind === 'map') return `{ ${c.summary} }`;
		if (c.kind === 'list') return `[ ${c.summary} ]`;
		return typeof c.summary === 'string' ? c.summary : JSON.stringify(c.summary);
	}
</script>

<p class="small">
	<a href={href('tree')}>Tree</a>
	{#each node?.trail ?? [] as t (t.path)} / <a class="mono" href={href('tree', t.path)}>{t.key}</a>{/each}
</p>

{#if error}
	<p class="muted">{error}</p>
{:else if node}
	<h1 class="mono">{node.path}</h1>
	<p>
		{#if node.layers.length}
			written by {#each [...new Set(node.layers.map((l) => l.plugin))] as p (p)}<span class="badge accent">{p}</span>{/each}
		{/if}
		{#if table}<a class="small" href={href('tables', table)}>the table view</a>{/if}
	</p>

	<div class="split">
		<div class="side">
			{#if node.kind === 'value'}
				<pre class="value">{JSON.stringify(node.value, null, 2)}</pre>
			{:else}
				<table class="grid">
					<thead><tr><th>Key</th><th>Value</th><th>Written by</th></tr></thead>
					<tbody>
						{#each node.children as c (c.path)}
							<tr class="clickable" onclick={() => (location.hash = href('tree', c.path))}>
								<td class="mono"><strong>{c.key}</strong></td>
								<td class="mono small" class:muted={c.kind !== 'value'}>{summary(c)}</td>
								<td>{#each c.plugins as p (p)}<span class="badge">{p}</span>{/each}</td>
							</tr>
						{:else}
							<tr><td colspan="3" class="faint small">empty</td></tr>
						{/each}
					</tbody>
				</table>

				<h2><button class="linkish" onclick={() => (whole = !whole)}>{whole ? 'Hide' : 'Show'} the node as it runs</button></h2>
				{#if whole}<pre class="value">{JSON.stringify(node.value, null, 2)}</pre>{/if}
			{/if}
		</div>

		<div class="side detail">
			<h2 style="margin-top: 0">Where it is written</h2>
			{#if node.layers_of && node.layers_of !== node.path}
				<p class="small muted">A list whose items have no identity: the lines of <a class="mono" href={href('tree', node.layers_of)}>{node.layers_of}</a>, which holds it.</p>
			{/if}
			{#each node.layers as l, i (i)}<Snippet layer={l} />{:else}<p class="small faint">Not found in the plugins' files: written by the framework while it composes the application.</p>{/each}
		</div>
	</div>
{:else}
	<p class="muted">Loading...</p>
{/if}

<style>
	.split { display: flex; gap: 1.5rem; align-items: flex-start; }
	.side { flex: 1 1 50%; min-width: 0; }
	.detail { position: sticky; top: 1rem; max-height: calc(100vh - 2rem); overflow-y: auto; }
	.value { margin: 0; padding: .5rem .75rem; background: var(--code); border-radius: 4px; overflow-x: auto; }
</style>
