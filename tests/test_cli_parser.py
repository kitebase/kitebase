"""An application extends the kitebase CLI with its own commands."""
from kitebase.cli import make_parser


def test_application_adds_a_command():
    parser = make_parser()
    p = parser.commands.add_parser('app-cmd', help='a command of the application')
    p.add_argument('--dry-run', action='store_true')

    args = parser.parse_args(['app-cmd', '--dry-run'])
    assert args.command == 'app-cmd' and args.dry_run

    # the kitebase commands are still there, next to it
    assert parser.parse_args(['db-check']).command == 'db-check'
    assert 'app-cmd' in parser.format_help()
