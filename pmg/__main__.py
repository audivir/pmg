"""Command-line interface of pmg."""

from __future__ import annotations

if __name__ == "__main__":
    import logging

    import doctyper

    from pmg.core import (
        autoremove,
        external,
        install,
        list_installed,
        logger,
        print_env,
        print_schema,
        uninstall,
        update,
        upgrade,
        use,
    )

    # info for pmg only, as httpx logs every request at info.
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
    app.command("list")(list_installed)
    # completions call the program pmg, not python -m pmg
    app(prog_name="pmg")
