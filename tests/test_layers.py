"""The modules of the core depend downwards.

Two things are held here. The spine of the application (context and registry,
then plugins and the model base, then schema and store, then the application
object) imports nothing from a later group, not even inside a function. And
the imports made inside a function to reach a module that sits higher are a
list: a new one fails, and one that goes away is taken off the list, so it
only gets shorter.
"""
import ast
from pathlib import Path
from typing import Dict, Set, Tuple

PACKAGE = Path(__file__).resolve().parent.parent / 'kitebase'
MODULES = {p.stem for p in PACKAGE.glob('*.py')} - {'__init__', '__main__'}

# Each group may import the ones before it, never the ones after.
SPINE = [
    {'context', 'registry', 'sqltypes'},
    {'plugins', 'model_base'},
    {'schema', 'store'},
    {'db'},
]

# importer -> the higher modules it reaches from inside a function.
KNOWN_UPWARD = {
    'access': {'endpoints'},
    'catalog': {'defaults', 'vocabulary'},
    'cli': {'db', 'db_copy', 'dev', 'diagnostics', 'help', 'help_texts', 'inspector',
            'pages', 'scaffold', 'schema_sync'},
    'db_copy': {'schema_sync'},
    'dev': {'plugins'},
    'diagnostics': {'pages'},
    'duplicate': {'endpoint_tree', 'pages'},
    'endpoint_db': {'endpoint_export'},
    'flow': {'access', 'endpoint_tree', 'endpoints', 'outbound', 'pages'},
    'help': {'endpoint_menu'},
    'help_texts': {'diagnostics'},
    'inspector': {'introspect'},
    'introspect': {'access', 'diagnostics', 'endpoints', 'pages'},
    'media': {'diagnostics'},
    'querybuilder': {'access', 'utils'},
    'settings': {'diagnostics'},
    'utils': {'access', 'media', 'model_base'},
    'vocabulary': {'diagnostics'},
}


def _imported(node: ast.AST) -> Set[str]:
    """The modules of the package an import statement names."""
    if isinstance(node, ast.Import):
        parts = [a.name.split('.') for a in node.names]
        return {p[1] for p in parts if p[0] == 'kitebase' and len(p) > 1 and p[1] in MODULES}
    if isinstance(node, ast.ImportFrom):
        parts = (node.module or '').split('.')
        if parts[0] != 'kitebase' and not node.level:
            return set()
        if len(parts) > 1 and parts[1] in MODULES:
            return {parts[1]}
        if node.level and parts[0] in MODULES:
            return {parts[0]}
        return {a.name for a in node.names if a.name in MODULES}
    return set()


def _graph() -> Dict[str, Tuple[Set[str], Set[str]]]:
    """module -> (imported at the top of the file, imported inside a function)."""
    graph = {}
    for name in sorted(MODULES):
        tree = ast.parse((PACKAGE / f'{name}.py').read_text())
        top: Set[str] = set()
        inside: Set[str] = set()
        for node in ast.walk(tree):
            (top if node in tree.body else inside).update(_imported(node))
        graph[name] = (top - {name}, inside - top - {name})
    return graph


def _depth(graph) -> Dict[str, int]:
    """How far a module is from the bottom, by the imports at the top of the files."""
    depth: Dict[str, int] = {}

    def of(name: str, path: Tuple[str, ...] = ()) -> int:
        if name not in depth:
            assert name not in path, f"import cycle: {' -> '.join(path + (name,))}"
            depth[name] = 1 + max((of(n, path + (name,)) for n in graph[name][0]), default=-1)
        return depth[name]

    for name in graph:
        of(name)
    return depth


def test_the_spine_imports_downwards_only():
    graph = _graph()
    for i, group in enumerate(SPINE):
        later = set().union(*SPINE[i + 1:]) if i + 1 < len(SPINE) else set()
        for name in sorted(group):
            top, inside = graph[name]
            assert not (top | inside) & later, (
                f"kitebase.{name} imports {sorted((top | inside) & later)}, which sit above it")


def test_the_bottom_of_the_spine_imports_nothing_of_the_package():
    graph = _graph()
    for name in sorted(SPINE[0]):
        assert graph[name] == (set(), set()), f"kitebase.{name} imports {graph[name]}"


def test_imports_that_reach_upwards_are_the_known_ones():
    graph = _graph()
    depth = _depth(graph)
    upward = {name: {n for n in inside if depth[n] >= depth[name]}
              for name, (_, inside) in graph.items()}
    upward = {name: found for name, found in upward.items() if found}

    new = {n: sorted(f - KNOWN_UPWARD.get(n, set())) for n, f in upward.items()
           if f - KNOWN_UPWARD.get(n, set())}
    gone = {n: sorted(k - upward.get(n, set())) for n, k in KNOWN_UPWARD.items()
            if k - upward.get(n, set())}
    assert not new, f"new imports of a higher module from inside a function: {new}"
    assert not gone, f"no longer there, take them off KNOWN_UPWARD: {gone}"
