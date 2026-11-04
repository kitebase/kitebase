"""
kitebase.secrets — the reserved values of an installation, by name.

Credentials do not belong in config.yaml, which is versioned and read by the
introspection tools. They live in a folder of their own, one YAML file per kind
and one top-level key per name:

    <app>/secrets/engines.yaml
        snapshot: sqlite:///data/snapshot.sqlite
        pg: postgresql+psycopg://kitebase:...@localhost:5432/app

    secrets.get('engines', 'pg')    # the URL above
    secrets.names('engines')        # ['snapshot', 'pg']

A folder is what the secret stores hand over: Docker mounts `/run/secrets/`,
systemd `$CREDENTIALS_DIRECTORY`, and an Ansible template fills it from a vault
as it would fill a `.env`. `KITEBASE_SECRETS` moves it (absolute, or relative to
the application directory); without it, `secrets/` next to config.yaml.

The module reads and returns: what an entry holds is checked by whoever uses
that kind (the core for `engines`, a plugin for its own). Values are read when
they are needed and never copied into the configuration or the merged tree, so
nothing that dumps or serves those can carry one.
"""

import os
from pathlib import Path
from typing import Any, List, Optional

import yaml

ENV_VAR = 'KITEBASE_SECRETS'

_app_root: Optional[Path] = None


class SecretError(LookupError):
    """A kind or a name that is not there. The message says where it looked."""


def set_app_root(path: Optional[Path]) -> None:
    """The directory a relative secrets folder hangs from (config.yaml's)."""
    global _app_root
    _app_root = Path(path) if path is not None else None


def root() -> Path:
    """The secrets folder: `KITEBASE_SECRETS`, or `secrets/` in the app directory."""
    base = _app_root or Path.cwd()
    declared = os.environ.get(ENV_VAR)
    return (base / declared).resolve() if declared else base / 'secrets'


def _load(kind: str) -> dict:
    path = root() / f'{kind}.yaml'
    if not path.is_file():
        raise SecretError(f"No secrets of kind '{kind}': {path} does not exist")
    with open(path, encoding='utf-8') as f:
        data = yaml.safe_load(f)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise SecretError(f"{path} must map names to values")
    return data


def names(kind: str) -> List[str]:
    """The names a kind declares; empty when its file does not exist."""
    try:
        return list(_load(kind))
    except SecretError:
        if not (root() / f'{kind}.yaml').is_file():
            return []
        raise


def get(kind: str, name: str) -> Any:
    """The value of `name` in `<secrets>/<kind>.yaml`."""
    data = _load(kind)
    if name not in data:
        known = ', '.join(data) or 'none'
        raise SecretError(
            f"No '{name}' in {root() / f'{kind}.yaml'} (declared: {known})")
    return data[name]
