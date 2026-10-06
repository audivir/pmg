"""Subcommands for the CLI."""

from __future__ import annotations

import logging
from pathlib import Path  # noqa: TC003
from typing import Annotated

import doctyper

from pmg.core import (
    ZSH_COMPLETION,
    available_specs,
    decode,
    env_code,
    exit_on_error,
    find_orphans,
    load_records,
    registry_dir,
    uninstall_packages,
    update_registry,
)

logger = logging.getLogger(__package__)


def complete_available(incomplete: str) -> list[str]:
    """Completes the names of the packages with a spec, downloading the registry if missing."""
    import contextlib

    if not registry_dir().exists():
        # quietly, as the output would land in the middle of the command line
        level = logger.level
        logger.setLevel(logging.WARNING)
        try:
            with contextlib.suppress(Exception):
                update_registry()
        finally:
            logger.setLevel(level)
    return [name for name in available_specs() if name.startswith(incomplete)]


def complete_installed(incomplete: str) -> list[str]:
    """Completes the names of installed packages and their versions as name@tag."""
    from pmg.core import load_records

    records = load_records()
    names = {record.name for record in records.values()} | set(records)
    return sorted(name for name in names if name.startswith(incomplete))


def install(
    names: Annotated[list[str], doctyper.Argument(autocompletion=complete_available)],
    no_external: Annotated[bool, doctyper.Option("--no-external")] = False,
) -> None:
    """Installs packages and their dependencies.

    A package that fails skips only itself and its dependents, pmg installs the others and then
    exits with an error.

    Args:
        names: Names of the packages to install, each optionally with a release tag as name@tag,
            or globs like "zst*" matching the names of specs, skipping those not for the host.
        no_external: Install the packages even if a version outside pmg, e.g. of the system, is
            there; their dependencies may still be external.
    """
    from pmg.core import (
        PmgError,
        ensure_registry,
        exit_on_error,
        expand_globs,
        install_packages,
        is_for_host,
        is_glob,
        load_spec,
    )

    with exit_on_error():
        if globs := [arg for arg in names if is_glob(arg)]:
            ensure_registry()
            matches = expand_globs(globs, available_specs())
            skipped = [name for name in matches if not is_for_host(load_spec(name))]
            if skipped:
                logger.info("skipped %s, not for this host", ", ".join(skipped))
            plain = [arg for arg in names if not is_glob(arg)]
            names = [
                *plain,
                *(name for name in matches if name not in skipped and name not in plain),
            ]
        requested: dict[str, list[str | None]] = {}
        for arg in names:
            name, _, tag = arg.partition("@")
            requested.setdefault(name, []).append(tag or None)
        if failed := install_packages(requested, external=not no_external):
            raise PmgError(f"not installed: {', '.join(failed)}")


def uninstall(
    names: Annotated[list[str], doctyper.Argument(autocompletion=complete_installed)],
) -> None:
    """Uninstalls packages; their dependencies stay until `autoremove`.

    Args:
        names: Names of the packages to uninstall with all their versions, name@tag for one, or
            globs like "zst*" matching the names of installed packages.
    """
    from pmg.core import expand_globs

    with exit_on_error():
        installed = {record.name for record in load_records().values()}
        uninstall_packages(expand_globs(names, installed))


def autoremove() -> None:
    """Uninstalls dependencies that no directly installed package needs anymore."""
    with exit_on_error():
        if orphans := find_orphans(load_records()):
            uninstall_packages(orphans)


def use(name: Annotated[str, doctyper.Argument(autocompletion=complete_installed)]) -> None:
    """Makes a version the one the plain command names, man pages, and completions link to.

    Args:
        name: Package version as name@tag.
    """
    from pmg.core import PmgError, activate, exit_on_error, load_records

    with exit_on_error():
        records = load_records()
        record = records.get(name)
        if record is None:  # pragma: no cover
            raise PmgError(f"not installed: {name}")
        activate(record, records)


def upgrade(
    names: Annotated[list[str] | None, doctyper.Argument(autocompletion=complete_installed)] = None,
) -> None:
    """Upgrades packages to their latest release.

    A package that fails does not stop the others, pmg upgrades them and then exits with an error.

    Args:
        names: Names of the packages to upgrade, or globs like "zst*" matching the names of
            installed packages; all installed ones if none are given.
    """
    from pmg.core import PmgError, exit_on_error, expand_globs, load_records, upgrade_packages

    with exit_on_error():
        installed = {record.name for record in load_records().values()}
        if failed := upgrade_packages(set(expand_globs(names, installed) if names else installed)):
            raise PmgError(f"not upgraded: {', '.join(failed)}")


