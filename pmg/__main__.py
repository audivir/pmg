"""Command-line interface of pmg."""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__package__)


def main() -> None:
    """Runs the pmg command line, the entry point of the pmg command."""
    import doctyper

    from pmg.cli import (
        autoremove,
        external,
        install,
        list_installed,
        print_completion,
        print_env,
        print_schema,
        search,
        uninstall,
        update,
        upgrade,
        use,
        validate,
    )

    # reduce completions time
    if "_PMG_COMPLETE" in os.environ:
        import doctyper._completion_classes

        doctyper._completion_classes._sanitize_help_text = lambda text: text  # noqa: SLF001

    logging.basicConfig(format="%(message)s")
    logger.setLevel(logging.INFO)

    app = doctyper.DocTyper(help=__doc__, add_completion=True)
    app.command()(install)
    app.command()(uninstall)
    app.command()(autoremove)
    app.command()(use)
    app.command()(upgrade)
    app.command()(update)
    app.command("schema")(print_schema)
    app.command("env")(print_env)
    app.command()(external)
    app.command()(search)
    app.command("list")(list_installed)
    app.command("completion")(print_completion)
    app.command()(validate)
    # completions call the program pmg, also when started as python -m pmg
    app(prog_name="pmg")


if __name__ == "__main__":
    main()
