"""
Schema registry — non-DB typed data containers.

Handles the `schemas:` section of plugin YAML.
Phase 2 will add ResultSet on top of this foundation.
"""

from typing import Any

# Default values for primitive types — used by ResultSet (Phase 2).
PRIMITIVE_DEFAULTS: dict[str, Any] = {
    'String':   '',
    'Text':     '',
    'Integer':  0,
    'Float':    0.0,
    'Numeric':  0.0,
    'Decimal':  0.0,
    'Money':    0.0,
    'Boolean':  False,
    'Date':     None,
    'DateTime': None,
    'Time':     None,
    'FK':       None,
    'JSON':     None,
}


def get_schema_registry(pm_data: dict[str, Any]) -> dict[str, Any]:
    """
    Parse the `schemas:` section from merged plugin data.

    Normalises shorthand syntax (field: TypeName) to {type: TypeName}.
    Returns a dict keyed by schema_id.
    """
    raw = pm_data.get('schemas', {})
    result: dict[str, Any] = {}
    for schema_id, fields in raw.items():
        if schema_id.startswith('$') or not isinstance(fields, dict):
            continue
        resolved: dict[str, Any] = {}
        for field_name, spec in fields.items():
            if field_name.startswith('$'):
                continue
            if isinstance(spec, str):
                resolved[field_name] = {'type': spec}
            else:
                resolved[field_name] = {k: v for k, v in spec.items() if not k.startswith('$')}
        result[schema_id] = resolved
    return result
