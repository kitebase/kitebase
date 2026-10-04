import sys
import os
import importlib
import importlib.util
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union, Any
import yaml
from kitebase import apptime
from kitebase.utils import deep_merge

# Merge directives. They belong to the YAML protocol, not to a single function:
# every one of them is consumed while merging and never reaches a consumer.
REPLACE = '$replace'   # on a dict: the listed keys supersede, they do not merge
REMOVE = '$remove'     # on a list item, or on a dict value: drop it
AFTER = '$after'
BEFORE = '$before'

_CORE_TIMESTAMP: Optional[float] = None


def expand_shorthand(node: Any) -> Any:
    """
    Expand the shorthand forms of a plugin's YAML, in place.

    Today there is one: inside a `fields:` list, a bare string is the field of
    that name, so the common case — a field with nothing to say about itself —
    costs one token instead of a nested mapping.

    Expanded **as the file is read**, before the merge, and that is the whole
    point: the merge indexes a list by the identity of its items (`name` among
    them), so a field written short is still addressable by a derived plugin
    (`$after: name`). Left to the client, the shorthand would have bought
    brevity by making a form unmergeable — and a list of bare strings would fall
    back to append semantics without anyone asking for it.

    `- filler:` stays a mapping: a bare `filler` could not be told apart from a
    field so named.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            if key == 'fields' and isinstance(value, list):
                node[key] = [{'name': i} if isinstance(i, str) else i for i in value]
            expand_shorthand(node[key])
    elif isinstance(node, list):
        for item in node:
            expand_shorthand(item)
    return node


def _core_timestamp() -> float:
    """
    Newest modification time among the core modules, computed once.

    Only the package's own top-level files: the generator and the schema live
    there, while `locale/` holds translations that cannot change a model.
    """
    global _CORE_TIMESTAMP
    if _CORE_TIMESTAMP is None:
        core = Path(__file__).parent
        _CORE_TIMESTAMP = max((f.stat().st_mtime for f in core.glob('*.py')), default=0.0)
    return _CORE_TIMESTAMP


class PluginsManager:
    """
    Manages the loading, dependency resolution, and merging of plugins.

    This class handles:
    - Plugin discovery and loading from filesystem
    - Plugin dependency resolution
    - Configuration management
    - Data merging from multiple plugins
    - History tracking of all plugin operations
    """

    def __init__(self, logger_name: str = 'kitebase') -> None:
        """
        Initialize the plugin manager.

        Args:
            logger_name: Name for the logger instance
        """
        self.history: Dict[str, List[str]] = {}
        self.issues: List[Dict[str, Any]] = []
        self.data: Dict[str, Any] = {}
        self.config: Dict[str, Any] = {}
        self.plugins: Dict[str, 'Plugin'] = {}
        self.sorted: List[str] = []
        self.merge_handlers: Dict[str, Any] = {}
        # The application config, once loaded: an input to what is generated.
        self.config_file: Optional[Path] = None

        # Everything an application declares by a relative path — plugin roots,
        # the sqlite file, the log, the generated model — hangs from the
        # directory of its config.yaml, not from the current one. An app is
        # then startable from anywhere: a service, a cron job, a command run
        # from somewhere else. Set for real by load_config().
        self.app_root: Path = Path.cwd()

        # The library speaks, it does not listen: no handler, no level. An
        # application that wants the lines calls server_utils.setup_logging.
        self.logger = logging.getLogger(logger_name)

    def add_issue(self, severity: str, code: str, path: str, message: str,
                  plugin: Optional[str] = None) -> None:
        """
        Record a load-time issue (same format as kitebase.diagnostics).

        Issues are collected here during merge and picked up later by
        diagnostics.run_checks(). Exact duplicates are skipped.

        Args:
            severity: 'error', 'warning' or 'info'
            code:     Short machine-readable code (e.g. 'merge-overlap')
            path:     Dot-notation path of the affected key
            message:  Human-readable description
            plugin:   Name of the plugin that triggered the issue
        """
        issue = {'severity': severity, 'code': code, 'path': path,
                 'message': message, 'plugin': plugin}
        if issue not in self.issues:
            self.issues.append(issue)

    def register_merge_handler(self, pattern: str, handler: Any) -> None:
        """
        Register a custom merge handler for a specific data path pattern.

        Args:
            pattern: Dot-notation path pattern (supports wildcards like 'tables.*.columns')
            handler: Callable that takes (base_list, new_list, plugin) and returns merged list
        """
        self.merge_handlers[pattern] = handler

    def _get_merge_handler(self, key_path: str) -> Optional[Any]:
        """
        Find a merge handler for the given key path.

        Args:
            key_path: Complete dot-notation path

        Returns:
            Handler function if found, None otherwise
        """
        import fnmatch

        # Exact match first
        if key_path in self.merge_handlers:
            return self.merge_handlers[key_path]

        # Pattern match with wildcards
        for pattern, handler in self.merge_handlers.items():
            if fnmatch.fnmatch(key_path, pattern):
                return handler

        return None

    def load_config(self, config: Union[str, Path] = "config.yaml") -> None:
        """
        Load global configuration from YAML file.

        Args:
            config: Path to configuration file
        """
        self.config = {
            "name": "myapp",
            "version": '',
            "description": "",
            "author": "",
            "license": "",
            "plugins": ['plugins'],
            "db_engine": "",
            "timezone": "",
        }
        self.app_root = Path(config).resolve().parent
        self.config_file = Path(config).resolve()

        with open(config) as f:
            data = yaml.safe_load(f)
        deep_merge(self.config, data)

        # The timezone the stored naive datetimes are written in. Declaring it
        # is what asks for the guarantee: from here on the clock is read
        # through it, and a process whose own clock disagrees does not start.
        # See kitebase.apptime for why an ambient timezone is the dangerous one.
        apptime.set_app_timezone(self.config['timezone'])
        apptime.check_process_timezone()

        # `log_file` used to redirect this logger alone to a file, truncated at
        # every start. Where the log goes is the process's choice, made once
        # for every logger (server_utils.setup_logging, from the environment):
        # a file named here would be a second, narrower answer to the same
        # question, so it is not honoured — and said, not ignored.
        if self.config.get('log_file'):
            self.logger.warning(
                "config.yaml 'log_file' is no longer honoured: the log is configured "
                "by the process (server_utils.setup_logging), not by the library.")

    def resolve_path(self, path: Union[str, Path]) -> Path:
        """
        Resolve a path the application declared, against its own directory.

        An absolute path is left alone; a relative one hangs from the directory
        holding config.yaml. resolve() also normalises out-of-tree roots
        ('../plugins') to a single canonical form, so sys.path never holds the
        same directory twice under two spellings.

        Args:
            path: absolute or relative to the app directory

        Returns:
            The absolute, normalised path
        """
        return (self.app_root / Path(path)).resolve()

    def load_plugins(self) -> None:
        """
        Load and initialize all plugins from configured directories.

        Raises:
            ValueError: If plugin directory doesn't exist or duplicate plugin names found
        """
        if not self.config:
            self.load_config()

        # Discover plugins — every plugin in every root, as before. Selection
        # happens afterwards, over the complete picture: a root's `include` can
        # then pull a dependency that lives in a different root, and there is
        # one notion of "what depends on what" rather than one per root.
        found: Dict[str, List[Plugin]] = {}
        chosen: Dict[str, Plugin] = {}
        for declared, include in self._plugin_roots():
            plugins_dir = self.resolve_path(declared)
            if not plugins_dir.exists():
                raise ValueError(
                    f"The plugins folder: {declared} does not exist "
                    f"(looked in {plugins_dir})")

            # Add plugin directory to Python path for imports, in the single
            # canonical spelling resolve_path() produces: '..' and duplicate
            # entries would import the same module twice under two names.
            sys.path.append(str(plugins_dir))

            # Scan for plugin directories
            in_root: Dict[str, Plugin] = {}
            for plugin_dir in plugins_dir.iterdir():
                if plugin_dir.is_dir():
                    config_file = plugin_dir / "config.yaml"
                    if not config_file.exists():
                        continue  # Not a plugin directory

                    plugin = Plugin(plugin_dir)
                    if plugin.name in in_root:
                        raise ValueError(
                            f"Duplicate plugin name: {plugin.name} "
                            f"({in_root[plugin.name].plugin_dir}, {plugin_dir})")
                    in_root[plugin.name] = plugin
                    found.setdefault(plugin.name, []).append(plugin)

            if include is None:
                take = list(in_root)        # whole root
            else:
                unknown = [n for n in include if n not in in_root]
                if unknown:
                    raise ValueError(
                        f"Plugins not found in root {plugins_dir}: "
                        f"{', '.join(sorted(unknown))} "
                        f"(available: {', '.join(sorted(in_root)) or 'none'})")
                take = include

            # An explicit request carries its root with it, so two roots asked
            # for the same name is a conflict — but only then. Two roots may
            # well hold alternative plugins under one name: naming the one you
            # want in `include` is how you choose, and the other stays inert.
            for name in take:
                if name in chosen:
                    raise ValueError(
                        f"Duplicate plugin name: {name} "
                        f"({chosen[name].plugin_dir}, {in_root[name].plugin_dir}) "
                        f"— use 'include' to name which root it comes from")
                chosen[name] = in_root[name]

        # Keep what was asked for plus what it references, drop the rest.
        # Restored to discovery order (root order, then directory order): the
        # topological sort is only a partial order, so the order plugins are
        # registered in decides deep_merge precedence between independent ones.
        # Selection must narrow that sequence, never reshuffle it.
        keep = self._select(found, chosen)
        self.plugins = {name: keep[name] for name in found if name in keep}

        # Process plugins in dependency order
        self._sort_dependencies()

        # Merge plugin data
        for name in self.sorted:
            plugin = self.plugins[name]
            for data in plugin.data:
                self.merge_dicts(data, name)

    def _plugin_roots(self) -> List[Tuple[Path, Optional[List[str]]]]:
        """
        Normalise the `plugins:` config entries into (path, include) pairs.

        A root is either a bare path — take every plugin it holds — or a
        mapping that also names the plugins wanted from it:

            plugins:
              - path: ../../commons/plugins
                include: [common, users, partners]
              - plugins                              # short form = whole root

        Returns:
            List of (Path, include) pairs; include is None for a whole root

        Raises:
            ValueError: If an entry is malformed
        """
        roots: List[Tuple[Path, Optional[List[str]]]] = []
        for entry in self.config['plugins']:
            if isinstance(entry, dict):
                path = entry.get('path')
                if not path:
                    raise ValueError(f"Plugin root entry has no 'path': {entry}")
                include = entry.get('include')
                if include is not None and not isinstance(include, list):
                    raise ValueError(
                        f"'include' for plugin root {path} must be a list, "
                        f"got {type(include).__name__}")
                unknown_keys = set(entry) - {'path', 'include'}
                if unknown_keys:
                    raise ValueError(
                        f"Unknown keys in plugin root {path}: "
                        f"{', '.join(sorted(unknown_keys))}")
            else:
                path, include = entry, None
            roots.append((Path(path), include))
        return roots

    @staticmethod
    def _depends_on(plugin: 'Plugin') -> List[str]:
        """Names a plugin depends on, normalised to a list."""
        deps = plugin.config.get('depends_on', []) or []
        return [deps] if isinstance(deps, str) else list(deps)

    @classmethod
    def _select(cls, found: Dict[str, List['Plugin']],
                chosen: Dict[str, 'Plugin']) -> Dict[str, 'Plugin']:
        """
        Keep the plugins asked for and everything they reference; drop the rest.

        Inclusion is positive on purpose: what a shared root gains over time
        stays inert until an application asks for it by name. An exclusion list
        would do the opposite — pulling an updated shared repository would hand
        every consumer the new plugin, and its tables, unasked.

        Dependencies are followed here rather than declared, so
        `include: [partners]` also brings `common`, and the list survives a
        plugin growing a new dependency. Following them over every root, rather
        than root by root, is what lets a shared plugin depend on one provided
        elsewhere. A referenced name nobody provides is left for
        _sort_dependencies(), which stays the single place that reports a
        missing dependency.

        Args:
            found: Every discovered plugin, by name — a list per name, since a
                   name may exist in more than one root
            chosen: Plugins asked for explicitly, already resolved to the root
                    that was asked

        Returns:
            The selected plugins, by name

        Raises:
            ValueError: If a dependency name is provided by more than one root
        """
        keep: Dict[str, Plugin] = {}
        pending = list(chosen)
        while pending:
            name = pending.pop()
            if name in keep:
                continue
            plugin = chosen.get(name)
            if plugin is None:
                # Reached as a dependency: nobody named a root for it, so it
                # must be unambiguous.
                matches = found.get(name)
                if not matches:
                    continue  # unprovided — _sort_dependencies() reports it
                if len(matches) > 1:
                    paths = ', '.join(str(p.plugin_dir) for p in matches)
                    raise ValueError(
                        f"Dependency '{name}' is provided by more than one "
                        f"root ({paths}) — include it explicitly from the one "
                        f"you mean")
                plugin = matches[0]
            keep[name] = plugin
            pending.extend(cls._depends_on(plugin))
        return keep

    def _sort_dependencies(self) -> None:
        """
        Sort plugins based on their dependencies using Kahn's topological sort algorithm.

        Raises:
            ValueError: If dependencies are missing or circular dependencies detected
        """
        # Create dependency graph
        dependencies: Dict[str, set] = {}
        for name, value in self.plugins.items():
            dependencies[name] = set(self._depends_on(value))

        # Validate dependencies
        all_items = set(dependencies.keys())
        for item, deps in dependencies.items():
            unknown_deps = deps - all_items
            if unknown_deps:
                raise ValueError(f"Not found dependence for {item}: {unknown_deps}")

        # Perform topological sort
        result = []
        no_deps = [k for k, v in dependencies.items() if not v]

        while no_deps:
            current = no_deps.pop(0)
            result.append(current)

            # Remove current node from all dependencies
            for item, deps in dependencies.items():
                if current in deps:
                    deps.remove(current)
                    if not deps:
                        no_deps.append(item)

        # Check for circular dependencies
        if len(result) != len(dependencies):
            remaining = set(dependencies.keys()) - set(result)
            raise ValueError(f"Circular dependence found between: {remaining}")

        self.sorted = result

    def merge_dicts(self, d: Dict[str, Any], plugin: str) -> Dict[str, Any]:
        """
        Entry point for merging a new dictionary into existing data.

        Args:
            d: New dictionary to merge
            plugin: Name of source plugin

        Returns:
            Merged dictionary
        """
        self.data = self._recursive_merge(self.data, d, plugin)
        return self.data

    def _recursive_merge(self,
                         base: Dict[str, Any],
                         new: Dict[str, Any],
                         plugin: str,
                         current_path: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        Recursively merge dictionaries while preserving plugin information.

        Args:
            base: Base dictionary to merge into
            new: New dictionary to merge from
            plugin: Source plugin name
            current_path: Current path in the dictionary hierarchy

        Returns:
            Merged dictionary

        Raises:
            TypeError: If incompatible types are found during merge
        """
        if current_path is None:
            current_path = []

        result: Dict[str, Any] = {}

        # Copy base dictionary
        for key in base:
            result[key] = base[key]

        # Keys this contribution supersedes instead of refining. Merging and
        # starting over are both legitimate, and until this existed they were
        # indistinguishable: whatever the author meant, the machinery appended.
        replaced = new.get(REPLACE)
        if replaced is not None and not isinstance(replaced, list):
            raise TypeError(
                f"'{REPLACE}' takes a list of key names, got {type(replaced).__name__}"
                f" at '{self._build_key_path(current_path, REPLACE)}'")
        replaced = set(replaced or ())

        # Process new dictionary
        for key in new:
            if key == REPLACE:
                continue          # a directive, never data

            key_path = self._build_key_path(current_path, key)
            self._add_to_history(key_path, plugin)

            # `$remove` on a dict value: the list form has existed since smart
            # merge, and its absence here meant a section keyed by id — a menu
            # item, a page — could be redefined but never dropped, while the
            # directive itself travelled to the client as data.
            if isinstance(new[key], dict) and new[key].get(REMOVE) is True:
                result.pop(key, None)
                continue

            if key in replaced:
                result[key] = new[key]
                continue

            if key in base:
                v1, v2 = base[key], new[key]

                # Validate type compatibility
                if type(v1) is not type(v2):
                    raise TypeError(
                        f"Incompatible types for key '{key_path}' between existing and {plugin}: "
                        f"{type(v1).__name__} vs {type(v2).__name__}"
                    )

                if isinstance(v1, dict):
                    result[key] = self._recursive_merge(v1, v2, plugin, current_path + [key])
                    if '$plugin' in v1:
                        result[key]['$plugin'] = v1['$plugin']
                    else:
                        result[key]['$plugin'] = plugin

                elif isinstance(v1, list):
                    # Check if there's a custom merge handler for this path
                    handler = self._get_merge_handler(key_path)
                    if handler:
                        result[key] = handler(v1, v2, plugin)
                    else:
                        result[key] = self._merge_lists(v1, v2, plugin, key_path)
                else:
                    self.logger.warning(f"[{plugin}] Overlapping value for key '{key_path}': {v1} -> {v2}")
                    self.add_issue('info', 'merge-overlap', key_path,
                                   f"value overridden: {v1!r} -> {v2!r}", plugin)
                    result[key] = v2
            else:
                if isinstance(new[key], dict):
                    result[key] = self._recursive_merge({}, new[key], plugin, current_path + [key])
                    result[key]['$plugin'] = plugin
                elif isinstance(new[key], list):
                    result[key] = new[key]
                    for item in result[key]:
                        if isinstance(item, dict) and '$plugin' not in item:
                            item['$plugin'] = plugin
                else:
                    result[key] = new[key]

        return result

    # Identity keys checked in priority order to find how a list item is identified.
    _IDENTITY_KEYS = ('id', 'name', 'field', 'group')


    def _detect_identity_key(self, items: list) -> Optional[str]:
        """
        Return the identity key for a list of dicts, or None for scalar/unkeyed lists.

        Scans the first dict item for the first key in _IDENTITY_KEYS.
        Pure string lists and dicts without a recognised identity key are treated
        as plain sequences (append semantics, unchanged from previous behaviour).
        """
        for item in items:
            if isinstance(item, dict):
                for k in self._IDENTITY_KEYS:
                    if k in item:
                        return k
                return None  # dict items without a recognised identity key
        return None  # all items are scalars

    def _merge_lists(self, base: list, new: list, plugin: str, key_path: str) -> list:
        """
        Merge two lists using identity-aware semantics when possible, plain append otherwise.

        Identity-aware merge (triggered when list items are dicts with a known identity key):
          - same identity           → deep-merge the item's properties
          - $remove: true           → drop the item from the result
          - $after: <id> / $before: <id>  → insert at the given position
          - new identity            → append at the end

        The $remove / $after / $before directives are consumed here and never
        appear in the resolved descriptor sent to the frontend.

        Plain-append fallback (string lists, unkeyed dicts):
          result = base + [item for item in new if item not in base]
        """
        id_key = self._detect_identity_key(base) or self._detect_identity_key(new)

        if id_key is None:
            # Plain sequence: extend without duplicates (original behaviour).
            #
            # Appending is right for a list of values and almost never what the
            # author meant for a list of *nodes*: without an identity there is no
            # way to say which one this contribution refines, so a second plugin
            # gets its section appended instead of merged — and nothing says so.
            # The warning fires only where two plugins actually meet on the same
            # list, which is the only place the ambiguity exists.
            if base and new:
                self.add_issue(
                    'warning', 'merge-unkeyed-list', key_path,
                    f"'{plugin}' contributes to a list whose items carry no identity "
                    f"({', '.join(self._IDENTITY_KEYS)}): the items are appended and cannot "
                    f"refine the ones already there. Give them an 'id' to make them addressable.",
                    plugin)
            merged = base + [item for item in new if item not in base]
            return merged


        # Build an ordered index of base items keyed by identity value.
        # Use a list of (identity, item) pairs to preserve insertion order.
        index: Dict[str, Any] = {}   # identity → item dict
        order: list = []             # identity values in base order

        for item in base:
            if isinstance(item, dict):
                identity = item.get(id_key)
                if identity is not None:
                    index[identity] = dict(item)
                    order.append(identity)
                else:
                    # A dict with no identity in an otherwise-identified list is
                    # positional and belongs to nobody — `- filler:` in a form
                    # column. It used to be dropped the moment a second plugin
                    # merged into the list: the layout lost a line break, and
                    # nothing said so.
                    order.append(item)
            else:
                # Scalar mixed into an otherwise-identified list — keep as-is
                order.append(item)

        # Deferred positional inserts: list of (anchor_id, position, item)
        deferred: list = []

        for item in new:
            if not isinstance(item, dict):
                if item not in order:
                    order.append(item)
                continue

            identity = item.get(id_key)
            remove   = item.get('$remove', False)
            after    = item.get('$after')
            before   = item.get('$before')

            # Strip merge directives from the item before storing
            clean = {k: v for k, v in item.items() if k not in ('$remove', '$after', '$before')}
            clean.setdefault('$plugin', plugin)

            if remove:
                if identity in index:
                    del index[identity]
                    order.remove(identity)
                continue

            if identity in index:
                # Deep-merge properties into existing item
                existing = index[identity]
                item_path = f'{key_path}[{identity}]'
                for k, v in clean.items():
                    if k == id_key:
                        continue
                    if isinstance(v, dict) and isinstance(existing.get(k), dict):
                        existing[k] = self._recursive_merge(existing[k], v, plugin, [item_path, k])
                    elif isinstance(v, list) and isinstance(existing.get(k), list):
                        # A list property goes back through the same door instead
                        # of being overwritten. Without this the merge stopped at
                        # the first list, which in a form layout is immediately:
                        # `layout → section.columns → column.fields` are lists all
                        # the way down, so refining a section meant losing the
                        # fields it already had.
                        existing[k] = self._merge_lists(existing[k], v, plugin, f'{item_path}.{k}')
                    else:
                        existing[k] = v
                existing['$plugin'] = plugin
            else:
                # New item
                index[identity] = clean
                if after or before:
                    deferred.append((after, before, identity))
                else:
                    order.append(identity)

        # Apply deferred positional inserts
        for (after, before, identity) in deferred:
            anchor = after or before
            if anchor in order:
                pos = order.index(anchor)
                order.insert(pos + 1 if after else pos, identity)
            else:
                self.logger.warning(
                    f"[{plugin}] Positional anchor '{anchor}' not found in '{key_path}', appending '{identity}'"
                )
                self.add_issue('warning', 'merge-anchor-missing', key_path,
                               f"positional anchor '{anchor}' not found, '{identity}' appended", plugin)
                order.append(identity)

        # Rebuild the ordered list
        result = []
        for entry in order:
            if isinstance(entry, str) and entry in index:
                result.append(index[entry])
            elif not isinstance(entry, str):
                result.append(entry)  # scalar passthrough
        return result

    def _add_to_history(self, key_path: str, plugin: str) -> None:
        """
        Track the history of key definitions by plugin.

        Args:
            key_path: Complete path to the key
            plugin: Plugin defining the key
        """
        if key_path not in self.history:
            self.history[key_path] = []
        self.history[key_path].append(plugin)

    def _build_key_path(self, current_path: List[str], key: str) -> str:
        """
        Build the complete path for a key in the dictionary hierarchy.

        Args:
            current_path: Current position in hierarchy
            key: Current key name

        Returns:
            Complete dot-notation path to key
        """
        if current_path:
            return f"{'.'.join(current_path)}.{key}"
        return key

    def export_pythonpath(self, windows: bool = os.name == 'nt') -> str:
        """
        Prepare a string for environment settings for linux/mac or windows.

        Args:
            windows: True for windows settings, False for Unix/Linux
                    (defaults to current OS)

        Returns:
            The string for environment script
        """
        env = ""
        for plugins_dir, _include in self._plugin_roots():
            abs_dir = str(self.resolve_path(plugins_dir))
            if windows:
                s = f'set PYTHONPATH="{abs_dir}";%PYTHONPATH%\n'
                env += s.replace("/", "\\")
            else:
                s = f'export PYTHONPATH="{abs_dir}:$PYTHONPATH"\n'
                env += s.replace("\\", "/")
        return env

    def get_timestamp(self) -> float:
        """
        Get the most recent timestamp among the inputs of what is generated.

        The plugins are one input; **the generator is the other**. A change to
        `source.py` or `db.py` changes the model produced from the very same
        YAML, so leaving the package out means a `git pull` that alters the
        generation leaves a stale `model.py` in place — with the new schema
        already in the database. The failure is confusing and silent, which is
        why the package counts.

        **The application config is the third.** It decides which plugin roots
        are read and which plugins are taken from them, so adding a shared root
        changes the model without touching a single plugin file — and a root
        cloned last week is older than the model generated yesterday. Left out,
        that edit regenerates nothing and the schema check answers "aligned"
        against a model that never saw the new tables.

        Returns:
            float: The timestamp of the most recent input: config, plugins, core
        """
        latest_timestamp = _core_timestamp()
        if self.config_file is not None and self.config_file.is_file():
            latest_timestamp = max(latest_timestamp, self.config_file.stat().st_mtime)
        for name, plugin in self.plugins.items():
            if plugin.timestamp > latest_timestamp:
                latest_timestamp = plugin.timestamp

        return latest_timestamp

    def get_formatted_timestamp(self) -> str:
        """
        Get the most recent timestamp from all plugins as a formatted date string.

        Returns:
            str: The formatted date of the most recent file
        """
        timestamp = self.get_timestamp()
        if timestamp > 0:
            return datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M:%S')
        return "Unknown"

    def should_regenerate(self, filename: str) -> bool:
        """
        Determine if a file should be regenerated by comparing its timestamp
        with the most recent timestamp among all plugins.

        Args:
            filename: Path to the file to check

        Returns:
            bool: True if the file must be regenerated
        """
        # If file doesn't exist, it needs to be generated
        path = self.resolve_path(filename)
        if not path.exists():
            return True

        # Compare timestamps
        file_timestamp = path.stat().st_mtime
        plugins_timestamp = self.get_timestamp()
        return file_timestamp < plugins_timestamp

    def get(self, path: str) -> Any:
        """
        Get a value from plugins.data by dotted path.

        Args:
            path: Dot-notation path, e.g. "views.book_list_view"

        Returns:
            The value at that path, or None if not found
        """
        current = self.data
        for part in path.split('.'):
            if not isinstance(current, dict):
                return None
            current = current.get(part)
            if current is None:
                return None
        return current

    def resolve_refs(self, obj: Any, _seen: Optional[frozenset] = None,
                     plugin_context: Optional[str] = None) -> Any:
        """
        Recursively resolve all ``$ref`` fields in obj.

        A dict containing ``$ref: "section.key"`` is replaced by the object
        at that path in plugins.data.  Any sibling keys are merged on top
        of the resolved value (allowing local overrides).

        Relative refs (section.id) are qualified using the plugin_context
        derived from $plugin metadata on the containing dict.

        Args:
            obj: Object to resolve (dict, list, or scalar)
            _seen: Internal set of already-visited ref paths (cycle detection)
            plugin_context: Plugin namespace for relative $ref resolution

        Returns:
            Object with all refs resolved

        Raises:
            ValueError: If a circular ref is detected
        """
        if _seen is None:
            _seen = frozenset()

        if isinstance(obj, dict):
            current_plugin = obj.get('$plugin', plugin_context)

            if '$ref' in obj and isinstance(obj['$ref'], str):
                ref_path = obj['$ref']
                if ref_path in _seen:
                    raise ValueError(f"Circular ref detected: {ref_path}")
                target = self.get(ref_path)
                if target is None:
                    self.logger.warning(f"Unresolved ref: '{ref_path}'")
                    return obj
                target_plugin = target.get('$plugin', current_plugin) if isinstance(target, dict) else current_plugin
                resolved = self.resolve_refs(target, _seen | {ref_path}, target_plugin)
                # Merge sibling keys on top of the resolved object
                if isinstance(resolved, dict):
                    result = dict(resolved)
                    for k, v in obj.items():
                        if k != '$ref':
                            result[k] = self.resolve_refs(v, _seen | {ref_path}, current_plugin)
                    return result
                return resolved

            return {k: self.resolve_refs(v, _seen, current_plugin) for k, v in obj.items()}

        if isinstance(obj, list):
            return [self.resolve_refs(item, _seen, plugin_context) for item in obj]

        return obj

    def load_locale(self, locale: str) -> None:
        """
        Load translations for the given locale from the core library and all plugins.

        - Core: kitebase.locale.{locale}  (standard importlib)
        - Plugins: {plugin_dir}/locale/{locale}.py  (spec_from_file_location, no __init__ needed)

        Call after load_plugins(). Safe to call with locale='en' (no-op).
        """
        if locale == 'en':
            return

        # Core library translations
        try:
            importlib.import_module(f'kitebase.locale.{locale}')
        except ModuleNotFoundError:
            pass

        # Plugin translations in dependency order
        for name in self.sorted:
            plugin = self.plugins[name]
            locale_file = plugin.plugin_dir / 'locale' / f'{locale}.py'
            if not locale_file.exists():
                continue
            spec = importlib.util.spec_from_file_location(
                f'_kitebase_locale_{name}_{locale}', locale_file
            )
            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)  # type: ignore[union-attr]
                self.logger.info(f'[i18n] Loaded {locale_file}')

    def load_all_locales(self) -> None:
        """
        Load every locale file found in the core library and all plugins.

        Scans kitebase/locale/*.py and {plugin_dir}/locale/*.py for each plugin,
        then calls load_locale() for each unique locale found.
        Replaces load_locale(single_locale) when multi-language support is needed.
        """
        core_locale_dir = Path(__file__).parent / 'locale'
        dirs: List[Path] = [core_locale_dir]
        for name in self.sorted:
            dirs.append(self.plugins[name].plugin_dir / 'locale')

        loaded: set = set()
        for d in dirs:
            if not d.exists():
                continue
            for f in sorted(d.glob('*.py')):
                if f.stem != '__init__' and f.stem not in loaded:
                    self.load_locale(f.stem)
                    loaded.add(f.stem)

    def get_sources(self, to_str: bool = False) -> List[Path]:
        """
        Get a list of all Python source files from all plugins.

        Args:
            to_str: if needed transform the Path object to string

        Returns:
            List[Path]: List of all Python source files
        """
        sources = []
        for name, plugin in self.plugins.items():
            sources.extend(plugin.sources)
        if to_str:
            return [str(s) for s in sources]
        return sources


