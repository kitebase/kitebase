<script lang="ts">
  /**
   * What a plugin sees of the form it is mounted on: a strip on top of every
   * form of a table, declared in `form_areas.yaml`. It shows the props the form
   * hands it and the last event it heard on `lifecycle`, as a component that
   * shows who else is editing would use them.
   */
  import { onDestroy } from 'svelte';
  import { lifecycle, type FormRef } from '$kitebase/lifecycle';

  let { form, meta = {}, dirty = false }: {
    form: FormRef;
    meta?: Record<string, unknown>;
    dirty?: boolean;
  } = $props();

  let last = $state('open');
  let changes = $state(0);
  let depth = $state(0);

  const at = () => new Date().toLocaleTimeString();
  const mine = (e: FormRef) => e.key === form.key;
  const offs = [
    lifecycle.on('form:change', (e) => { if (mine(e)) { changes += 1; last = `change ${at()}`; } }),
    lifecycle.on('form:dirty', (e) => { if (mine(e)) last = `${e.dirty ? 'dirty' : 'clean'} ${at()}`; }),
    lifecycle.on('form:saved', (e) => { if (mine(e)) last = `saved ${at()}`; }),
    lifecycle.on('stack:push', (e) => { depth = e.depth; last = `frame opened ${at()}`; }),
    lifecycle.on('stack:pop', (e) => { depth = e.depth; last = `frame closed ${at()}`; }),
  ];
  onDestroy(() => offs.forEach((off) => off()));
</script>

<div class="pulse">
  <span>{form.page} #{form.id ?? 'new'}</span>
  {#if meta.$version !== undefined}<span>version {String(meta.$version)}</span>{/if}
  <span>{dirty ? 'dirty' : 'clean'}</span>
  <span>{changes} changes</span>
  {#if depth}<span>depth {depth}</span>{/if}
  <span class="last">{last}</span>
</div>

<style>
  .pulse {
    display: flex;
    flex-wrap: wrap;
    gap: 0.75rem;
    padding: 0.2rem 0.75rem;
    font-size: 0.72rem;
    color: var(--kb-text-subtle);
    background: var(--kb-surface);
    border-bottom: 1px solid var(--kb-border);
  }
  .last {
    margin-left: auto;
  }
</style>
