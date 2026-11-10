"""
kitebase.inspector — the developer's view of a running application.

`kitebase inspector` loads the application as `check` does (its own script
composes it) and serves, on one port of this machine, the inspector's page and
the questions it asks (`/api/...`, answered by `kitebase.introspect`). The data
is the application as it is now, not a dump of it.

The standard library is enough, and that is a requirement: an application on
Flask has no FastAPI in its environment, and the inspector must run in any of
them without installing anything.

Safety, because a local server is reachable by any page the browser opens:

- it listens on 127.0.0.1 only;
- the `Host` header must name localhost or 127.0.0.1 with this port: a page
  that rebinds its own domain to 127.0.0.1 sends its own name, and is refused;
- no CORS header, and only GET, plus the POST that restarts it;
- files are served from the compiled page only; the sources of the plugins
  are read through `introspect.source`, which knows each plugin's files.

When the plugins' files change the application on show is old: `/api/version`
says so, and the page offers to restart, which runs the same command again in
the same process (`os.execv`), so the application is composed afresh.

The page is compiled from `inspector/` beside the package, in a source
checkout, into `kitebase/inspector_ui/`; `ensure_built` does it when the
compiled page is missing or older than its sources.
"""
import json
import mimetypes
import os
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

import kitebase

DEFAULT_PORT = 8400
DEV_PORT = 5190

PACKAGE_DIR = Path(kitebase.__file__).resolve().parent
UI_DIR = PACKAGE_DIR / 'inspector_ui'
SOURCE_DIR = PACKAGE_DIR.parent / 'inspector'

# What the compiled page is made of: newer than the page, it is compiled again
_SOURCE_PARTS = ('src', 'index.html', 'package.json', 'vite.config.ts')


class InspectorError(Exception):
    """What stops the inspector, said to whoever started it."""


# ── The compiled page ──────────────────────────────────────────────────────────

def _newest(paths: Iterable[Path]) -> float:
    newest = 0.0
    for path in paths:
        if path.is_dir():
            newest = max([newest] + [f.stat().st_mtime for f in path.rglob('*') if f.is_file()])
        elif path.exists():
            newest = max(newest, path.stat().st_mtime)
    return newest


def build_state(source: Path = SOURCE_DIR, ui: Path = UI_DIR) -> str:
    """'ok', 'missing' (to compile), 'stale' (older than its sources), or
    'no-source' (compiled page absent and nothing to compile it from)."""
    page = ui / 'index.html'
    has_source = (source / 'package.json').exists()
    if not page.exists():
        return 'missing' if has_source else 'no-source'
    if has_source and _newest(source / p for p in _SOURCE_PARTS) > page.stat().st_mtime:
        return 'stale'
    return 'ok'


def build(source: Path = SOURCE_DIR) -> None:
    """Compile the page: `pnpm install` the first time, then `pnpm build`."""
    if not (source / 'package.json').exists():
        raise InspectorError(
            f'No inspector sources in {source}: the page is compiled from a checkout '
            'of kitebase (an editable install).')
    pnpm = shutil.which('pnpm')
    if pnpm is None:
        raise InspectorError(
            'Compiling the inspector needs Node.js and pnpm '
            '(https://pnpm.io/installation), once per workstation.')
    if not (source / 'node_modules').exists():
        print(f'inspector: installing its packages in {source}', flush=True)
        _run([pnpm, 'install'], source)
    print(f'inspector: compiling into {UI_DIR}', flush=True)
    _run([pnpm, 'build'], source)


def _run(command, cwd: Path) -> None:
    if subprocess.call(command, cwd=str(cwd)) != 0:
        raise InspectorError(f"`{' '.join(command[1:] if len(command) > 1 else command)}` failed in {cwd}")


def ensure_built() -> None:
    """Compile the page when it is missing or older than its sources."""
    state = build_state()
    if state in ('missing', 'stale'):
        build()
    elif state == 'no-source':
        raise InspectorError(
            f'The inspector page is not compiled ({UI_DIR}) and there are no sources '
            'to compile it from: use a checkout of kitebase.')


# ── The sources of the application, to know when they change ───────────────────

def sources_signature(app: Any) -> Tuple[int, float]:
    """How many files the application is read from, and the newest of them."""
    pm = app.plugins
    files = list(getattr(pm, 'config_files', [pm.config_file]))   # a variant and its base
    for name in pm.sorted:
        d = pm.plugins[name].plugin_dir
        files += [f for f in d.rglob('*') if f.suffix in ('.yaml', '.yml', '.py', '.md')
                  and '__pycache__' not in f.parts]
    return len(files), max((f.stat().st_mtime for f in files if f.exists()), default=0.0)


# ── The server ─────────────────────────────────────────────────────────────────

def routes(app: Any) -> Dict[str, Callable[..., Any]]:
    """`/api/<name>` -> what answers it; the rest of the path is its argument."""
    from kitebase import introspect

    return {
        'app': lambda: introspect.app_summary(app),
        'issues': lambda: introspect.issues(app),
        'catalog': lambda: introspect.catalog(app),
        'endpoints': introspect.endpoints,
        'roots': lambda: introspect.roots(app),
        'node': lambda path='', subject=None: introspect.node(app, path, subject),
        'tables': lambda: introspect.tables(app),
        'table': lambda name: introspect.table(app, name),
        'source': lambda plugin, file: introspect.source(app, plugin, file),
    }


