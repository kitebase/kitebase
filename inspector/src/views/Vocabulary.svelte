<script lang="ts">
	import { onMount } from 'svelte';
	import { api, type Catalog, type CatalogKey } from '../lib/api';
	import { href } from '../lib/router.svelte';

	// The keys the YAML is written with, by form, and what each one means
	// (kitebase/catalog.yaml), with where this application uses them.

	type Selection = { kind: 'form'; name: string } | { kind: 'directives' } | { kind: 'tokens' };

	let catalog = $state<Catalog | null>(null);
	let error = $state('');
	let selected = $state<Selection>({ kind: 'form', name: 'Table' });
	let query = $state('');
	let open = $state<Set<string>>(new Set());

	onMount(() => {
		api.catalog().then((c) => (catalog = c)).catch((e) => (error = e.message));
	});

	const found = $derived.by(() => {
		const q = query.trim().toLowerCase();
		if (!q || !catalog) return [];
		const out: { form: string; key: string; spec: CatalogKey }[] = [];
		for (const [form, spec] of Object.entries(catalog.forms)) {
			for (const [key, k] of Object.entries(spec.keys)) {
				if (key.toLowerCase().includes(q) || (k.doc ?? '').toLowerCase().includes(q)) out.push({ form, key, spec: k });
			}
		}
		return out;
	});

	function pick(sel: Selection) {
		selected = sel;
		query = '';
	}

	function toggle(id: string) {
		const next = new Set(open);
		if (next.has(id)) next.delete(id);
		else next.add(id);
		open = next;
	}

	const usesOf = (form: string, key: string) => catalog?.uses[`${form}.${key}`] ?? [];
	const show = (v: unknown) => (typeof v === 'string' ? v : JSON.stringify(v));

	// tables.Book.columns[3].nullable -> the table, to open it
	function tableOf(path: string): string | null {
		const m = /^tables\.([^.[]+)/.exec(path);
		return m ? m[1] : null;
	}

	const isForm = (name: string) => selected.kind === 'form' && selected.name === name && !query;
</script>

{#snippet keyRow(form: string, key: string, spec: CatalogKey, withForm: boolean)}
	{@const uses = usesOf(form, key)}
	{@const id = `${form}.${key}`}
	<tr class:unused={!uses.length}>
		<td class="mono nowrap">
			{#if withForm}<button class="linkish" onclick={() => pick({ kind: 'form', name: form })}>{form}</button>.{/if}<strong>{key}</strong>
			{#if spec.derived}<span class="badge" title="Written by the framework while resolving: nobody declares it">derived</span>{/if}
		</td>
		<td class="mono small nowrap">
			{#if spec.form}
				{#if spec.list}list of&nbsp;{/if}<button class="linkish" onclick={() => pick({ kind: 'form', name: spec.form! })}>{spec.form}</button>
			{:else if spec.values}{spec.values.join(' | ')}{:else}{spec.type}{/if}
			{#if spec.ref}<span class="faint" title="Names a {spec.ref} that must exist"> &rarr; {spec.ref}</span>{/if}
			{#if spec.tokens}<button class="linkish" onclick={() => pick({ kind: 'tokens' })}> $ {spec.tokens}</button>{/if}
		</td>
		<td class="small">{spec.doc ?? ''} {#if spec.see}<span class="faint">({spec.see})</span>{/if}</td>
		<td class="small nowrap right">
			{#if uses.length}<button class="linkish" onclick={() => toggle(id)}>{uses.length} {open.has(id) ? '▴' : '▾'}</button>{:else}0{/if}
		</td>
	</tr>
	{#if open.has(id)}
		<tr><td colspan="4" class="uses">
			<table>
				<tbody>
					{#each uses as u (u.path)}
						{@const t = tableOf(u.path)}
						<tr>
							<td class="mono small">{#if t}<a href={href('tables', t)}>{u.path}</a>{:else}{u.path}{/if}</td>
							<td class="mono small">{show(u.value)}</td>
							<td class="right">{#if u.plugin}<span class="badge">{u.plugin}</span>{/if}</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</td></tr>
	{/if}
{/snippet}

<h1>Vocabulary</h1>
<p class="muted small">The keys of the YAML by form, what each one means, and where this application uses it.</p>

{#if error}
	<p class="muted">{error}</p>
{:else if catalog}
	<div class="split">
		<aside>
			<input class="search" placeholder="Find a key or a meaning" bind:value={query} />
			<h3>Forms</h3>
			{#each Object.keys(catalog.forms) as name (name)}
				<button class="item" class:active={isForm(name)} onclick={() => pick({ kind: 'form', name })}>{name}</button>
			{/each}
			<h3>The $ words</h3>
			<button class="item" class:active={selected.kind === 'directives' && !query} onclick={() => pick({ kind: 'directives' })}>Directives</button>
			<button class="item" class:active={selected.kind === 'tokens' && !query} onclick={() => pick({ kind: 'tokens' })}>Tokens</button>
			<h3>Paths</h3>
			{#each Object.entries(catalog.roots) as [path, form] (path)}
				<button class="item mono small" onclick={() => pick({ kind: 'form', name: form })}>{path} <span class="faint">&rarr; {form}</span></button>
			{/each}
		</aside>

		<div class="main">
			{#if query.trim()}
				<p class="small muted">{found.length} keys</p>
				<table class="grid"><tbody>
					{#each found as f (f.form + '.' + f.key)}{@render keyRow(f.form, f.key, f.spec, true)}{/each}
				</tbody></table>
			{:else if selected.kind === 'form'}
				{@const spec = catalog.forms[selected.name]}
				{#if spec}
					<h2 style="margin-top: 0">{selected.name}</h2>
					<p class="small muted">{spec.doc ?? ''} {#if spec.see}<span class="faint">({spec.see})</span>{/if}</p>
					<table class="grid">
						<thead><tr><th>Key</th><th>Holds</th><th>Meaning</th><th class="right">Uses</th></tr></thead>
						<tbody>{#each Object.entries(spec.keys) as [key, k] (key)}{@render keyRow(selected.name, key, k, false)}{/each}</tbody>
					</table>
				{/if}
			{:else if selected.kind === 'directives'}
				<h2 style="margin-top: 0">Directives</h2>
				<p class="small muted">The <code>$</code> keys of the merge and of the framework: not keys of a form, they may sit on any node.</p>
				<table class="grid"><tbody>
					{#each Object.entries(catalog.directives) as [name, d] (name)}
						<tr><td class="mono"><strong>{name}</strong></td><td class="small muted">{d.family}</td>
							<td class="small">{d.doc} {#if d.see}<span class="faint">({d.see})</span>{/if}</td></tr>
					{/each}
				</tbody></table>
			{:else}
				<h2 style="margin-top: 0">Tokens</h2>
				<p class="small muted">The <code>$</code> values resolved later; their meaning is the docstring of what the registry holds.</p>
				{#each Object.entries(catalog.tokens) as [family, t] (family)}
					<h3 class="mono">{family}</h3>
					<p class="small muted">{t.doc} {#if t.registry}<span class="mono faint">{t.registry}</span>{/if}</p>
					<table class="grid"><tbody>
						{#each Object.entries(t.names) as [name, doc] (name)}
							<tr><td class="mono"><strong>{name}</strong></td><td class="small">{doc}</td></tr>
						{/each}
					</tbody></table>
				{/each}
			{/if}
		</div>
	</div>
{:else}
	<p class="muted">Loading...</p>
{/if}

<style>
	.split { display: flex; gap: 1.5rem; align-items: flex-start; }
	aside { width: 16rem; flex-shrink: 0; }
	aside h3 { font-size: 11px; text-transform: uppercase; color: var(--muted); margin: 1rem 0 .25rem; }
	.item { display: block; width: 100%; text-align: left; background: none; border: 0; padding: .15rem .5rem; border-radius: 4px; }
	.item:hover { background: var(--code); }
	.item.active { background: var(--accent); color: var(--accent-text); }
	.main { flex: 1; min-width: 0; }
	.nowrap { white-space: nowrap; }
	.right { text-align: right; }
	tr.unused td { color: var(--faint); }
	.uses { background: var(--code); }
	.uses table { width: 100%; }
</style>
