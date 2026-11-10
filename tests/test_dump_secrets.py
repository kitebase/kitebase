"""
The dump holds no secrets, by construction: whatever a plugin writes in its
YAML, a value under a secret-looking key and the credentials in a URL are
masked in the parsed content, in the raw text and in the merged sections.
"""
import json

import yaml

from kitebase.diagnostics import mask_secrets
from kitebase.dump import dump_app

SMTP_PASSWORD = 'smtp-pw-4f9a'
API_KEY = 'apikey-7c21'
DB_PASSWORD = 'db-pw-e83b'
DB_URL = f'postgresql://kite:{DB_PASSWORD}@db.example.org/kite'


def _app(tmp_path, monkeypatch):
    import kitebase.utils
    from kitebase.app import App
    from kitebase.plugins import PluginsManager

    plugin = tmp_path / 'plugins' / 'mailer'
    plugin.mkdir(parents=True)
    (plugin / 'config.yaml').write_text(
        'name: mailer\n'
        'version: 0.0.1\n'
        '# where the mail goes out\n'
        'smtp:\n'
        '  host: mail.example.org\n'
        f'  password: "{SMTP_PASSWORD}"\n'
        f'archive_db: {DB_URL}\n')
    (plugin / 'model.yaml').write_text(yaml.safe_dump({
        'tables': {'Message': {'columns': [
            {'name': 'id', 'type': 'Integer', 'primary_key': True, 'autoincrement': True},
            {'name': 'password', 'type': 'String', 'secret': True}]}},
        'pages': {'messages': {'title': 'Messages', 'lookup': {'source': {'api_key': API_KEY}}}},
    }))
    cfg = tmp_path / 'config.yaml'
    cfg.write_text(yaml.safe_dump({'name': 'test', 'plugins': ['plugins']}))
    monkeypatch.chdir(tmp_path)

    pm = PluginsManager()
    pm.load_config(str(cfg))
    pm.load_plugins()
    db = App()
    db.calc_db(pm)
    monkeypatch.setattr(kitebase.utils, 'get_app', lambda: db)
    return db


def test_no_known_secret_leaves_in_the_dump(tmp_path, monkeypatch):
    text = json.dumps(dump_app(_app(tmp_path, monkeypatch)), default=str)
    for secret in (SMTP_PASSWORD, API_KEY, DB_PASSWORD):
        assert secret not in text


def test_what_is_not_secret_stays(tmp_path, monkeypatch):
    dump = dump_app(_app(tmp_path, monkeypatch))
    files = dump['source_files']['mailer']['files']
    config = files['config.yaml']
    assert config['content']['smtp'] == {'host': 'mail.example.org', 'password': '***'}
    assert config['content']['archive_db'] == 'postgresql://kite:***@db.example.org/kite'
    # The raw text keeps its comments and shape, only the values change.
    assert '# where the mail goes out' in config['text']
    assert '  password: ***\n' in config['text']
    assert 'archive_db: postgresql://kite:***@db.example.org/kite' in config['text']
    # A flag is not a value to hide, and a column named `password` is a name.
    columns = dump['tables']['Message']['columns']
    assert any(c.get('name') == 'password' and c.get('secret') is True for c in columns)
    assert dump['pages']['messages']['lookup']['source']['api_key'] == '***'


def test_the_masking_rules():
    assert mask_secrets({'Secret_Key': 'x', 'token': 'y', 'host': 'z'}) == \
        {'Secret_Key': '***', 'token': '***', 'host': 'z'}
    assert mask_secrets({'password': None, 'secret': True, 'api_key': ''}) == \
        {'password': None, 'secret': True, 'api_key': ''}
    assert mask_secrets(['https://u:p@h/x', 'https://h/x', 'mailto:a@b']) == \
        ['https://u:***@h/x', 'https://h/x', 'mailto:a@b']


def test_the_raw_text_hides_a_block_and_keeps_the_rest():
    from kitebase.diagnostics import mask_yaml_text
    text = ('keys:\n'
            '  private_key: |\n'
            '    -----BEGIN KEY-----\n'
            '    abc\n'
            '  public: yes\n'
            '  "token": t0k  # the bot\n'
            '  secret: true\n'
            '  password: { label: Change password }\n')
    assert mask_yaml_text(text) == ('keys:\n'
                                    '  private_key: |\n'
                                    '    ***\n'
                                    '    ***\n'
                                    '  public: yes\n'
                                    '  "token": ***\n'
                                    '  secret: true\n'
                                    '  password: { label: Change password }\n')
