"""
kitebase.clientui — where the compiled client is mounted, and who logs people in.

Kitebase has two natures, and an application says which one it has in the
`client:` section of its config.yaml:

    client:
      role: app | admin   # app:   kitebase IS the application and owns "/"
                          # admin: kitebase is the admin of a host application,
                          #        mounted under /admin/; "/" belongs to the host
      login: /login       # the host's login page; absent = the client's own login
      logout: /logout     # the host's page that ends its session
      base: /backoffice   # only to mount somewhere else than the role says

The login is a second axis, not a consequence of the role: a host may have no
users of its own (a public showcase), and then its admin logs people in itself.
With a host login the client takes its token from the host's session, so
leaving has to end that session too: that is what `logout` points to.

The compiled client always lives in `<app>/clientui/`: the directory says what it
is, the configuration says where it is mounted. `static/` stays the host's, with
the route its framework gives it.

Pure: no web framework is imported here, so `check`, `kitebase dev` and the
servers read the same rule.
"""
from typing import Any, Dict, NamedTuple, Optional

ROLES = ('app', 'admin')
DEFAULT_ROLE = 'app'
DEFAULT_BASE = {'app': '', 'admin': '/admin'}

# The directory of an application that holds its compiled client.
CLIENT_DIR = 'clientui'


class ClientSettings(NamedTuple):
    role: str
    base: str              # '' for the root, otherwise '/path' without a trailing slash
    login: Optional[str]   # the host's login page, or None when the client logs in
    logout: Optional[str]  # the host's page that ends its session


def client_settings(config: Dict[str, Any]) -> ClientSettings:
    """
    The `client:` section of an application's config.yaml, checked.

    Raises:
        ValueError: on a role outside ROLES, or a path that does not start
            with '/' — the client would be mounted where nobody looks
    """
    section = config.get('client') or {}
    role = section.get('role', DEFAULT_ROLE)
    if role not in ROLES:
        raise ValueError(f"client.role '{role}' is not one of {', '.join(ROLES)}")

    base = section.get('base', DEFAULT_BASE[role])
    base = '' if base in (None, '', '/') else str(base)
    if base and not base.startswith('/'):
        raise ValueError(f"client.base '{base}' must start with '/'")
    base = base.rstrip('/')

    login = _page(section, 'login')
    logout = _page(section, 'logout')
    return ClientSettings(role, base, login, logout)


def _page(section: Dict[str, Any], key: str) -> Optional[str]:
    """A page of the host, as a path: the client prefixes the backend in dev."""
    page = section.get(key) or None
    if page is not None and not str(page).startswith('/'):
        raise ValueError(f"client.{key} '{page}' must be a path starting with '/'")
    return page
