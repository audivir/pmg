"""Command-line interface of pmg."""

from __future__ import annotations

if __name__ == "__main__":
    import logging

    import doctyper

    from pmg.core import (
        autoremove,
        install,
        list_installed,
        logger,
        print_env,
        uninstall,
        upgrade,
        use,
    )

    # info for pmg only, as httpx logs every request at info.
    logging.basicConfig(format="%(message)s")
    logger.setLevel(logging.INFO)
    app = doctyper.DocTyper(help=__doc__)
    app.command()(install)
    app.command()(uninstall)
    app.command()(autoremove)
    app.command()(use)
    app.command()(upgrade)
    app.command("env")(print_env)
    app.command("list")(list_installed)
    app()
