# Kitebase Inspector

The developer's view of a running Kitebase application: its tables and where
each column is written, plugin by plugin and line by line; the types a column
inherits from; the vocabulary of the YAML with the meaning of each key; the
endpoints with their docstrings; what `kitebase check` reports.

```bash
cd my-app
kitebase inspector              # http://localhost:8400, compiles the page when needed
kitebase inspector --port 8410
kitebase inspector build        # compile the page now
kitebase inspector dev          # work on the inspector itself: Vite on 5190, /api to the server
```

The server is `kitebase/inspector.py`, standard library only, listening on
127.0.0.1; the answers come from `kitebase/introspect.py`, on the application
as it runs. When the plugins' files change the page says so, and composes the
application again on request.

This directory is the page: Svelte 5 and Vite, no SvelteKit and no
`@kitebase/ui` (the inspector has to work when the descriptors are broken).
It compiles into `kitebase/inspector_ui/`, which git ignores. Compiling needs
Node.js and pnpm, once per workstation.
