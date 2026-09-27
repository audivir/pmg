"""Command-line interface of pmg."""

from __future__ import annotations

if __name__ == "__main__":
    import logging

    import doctyper

    from pmg.core import autoremove, install, list_installed, logger, uninstall

    # info for pmg only, as httpx logs every request at info.
    logging.basicConfig(format="%(message)s")
    logger.setLevel(logging.INFO)
    app = doctyper.DocTyper()
    app.command()(install)
    app.command()(uninstall)
    app.command()(autoremove)
    app.command("list")(list_installed)
    app()
