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
        print_version,
        search,
        show,
        uninstall,
        update,
        upgrade,
        use,
        validate,
    )

    if "_PMG_COMPLETE" in os.environ:
        import doctyper._completion_classes
        from doctyper._completion_classes import completion_init

        # the completion classes of the shells, which only the --install-completion option of
        # add_completion would register; pmg prints and writes its completion itself
        completion_init()
        # reduce completions time
        doctyper._completion_classes._sanitize_help_text = lambda text: text  # noqa: SLF001

    from pmg.core import LogHandler

    # log lines go above the bars of installs and upgrades
    logging.basicConfig(format="%(message)s", handlers=[LogHandler()])
    logger.setLevel(logging.INFO)

    app = doctyper.DocTyper(help=__doc__)
    app.command()(install)
    app.command()(uninstall)
    app.command()(autoremove)
    app.command()(use)
    app.command()(upgrade)
    app.command()(update)
    app.command("schema")(print_schema)
    app.command("env")(print_env)
    app.command()(external)
    app.command()(show)
    app.command()(search)
    app.command("list")(list_installed)
    app.command("completion")(print_completion)
    app.command("version")(print_version)
    app.command()(validate)
    # completions call the program pmg, also when started as python -m pmg; any error ends in a
    # single line instead of a traceback, e.g. a failed download, and doctyper exits with 130 on
    # an interrupt itself
    try:
        app(prog_name="pmg")
    except Exception as e:  # noqa: BLE001
        lines = str(e).splitlines()
        logger.error("error: %s", lines[0] if lines else type(e).__name__)  # noqa: TRY400
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
