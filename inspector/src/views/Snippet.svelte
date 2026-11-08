<script lang="ts">
	import { api, editorLink, type Layer } from '../lib/api';

	// What one plugin wrote, as it is written: the lines of its file, numbered,
	// with the way to the whole file and to the editor.

	let { layer }: { layer: Layer } = $props();

	let whole = $state<string | null>(null);
	let error = $state('');

	const fileName = $derived(layer.file.split('/').pop() ?? layer.file);
	const shortPath = $derived(layer.file.split('/').slice(-3).join('/'));

	function numbered(text: string, first: number) {
		return text.replace(/\n$/, '').split('\n').map((t, i) => ({ n: first + i, t }));
	}

	async function toggleWhole() {
		if (whole !== null) {
			whole = null;
			return;
		}
		try {
			whole = (await api.source(layer.plugin, fileName)).text;
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		}
	}

	const inRange = (n: number) => layer.line !== undefined && n >= layer.line && n <= (layer.end ?? layer.line);

	// Inside the box only: the page stays where the reader is
	function scrollIn(node: HTMLElement) {
		const hit = node.querySelector<HTMLElement>('.hit');
		if (hit) node.scrollTop = hit.offsetTop - node.clientHeight / 3;
	}
</script>

<div class="snippet">
	<div class="head">
		<span class="badge accent">{layer.plugin}</span>
		<span class="mono small muted" title={layer.file}>{shortPath}{#if layer.line}:{layer.line}{/if}</span>
		<span class="spacer"></span>
		{#if layer.line}<button class="linkish small" onclick={toggleWhole}>{whole === null ? 'whole file' : 'just these lines'}</button>{/if}
		<a class="small" href={editorLink(layer.file, layer.line)}>editor</a>
	</div>
	{#if error}<p class="small muted">{error}</p>{/if}
	{#if whole !== null}
		<pre class="code tall" use:scrollIn>{#each numbered(whole, 1) as l (l.n)}<span class:hit={inRange(l.n)}><span class="ln">{l.n}</span>{l.t}
</span>{/each}</pre>
	{:else if layer.text && layer.line}
		<pre class="code">{#each numbered(layer.text, layer.line) as l (l.n)}<span><span class="ln">{l.n}</span>{l.t}
</span>{/each}</pre>
	{:else}
		<pre class="code">{JSON.stringify(layer.declared, null, 2)}</pre>
	{/if}
</div>

<style>
	.snippet { margin-bottom: .6rem; }
	.head { display: flex; gap: .5rem; align-items: center; margin-bottom: .2rem; }
	.spacer { flex: 1; }
	.code { position: relative; margin: 0; padding: .4rem 0; background: var(--code); border-radius: 4px; overflow-x: auto; }
	.code > span { display: block; padding-right: .75rem; }
	.code.tall { max-height: 26rem; overflow-y: auto; }
	.hit { background: var(--hit); }
	.ln { display: inline-block; width: 3.2rem; padding-right: .75rem; text-align: right; color: var(--faint); user-select: none; }
</style>
