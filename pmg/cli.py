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
) -> None:
    """Installs packages and their dependencies.

    Args:
        names: Names of the packages to install, each optionally with a release tag as name@tag.
    """
    from pmg.core import exit_on_error, install_package, needed_packages, resolve_install_order

    with exit_on_error():
        requested: dict[str, list[str | None]] = {}
        for arg in names:
            name, _, tag = arg.partition("@")
            requested.setdefault(name, []).append(tag or None)
        order, specifiers = resolve_install_order(list(requested))
        needed = needed_packages(requested, order, specifiers)
        for name in (name for name in order if name in needed):
            for requested_tag in requested.get(name, []):
                install_package(name, explicit=True, tag=requested_tag)
            # dependencies, or requested packages whose dependents need other versions
            if name not in requested or name in specifiers:
                install_package(name, explicit=False, specifier=specifiers.get(name))


def uninstall(
    names: Annotated[list[str], doctyper.Argument(autocompletion=complete_installed)],
) -> None:
    """Uninstalls packages; their dependencies stay until `autoremove`.

    Args:
        names: Names of the packages to uninstall with all their versions, or name@tag for one.
    """
    with exit_on_error():
        uninstall_packages(names)


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

    Args:
        names: Names of the packages to upgrade, all installed ones if none are given.
    """
    from pmg.core import exit_on_error, load_records, upgrade_package

    with exit_on_error():
        for name in sorted(set(names or (record.name for record in load_records().values()))):
            upgrade_package(name)


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


def search(pattern: Annotated[str | None, doctyper.Argument()] = None) -> None:
    """Lists the packages with a spec, marking the installed ones.

    Args:
        pattern: Part of the name, or a glob like "zs*" or "*-libs"; all packages if not given.
    """
    from pmg.core import search_specs

    with exit_on_error():
        installed = {record.name for record in load_records().values()}
        for name in sorted(search_specs(pattern)):
            print(f"{name} installed" if name in installed else name)  # noqa: T201


def list_installed() -> None:
    """Lists the installed package versions."""
    for key, record in load_records().items():
        words = [key, "explicit" if record.explicit else "dependency"]
        if record.active:
            words.append("active")
        if record.external:
            words += ["external", record.external_version or "unknown"]
        print(" ".join(words))  # noqa: T201


def print_version() -> None:
    """Prints the version of pmg, which is its tag without the leading v."""
    from pmg import __version__

    print(__version__)  # noqa: T201


def print_completion() -> None:
    """Prints the zsh completion of pmg, which pmg also writes next to the other completions."""
    print(ZSH_COMPLETION, end="")  # noqa: T201


def validate(paths: Annotated[list[Path], doctyper.Argument()]) -> None:
    """Checks spec files against the models of pmg, e.g. in the CI of a spec repo.

    Args:
        paths: Spec files to check.
    """
    import msgspec

    invalid = 0
    for path in paths:
        try:
            decode(path.read_text())
        except msgspec.ValidationError as e:
            logger.error("%s: %s", path, e)  # noqa: TRY400
            invalid += 1
    if invalid:
        raise SystemExit(1)
    logger.info("%d specs are valid", len(paths))