class Plugin:
    """
    Represents a single plugin with its configuration and associated files.

    A plugin consists of:
    - Configuration file (config.yaml)
    - Data files (additional YAML files)
    - Python source files
    - Other resource files
    """

    def __init__(self, plugin_dir: Path) -> None:
        """
        Initialize a plugin from its directory.

        Args:
            plugin_dir: Path to plugin directory
        """
        # Load plugin configuration with defaults
        self.config: Dict[str, Any] = {
            "name": plugin_dir.name,
            "version": '0.0.1',
            "description": "",
            "author": "",
            "license": "",
            "depends_on": [],
        }

        with open(plugin_dir / "config.yaml") as f:
            data = yaml.safe_load(f)
        deep_merge(self.config, data)

        self.name: str = self.config['name']
        self.plugin_dir: Path = plugin_dir

        # Initialize file lists and timestamp
        self.data: List[Dict[str, Any]] = []
        self.sources: List[Path] = []
        self.data_files: List[Path] = []
        self.files: List[Path] = []
        self.timestamp: float = 0  # Default timestamp

        # Categorize and load files
        for file in plugin_dir.iterdir():
            if file.is_file():
                # Update timestamp if this file is newer
                file_timestamp = os.path.getmtime(file)
                if file_timestamp > self.timestamp:
                    self.timestamp = file_timestamp

                # Categorize file by type
                if file.suffix.lower() == '.py':
                    self.sources.append(file)
                elif file.suffix.lower() == '.yaml' and file.stem != 'config':
                    with open(file) as f:
                        self.data.append(expand_shorthand(yaml.safe_load(f)))
                    self.data_files.append(file)
                else:
                    self.files.append(file)
