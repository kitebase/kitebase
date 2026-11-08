/**
 * Where the page is: the hash, `#/tables/Book`. A hash keeps every view a link
 * that can be kept or sent, and needs nothing from the server that serves it.
 */

function read(): string[] {
	return location.hash.replace(/^#\/?/, '').split('/').filter(Boolean).map(decodeURIComponent);
}

class Router {
	parts = $state<string[]>(read());

	constructor() {
		addEventListener('hashchange', () => (this.parts = read()));
	}
}

export const router = new Router();

export function href(...parts: string[]): string {
	return '#/' + parts.map(encodeURIComponent).join('/');
}
