<script lang="ts">
	import { onMount } from 'svelte';
	import { api, type AppSummary } from './lib/api';
	import { href, router } from './lib/router.svelte';
	import Overview from './views/Overview.svelte';
	import Issues from './views/Issues.svelte';
	import Tables from './views/Tables.svelte';
	import TableView from './views/TableView.svelte';
	import Vocabulary from './views/Vocabulary.svelte';
	import Endpoints from './views/Endpoints.svelte';

	// The developer's view of the running application: each section asks the
	// server when it is shown, so what it shows is the application as it is.

	const sections = [
		{ id: '', label: 'Overview' },
		{ id: 'issues', label: 'Issues' },
		{ id: 'tables', label: 'Tables' },
		{ id: 'vocabulary', label: 'Vocabulary' },
		{ id: 'endpoints', label: 'Endpoints' }
	];

	let summary = $state<AppSummary | null>(null);
	let stale = $state(false);
	let canRestart = $state(false);
	let restarting = $state(false);
	let offline = $state(false);

	const section = $derived(router.parts[0] ?? '');

	onMount(() => {
		api.app().then((s) => (summary = s)).catch(() => (offline = true));
		// The application on show is the one composed at start: when its files
		// change, the page says so and offers to compose it again.
		const timer = setInterval(async () => {
			try {
				const v = await api.version();
				if (restarting) location.reload();
				stale = v.stale;
				canRestart = v.restart;
				offline = false;
			} catch {
				offline = !restarting;
			}
		}, 2000);
		return () => clearInterval(timer);
	});

	async function restart() {
		restarting = true;
		await api.restart().catch(() => {});
	}
</script>

<div class="shell">
	<aside>
		<div class="brand">
			<strong>Kitebase Inspector</strong>
			{#if summary}
				<div class="small muted">{summary.app.name ?? '?'} {summary.app.version ?? ''}</div>
			{/if}
		</div>
		<nav>
			{#each sections as s (s.id)}
				<a class:active={section === s.id} href={href(...(s.id ? [s.id] : []))}>{s.label}</a>
			{/each}
		</nav>
	</aside>

	<main>
		{#if restarting}
			<div class="banner">Composing the application again...</div>
		{:else if offline}
			<div class="banner">The inspector server does not answer: is <code>kitebase inspector</code> running?</div>
		{:else if stale}
			<div class="banner">
				The files of the application changed since it was composed.
				{#if canRestart}<button class="linkish" onclick={restart}>Compose it again</button>{/if}
			</div>
		{/if}

		{#if section === ''}
			<Overview {summary} />
		{:else if section === 'issues'}
			<Issues />
		{:else if section === 'tables' && router.parts[1]}
			{#key router.parts[1]}<TableView name={router.parts[1]} column={router.parts[2]} />{/key}
		{:else if section === 'tables'}
			<Tables />
		{:else if section === 'vocabulary'}
			<Vocabulary />
		{:else if section === 'endpoints'}
			<Endpoints />
		{/if}
	</main>
</div>

<style>
	.shell { display: flex; min-height: 100vh; }
	aside { width: 13rem; flex-shrink: 0; background: var(--panel); border-right: 1px solid var(--line); }
	.brand { padding: .75rem 1rem; border-bottom: 1px solid var(--line); }
	nav { display: flex; flex-direction: column; padding: .5rem; gap: 2px; }
	nav a { padding: .35rem .6rem; border-radius: 4px; color: var(--text); }
	nav a:hover { background: var(--code); text-decoration: none; }
	nav a.active { background: var(--accent); color: var(--accent-text); }
	main { flex: 1; min-width: 0; padding: 1.25rem 1.5rem 3rem; }
	.banner { margin-bottom: 1rem; padding: .5rem .75rem; border-radius: 6px; background: var(--hit); }
</style>
