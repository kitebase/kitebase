"""Every text file is read and written as UTF-8, whatever the platform.

Without `encoding`, open(), read_text() and write_text() use the locale's
encoding: UTF-8 on Linux and macOS, cp1252 on most Windows installations,
where a label with an accent in a plugin's YAML would be read wrong. The
library cannot rely on PYTHONUTF8 being set by whoever runs it, so the
encoding is written at each call, and this test finds a call that lacks it:
in the package, in its tests and in the commons, when they sit beside it.
"""
import ast
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent
ROOTS = [SERVER / 'kitebase', SERVER / 'tests', SERVER / 'devtest',
         SERVER.parent / 'commons']


def _mode(call: ast.Call):
    if len(call.args) > 1:
        return call.args[1].value if isinstance(call.args[1], ast.Constant) else None
    for keyword in call.keywords:
        if keyword.arg == 'mode':
            return keyword.value.value if isinstance(keyword.value, ast.Constant) else None
    return 'r'


def _lacks_encoding(call: ast.Call) -> bool:
    if any(keyword.arg in ('encoding', None) for keyword in call.keywords):
        return False
    func = call.func
    if isinstance(func, ast.Name) and func.id == 'open':
        mode = _mode(call)
        # a mode that is not a literal is not guessed: binary is the likely case
        return isinstance(mode, str) and 'b' not in mode
    return isinstance(func, ast.Attribute) and func.attr in ('read_text', 'write_text')


def _sources():
    for root in ROOTS:
        if root.is_dir():
            for path in sorted(root.rglob('*.py')):
                if not {'.venv', 'node_modules'} & set(path.parts):
                    yield path


def test_every_text_file_is_opened_as_utf8():
    missing = [
        f"{path.relative_to(SERVER.parent)}:{node.lineno}"
        for path in _sources()
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8')))
        if isinstance(node, ast.Call) and _lacks_encoding(node)
    ]
    assert not missing, "add encoding='utf-8' to:\n  " + "\n  ".join(missing)