def make_server(app: Any, port: int = DEFAULT_PORT, ui: Path = UI_DIR,
                restart: Optional[Callable[[], None]] = None) -> ThreadingHTTPServer:
    """The server, bound and not yet serving (port 0: any free one)."""
    api = routes(app)
    started = sources_signature(app)

    class Handler(BaseHTTPRequestHandler):
        server_version = 'kitebase-inspector'

        def log_message(self, format, *args):  # noqa: A002 - the base class names it
            pass

        def _host_ok(self) -> bool:
            bound = self.server.server_address[1]
            return self.headers.get('Host', '') in (f'localhost:{bound}', f'127.0.0.1:{bound}')

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, data: Any) -> None:
            self._send(status, json.dumps(data, ensure_ascii=False, default=str).encode('utf-8'),
                       'application/json; charset=utf-8')

        def do_GET(self):  # noqa: N802 - the name the base class calls
            if not self._host_ok():
                return self._send(403, b'Forbidden host', 'text/plain')
            url = urlparse(self.path)
            if url.path.startswith('/api/'):
                return self._api(url)
            return self._static(url.path)

        def do_POST(self):  # noqa: N802
            if not self._host_ok():
                return self._send(403, b'Forbidden host', 'text/plain')
            if urlparse(self.path).path == '/api/restart' and restart is not None:
                self._json(200, {'restarting': True})
                threading.Thread(target=restart, daemon=True).start()
                return
            self._json(404, {'error': 'not found'})

        def _api(self, url):
            parts = [unquote(p) for p in url.path[len('/api/'):].split('/') if p]
            if parts == ['version']:
                return self._json(200, {'stale': sources_signature(app) != started,
                                        'restart': restart is not None})
            fn = api.get(parts[0]) if parts else None
            if fn is None:
                return self._json(404, {'error': f'no such question: {url.path}'})
            query = {k: v[-1] for k, v in parse_qs(url.query).items()}
            try:
                return self._json(200, fn(*parts[1:], **query))
            except (KeyError, TypeError) as e:
                return self._json(404, {'error': f'not found: {e}'})
            except Exception as e:  # what broke is the answer the developer needs
                return self._json(500, {'error': f'{type(e).__name__}: {e}'})

        def _static(self, path: str):
            root = ui.resolve()
            target = (root / path.lstrip('/')).resolve()
            if not target.is_relative_to(root) or not target.is_file():
                # A route of the page, not a file: the page decides
                target = root / 'index.html'
            if not target.is_file():
                return self._send(503, b'The inspector page is not compiled: kitebase inspector build',
                                  'text/plain')
            kind = mimetypes.guess_type(target.name)[0] or 'application/octet-stream'
            self._send(200, target.read_bytes(), kind)

    return ThreadingHTTPServer(('127.0.0.1', port), Handler)


def serve(app: Any, port: int = DEFAULT_PORT, build_page: bool = True) -> None:
    """Serve the inspector of `app` until Ctrl-C."""
    if build_page:
        ensure_built()
    server: Optional[ThreadingHTTPServer] = None
    again = threading.Event()

    def restart() -> None:
        again.set()
        server.shutdown()   # serve_forever returns, and the main thread restarts

    try:
        server = make_server(app, port, restart=restart)
    except OSError as e:
        raise InspectorError(f'Port {port} is not free ({e.strerror}): --port to choose another')
    name = app.plugins.config.get('name') or 'application'
    print(f'Inspector of {name}: http://localhost:{port}   (Ctrl-C to stop)', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    if again.is_set():
        # The same command again, in this process: the application is composed anew
        print('inspector: the sources changed, starting again', flush=True)
        os.execv(sys.executable, [sys.executable] + sys.argv)


def dev(port: int = DEFAULT_PORT) -> int:
    """Develop the inspector itself: its Vite server, with /api sent to the server.

    The server is the application's (`kitebase inspector --no-build`, started
    beside it from the same directory); the page comes from Vite, with its
    reloads. Two ports, for whoever works on the inspector only.
    """
    pnpm = shutil.which('pnpm')
    if pnpm is None or not (SOURCE_DIR / 'package.json').exists():
        raise InspectorError('Developing the inspector needs its sources and pnpm.')
    if not (SOURCE_DIR / 'node_modules').exists():
        _run([pnpm, 'install'], SOURCE_DIR)
    backend = subprocess.Popen([sys.executable, '-m', 'kitebase', 'inspector', '--no-build',
                                '--port', str(port)])
    env = {**os.environ, 'KITEBASE_INSPECTOR_PORT': str(port)}
    try:
        return subprocess.call([pnpm, 'dev', '--port', str(DEV_PORT)], cwd=str(SOURCE_DIR), env=env)
    except KeyboardInterrupt:
        return 0
    finally:
        backend.terminate()