def update() -> None:
    """Updates the specs from their repo."""
    with exit_on_error():
        update_registry()


def print_schema() -> None:
    """Prints the JSON schema of specs, which the `schema.json` of this repo keeps for editors."""
    import msgspec

    from pmg.models import Package

    print(msgspec.json.format(msgspec.json.encode(msgspec.json.schema(Package))).decode())  # noqa: T201


def print_env() -> None:
    """Prints shell code setting the environment and PATH entries of the active versions."""
    print(env_code(), end="")  # noqa: T201


def external(name: Annotated[str, doctyper.Argument(autocompletion=complete_available)]) -> None:
    """Prints the names of a package in system package managers, as "manager name" lines.

    Args:
        name: Name of the package.
    """
    import msgspec

    from pmg.core import exit_on_error, load_spec

    with exit_on_error():
        for manager, package in msgspec.structs.asdict(load_spec(name).external).items():
            if package:
                print(f"{manager} {package}")  # noqa: T201


def show(name: Annotated[str, doctyper.Argument(autocompletion=complete_available)]) -> None:
    """Prints the spec of a package as written, from the first spec dir that has one.

    Args:
        name: Name of the package.
    """
    from pmg.core import exit_on_error, find_spec

    with exit_on_error():
        print(find_spec(name).read_text(), end="")  # noqa: T201


def search(pattern: Annotated[str | None, doctyper.Argument()] = None) -> None:
    """Lists the packages with a spec, marking the installed ones.

    Args:
        pattern: Part of the name, or a glob like "zs*" or "*-libs"; all packages if not given.
    """
    from pmg.core import search_specs

    with exit_on_error():
        tags: dict[str, list[str]] = {}
        for record in load_records().values():
            tags.setdefault(record.name, []).append(
                f"{record.tag} (active)" if record.active else record.tag
            )
        rows = [[name, ", ".join(tags.get(name, []))] for name in sorted(search_specs(pattern))]
        print_columns(rows)


def list_installed() -> None:
    """Lists the installed package versions, with the version of external ones."""
    rows = []
    for key, record in load_records().items():
        # one column for both, as external versions are rarely active, which left a gap
        state = ["active"] if record.active else []
        if record.external:
            state.append(f"external {record.external_version or 'unknown'}")
        if record.upgraded_tag:
            state.append(f"upgraded to {record.upgraded_tag}")
        rows.append([key, "explicit" if record.explicit else "dependency", " ".join(state)])
    print_columns(rows)


def print_columns(rows: list[list[str]]) -> None:
    """Prints rows with their columns aligned, without trailing spaces."""
    widths = [max(map(len, column)) for column in zip(*rows, strict=True)]
    for row in rows:
        cells = (cell.ljust(width) for cell, width in zip(row, widths, strict=True))
        print("  ".join(cells).rstrip())  # noqa: T201


def print_version() -> None:
    """Prints the version of pmg, which is its tag without the leading v."""
    from pmg import __version__

    print(__version__)  # noqa: T201


def print_completion() -> None:
    """Prints the zsh completion of pmg, which pmg also writes next to the other completions."""
    print(ZSH_COMPLETION, end="")  # noqa: T201


def validate(paths: Annotated[list[Path], doctyper.Argument()]) -> None:
    """Checks spec files against the models of pmg, e.g. in the CI of a spec repo.

    A path that cannot be read or decoded counts as invalid, the others are still checked.

    Args:
        paths: Spec files to check, or dirs whose *.toml files are checked.
    """
    import msgspec

    invalid = 0
    files: list[Path] = []
    for path in paths:
        if not path.is_dir():
            files.append(path)
        elif specs := sorted(path.glob("*.toml")):
            files += specs
        else:
            logger.error("%s: no *.toml specs in this dir", path)
            invalid += 1
    for path in files:
        try:
            decode(path.read_text())
        except (OSError, UnicodeDecodeError, msgspec.DecodeError) as e:  # noqa: PERF203
            logger.error("%s: %s", path, e)  # noqa: TRY400
            invalid += 1
    if invalid:
        raise SystemExit(1)
    logger.info("%d specs are valid", len(files))
