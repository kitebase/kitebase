<script lang="ts">
  /**
   * The home page of devtest: a few words of welcome and the users to try,
   * one per role, with what each of them is meant to see.
   * Registry ID: library.welcome
   *
   * Everything it shows comes from the page's props (pages.welcome): the
   * component only lays it out. The row of whoever is signed in is marked.
   */
  import { authStore } from '$kitebase/auth/store.svelte';

  interface TryUser {
    username: string;
    password: string;
    role: string;
    sees: string;
  }

  interface Props {
    title?: string;
    intro?: string[];
    users?: TryUser[];
  }

  let { title = 'Welcome', intro = [], users = [] }: Props = $props();

  const me = $derived(authStore.user?.username);
</script>

<div class="welcome">
  <h2>{title}</h2>
  {#each intro as line, i (i)}
    <p>{line}</p>
  {/each}

  {#if users.length}
    <table>
      <thead>
        <tr><th>User</th><th>Password</th><th>Role</th><th>What it shows</th></tr>
      </thead>
      <tbody>
        {#each users as u (u.username)}
          <tr class={{ me: u.username === me }}>
            <td><code>{u.username}</code></td>
            <td><code>{u.password}</code></td>
            <td>{u.role}</td>
            <td>{u.sees}</td>
          </tr>
        {/each}
      </tbody>
    </table>
  {/if}
</div>

<style>
  .welcome {
    max-width: 60rem;
    padding: 1.5rem;
    color: var(--kb-text);
  }
  h2 {
    margin: 0 0 0.75rem;
    font-size: 1.4rem;
    font-weight: 600;
  }
  p {
    margin: 0 0 0.5rem;
    color: var(--kb-text-subtle);
  }
  table {
    margin-top: 1.25rem;
    width: 100%;
    border-collapse: collapse;
    font-size: 0.875rem;
  }
  th,
  td {
    padding: 0.4rem 0.6rem;
    border-bottom: 1px solid var(--kb-border);
    text-align: left;
    vertical-align: top;
  }
  th {
    font-weight: 600;
    color: var(--kb-text-subtle);
  }
  tr.me td {
    background: var(--kb-accent-light);
    font-weight: 500;
  }
  code {
    font-size: 0.85em;
  }
</style>
