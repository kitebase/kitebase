<script lang="ts">
  /**
   * Library plugin — msgbox demo (registry id: library.msgboxdemo)
   *
   * The same chain of questions twice. On the left the component asks them
   * itself, with the msgbox API: it knows what a colour is, and it can say
   * "nothing chosen" because it was there when the menu closed. On the right
   * the server composes the chain with the result vocabulary and the client
   * only interprets verbs — `endpoint` with `confirm`, `choose`, a report —
   * so it never learns what a colour is, and a dismissed menu is silence.
   *
   * A plugin ships this file next to its YAML and Python; Vite compiles it
   * into the client, and a page reaches it with `type: plugin`.
   */
  import { getContext } from 'svelte';
  import { api } from '$kitebase/api/client';
  import { msgbox } from '$kitebase/components/msgbox.svelte';
  import { applyResult } from '$kitebase/components/resultAction';
  import { stack as globalStack } from '$kitebase/stack/stack.svelte';
  import type { StackInstance } from '$kitebase/stack/stack.svelte';

  const stack = getContext<StackInstance>('kb:stack') ?? globalStack;

  const colours = [
    { code: 'red',   label: 'Red' },
    { code: 'green', label: 'Green' },
    { code: 'blue',  label: 'Blue' },
  ];

  let clientLog = $state<string[]>([]);
  let serverLog = $state<string[]>([]);
  let current = $state<string | null>(null);

  // ── Client side: the component owns the chain ─────────────────────────────

  async function runClient() {
    clientLog = [];
    const wants = await msgbox.confirm('Do you want to see the choices?', 'Colours');
    clientLog.push(`confirm → ${wants}`);
    if (!wants) return;

    const picked = await msgbox.choose(
      colours.map(c => ({ label: c.label, value: c.code, current: c.code === current })),
      'Colours', 'Pick one',
    ) as string | undefined;
    clientLog.push(`choose → ${picked ?? 'undefined'}`);

    if (picked === undefined) {
      await msgbox.alert('You chose nothing', 'Colours');
      return;
    }
    current = picked;
    await msgbox.alert(`You chose ${colours.find(c => c.code === picked)?.label}`, 'Colours');
  }

  // ── Server side: the endpoint owns the chain ──────────────────────────────
  // Every answer is logged before it is applied, so the page shows the data
  // that became each dialog. `api.endpoint` is wrapped only for the log: the
  // real thing is `applyResult`, the same interpreter the menu and the
  // navigator use.

  async function runServer() {
    serverLog = [];
    const res = await api.endpoint('color_chooser', { current });
    serverLog.push(JSON.stringify(res.data));
    await applyResult(res, { title: 'Colours', stack });
  }
</script>

<div class="p-6 space-y-6">
  <p class="text-sm" style="color: var(--kb-text-muted)">
    The same three questions. Left: the component asks them with the msgbox
    API. Right: the server answers with data, and the client interprets verbs.
  </p>

  <div class="grid grid-cols-1 gap-6 md:grid-cols-2">
    <div class="rounded-lg border p-4" style="border-color: var(--kb-border)">
      <h3 class="font-semibold mb-1">Client-owned chain</h3>
      <p class="text-xs mb-3" style="color: var(--kb-text-muted)">
        confirm → choose → alert. The component knows the colours, and can
        say "nothing chosen" because it saw the menu close.
      </p>
      <button class="btn btn-primary" onclick={runClient}>Run</button>
      <pre class="mt-3 text-xs whitespace-pre-wrap" style="color: var(--kb-text-subtle)">{clientLog.join('\n')}</pre>
    </div>

    <div class="rounded-lg border p-4" style="border-color: var(--kb-border)">
      <h3 class="font-semibold mb-1">Server-composed chain</h3>
      <p class="text-xs mb-3" style="color: var(--kb-text-muted)">
        <code>color_chooser</code> answers <code>endpoint</code>+<code>confirm</code>,
        then <code>choose</code>, then a report. Dismissing the menu is silence:
        the server never hears about it.
      </p>
      <button class="btn btn-primary" onclick={runServer}>Run</button>
      <pre class="mt-3 text-xs whitespace-pre-wrap" style="color: var(--kb-text-subtle)">{serverLog.join('\n')}</pre>
    </div>
  </div>

  {#if current}
    <p class="text-sm">Current colour: <strong>{current}</strong> — both chains mark it in the list.</p>
  {/if}
</div>
