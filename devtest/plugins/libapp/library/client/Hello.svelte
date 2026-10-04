<script lang="ts">
  /**
   * Library Plugin - Hello World Component
   * Located in: kitebase/devtest/plugins/libapp/library/client/
   * Registry ID: library.hello
   *
   * Demonstrates callback props: the host page can pass `onAction` to react
   * to events emitted by this plugin component.
   */

  interface Props {
    message?: string;
    userName?: string;
    /** Optional callback — called when the user clicks the action button. */
    onAction?: (data: { type: string; payload: unknown }) => void;
  }

  let { message = 'Hello from Library Plugin!', userName = 'Guest', onAction }: Props = $props();

  function handleClick() {
    if (onAction) {
      onAction({ type: 'button_click', payload: { userName, message } });
    } else {
      alert('Hello from plugin component! (no onAction callback set)');
    }
  }
</script>

<div class="rounded-lg border-2 border-purple-300 bg-purple-50 p-6">
  <div class="mb-4 flex items-center gap-3">
    <svg class="h-8 w-8 text-purple-600" fill="none" viewBox="0 0 24 24" stroke="currentColor">
      <path
        stroke-linecap="round"
        stroke-linejoin="round"
        stroke-width="2"
        d="M12 6.253v13m0-13C10.832 5.477 9.246 5 7.5 5S4.168 5.477 3 6.253v13C4.168 18.477 5.754 18 7.5 18s3.332.477 4.5 1.253m0-13C13.168 5.477 14.754 5 16.5 5c1.747 0 3.332.477 4.5 1.253v13C19.832 18.477 18.247 18 16.5 18c-1.746 0-3.332.477-4.5 1.253"
      />
    </svg>
    <h3 class="text-xl font-bold text-purple-800">Library Plugin Component</h3>
  </div>

  <div class="space-y-3">
    <p class="text-lg font-semibold text-purple-900">{message}</p>
    <p class="text-purple-700">Welcome, <strong>{userName}</strong>!</p>

    <div class="mt-4 rounded border border-purple-200 bg-white p-4">
      <p class="text-sm text-gray-600"><strong>Plugin Info:</strong></p>
      <ul class="mt-2 space-y-1 text-xs text-gray-500">
        <li>• Component: HelloWorld.svelte</li>
        <li>• Plugin: library</li>
        <li>• Registry ID: library.hello</li>
        <li>• onAction prop: {onAction ? 'wired by host' : 'not set'}</li>
      </ul>
    </div>

    <button
      onclick={handleClick}
      class="mt-2 rounded bg-purple-600 px-4 py-2 text-white transition-colors hover:bg-purple-700"
    >
      Fire onAction callback
    </button>
  </div>
</div>
