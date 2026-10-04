"""Entry point for `python -m kitebase`.

The same thing the `kitebase` console script runs. It exists so the command
works from a source checkout that has not been installed — which is exactly the
situation of someone who has just cloned the repository.
"""
from kitebase.cli import main

if __name__ == "__main__":
    main()
