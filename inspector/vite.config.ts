import { defineConfig } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

// The page is compiled into the Python package, beside the server that serves
// it (kitebase/inspector.py). In development (`kitebase inspector dev`) the
// questions go to that server, on the port it was started with.
const backend = `http://127.0.0.1:${process.env.KITEBASE_INSPECTOR_PORT ?? '8400'}`;

export default defineConfig({
	plugins: [svelte()],
	base: './',
	build: { outDir: '../kitebase/inspector_ui', emptyOutDir: true },
	server: {
		proxy: { '/api': { target: backend, changeOrigin: true } }
	}
});
