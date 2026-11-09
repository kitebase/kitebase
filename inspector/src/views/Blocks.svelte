<script lang="ts">
	import type { Block, Cell } from '../lib/api';
	import { href } from '../lib/router.svelte';

	// What a plugin adds to the nodes of its root (introspect.add_detail):
	// lists of links and tables, drawn without knowing what they mean.

	let { blocks }: { blocks: Block[] } = $props();

	const text = (c: Cell) => (typeof c === 'string' ? c : c.text);
	const link = (c: Cell) =>
		typeof c === 'string' || !c.path ? null : c.subject ? href('tree', c.path, c.subject) : href('tree', c.path);
	const tone = (c: Cell) => (typeof c === 'string' ? '' : (c.tone ?? ''));
	const title = (c: Cell) => (typeof c === 'string' ? undefined : c.title);
</script>

{#snippet cell(c: Cell)}
	{#if link(c)}
		<a class="mono {tone(c)}" href={link(c)} title={title(c)}>{text(c)}</a>
	{:else}
		<span class={tone(c)} title={title(c)}>{text(c)}</span>
	{/if}
{/snippet}

{#each blocks as b, i (i)}
	<h2>{b.title}</h2>
	{#if b.doc}<p class="small muted">{b.doc}</p>{/if}
	{#if b.links}
		<p class="links">
			{#each b.links as c, j (j)}{@render cell(c)}{:else}<span class="faint small">none</span>{/each}
		</p>
	{:else if b.rows}
		<table class="grid">
			<thead><tr>{#each b.columns ?? [] as h (h)}<th>{h}</th>{/each}</tr></thead>
			<tbody>
				{#each b.rows as r, j (j)}
					<tr>{#each r as c, k (k)}<td class="small">{@render cell(c)}</td>{/each}</tr>
				{/each}
			</tbody>
		</table>
	{/if}
{/each}

<style>
	.links { display: flex; flex-wrap: wrap; gap: .3rem .8rem; }
	.ok { color: var(--accent); }
	.cond { color: var(--warn); }
	.no { color: var(--error); }
	.faint { color: var(--faint); }
	[title] { cursor: help; }
	a[title] { cursor: pointer; }
</style>
