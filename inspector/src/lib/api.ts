/**
 * The questions the page asks the server (kitebase/inspector.py), and the
 * shapes of the answers (kitebase/introspect.py). The answers describe the
 * application as it runs now: nothing is cached beyond the view that shows it.
 */

export type Severity = 'error' | 'warning' | 'info';

export interface Issue {
	severity: Severity;
	code: string;
	path: string;
	message: string;
	plugin: string | null;
}

export interface PluginInfo {
	name: string;
	version: string | null;
	description: string | null;
	depends_on: string[];
	dir: string;
	data_files: string[];
	sources: string[];
}

export interface AppSummary {
	app: { name?: string; version?: string; description?: string };
	root: string;
	plugins: PluginInfo[];
	counts: Record<string, number>;
}

/** What one plugin wrote at a place of one file, with its lines. */
export interface Layer {
	plugin: string;
	file: string;
	declared: unknown;
	line?: number;
	end?: number;
	text?: string;
}

export interface TypeStep {
	name: string;
	builtin: boolean;
	plugin: string | null;
	layers: Layer[];
}

export interface ColumnInfo {
	name: string;
	attributes: Record<string, unknown>;
	type: string | null;
	types: TypeStep[];
	origin: { kind: 'table' } | { kind: 'mixin'; type: string | null };
	layers: Layer[];
}

export interface TableSummary {
	name: string;
	label: string | null;
	plugins: string[];
	columns: number;
	mixins: string[];
}

export interface TableInfo {
	name: string;
	attributes: Record<string, unknown>;
	plugins: string[];
	layers: Layer[];
	columns: ColumnInfo[];
	used_by: { foreign_keys: string[]; views: string[]; pages: string[] };
}

export interface EndpointInfo {
	module: string;
	doc: string;
	docstring: string;
	file?: string;
	line?: number;
}

export interface CatalogKey {
	type?: string;
	form?: string;
	list?: boolean;
	values?: string[];
	ref?: string;
	tokens?: string;
	derived?: boolean;
	doc?: string;
	see?: string;
}

export interface Catalog {
	roots: Record<string, string>;
	forms: Record<string, { doc?: string; see?: string; keys: Record<string, CatalogKey> }>;
	directives: Record<string, { family: string; doc: string; see?: string }>;
	tokens: Record<string, { doc: string; registry?: string; see?: string; names: Record<string, string> }>;
	uses: Record<string, { path: string; plugin: string | null; value: unknown }[]>;
}

/** A root of the merge: `tables`, `pages`, `roles`... */
export interface RootSummary {
	name: string;
	kind: 'map' | 'list' | 'value';
	count: number;
	plugins: string[];
}

export interface NodeChild {
	key: string;
	path: string;
	kind: 'map' | 'list' | 'value';
	summary: unknown;
	plugins: string[];
}

/** A node of the merged tree as it runs, and the plugins that write it. */
export interface NodeInfo {
	path: string;
	trail: { key: string; path: string }[];
	kind: 'map' | 'list' | 'value';
	value: unknown;
	plugin: string | null;
	children: NodeChild[];
	layers: Layer[];
	layers_of: string;
	/** A page or a view resolved, and as a subject of the guard receives it. */
	resolved: {
		subjects: string[];
		subject: string | null;
		refused?: string[];
		value?: unknown;
		error?: string;
	} | null;
}

export interface SourceFile {
	plugin: string;
	file: string;
	text: string;
}

async function get<T>(path: string): Promise<T> {
	const res = await fetch(`/api/${path}`);
	const body = await res.json();
	if (!res.ok) throw new Error(body?.error ?? `${res.status}`);
	return body as T;
}

export const api = {
	app: () => get<AppSummary>('app'),
	issues: () => get<Issue[]>('issues'),
	catalog: () => get<Catalog>('catalog'),
	endpoints: () => get<Record<string, EndpointInfo>>('endpoints'),
	roots: () => get<RootSummary[]>('roots'),
	node: (path: string, subject?: string) =>
		get<NodeInfo>(`node?path=${encodeURIComponent(path)}${subject ? `&subject=${encodeURIComponent(subject)}` : ''}`),
	tables: () => get<TableSummary[]>('tables'),
	table: (name: string) => get<TableInfo>(`table/${encodeURIComponent(name)}`),
	source: (plugin: string, file: string) =>
		get<SourceFile>(`source/${encodeURIComponent(plugin)}/${encodeURIComponent(file)}`),
	version: () => get<{ stale: boolean; restart: boolean }>('version'),
	restart: () => fetch('/api/restart', { method: 'POST' })
};

/** A link that opens the file at the line in the editor, for whoever has VS Code. */
export function editorLink(file: string, line?: number): string {
	return `vscode://file${file}${line ? `:${line}` : ''}`;
}
