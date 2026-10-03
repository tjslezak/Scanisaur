"""The ``scanisaur`` command.

``scanisaur hook`` runs before every SQL tool call an agent makes, so it starts here,
without importing the CLI and the engine; every other command goes to the Typer app.
"""

import sys


def main() -> None:
    if sys.argv[1:2] == ["hook"]:
        from scanisaur.hook import main as hook_main

        raise SystemExit(hook_main(sys.argv[2:]))
    from scanisaur.cli import app

    app(prog_name="scanisaur")


if __name__ == "__main__":
    main()
