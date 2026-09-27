"""Resolving, installing, and uninstalling packages from their specs.

Package specs are TOML files named after the package, searched in `$PMG_SPECS_DIR`, then in
`$PMG_HOME/specs`, then in the specs shipped with pmg. Templates in a spec are Jinja templates with
{{ tag }} (the release tag, e.g. "v0.26.1"), {{ version }} (the tag without a leading "v"),
{{ data }} (`$XDG_DATA_HOME`), {{ bin }} (the bin dir), {{ dir }} (the package dir),
{{ dirs.<key> }} (the extra dirs of the package), and, for files, {{ asset }} (the asset name).

Packages install their commands, man pages, and completions into the shared layout below `~/.local`
and everything else into their own dirs below `$XDG_DATA_HOME`, which they own as a whole.
"""

from __future__ import annotations

import contextlib
import functools
import graphlib
import logging
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Annotated
from urllib.parse import urlsplit

import jinja2
import msgspec
from mxhttp import BearerAuth, Downloader, RawPath, SyncConsumer, base_url, get

from pmg.models import (
    CommandDownload,
    CommandRelease,
    Context,
    GitHubDownload,
    GitHubRelease,
    GitHubReleaseInfo,
    Package,
    Platform,
    Record,
    version_tuple,
)

if TYPE_CHECKING:
    from collections.abc import Generator

    from _typeshed import StrPath

GH_TOKEN_ENV = "PMG_GH_TOKEN"  # noqa: S105
HOST_PLATFORMS: dict[tuple[str, str], Platform] = {
    ("glibc", "x86_64"): "glibc_x64",
    ("glibc", "aarch64"): "glibc_arm64",
    ("musl", "x86_64"): "musl_x64",
    ("musl", "aarch64"): "musl_arm64",
    ("macos", "arm64"): "macos_arm64",
}
"""Platform for each libc and machine, as reported by `platform.machine`."""
COMPLETION_NAMES = {"zsh": "_{}", "bash": "{}", "fish": "{}.fish"}
"""File name of the completion script of a command for each shell."""
TAR_SUFFIXES = (".tar", ".tar.gz", ".tgz", ".tar.xz", ".txz", ".tar.bz2", ".tbz2")

logger = logging.getLogger("pmg")


class PmgError(Exception):
    """A package could not be resolved, installed, or uninstalled."""


@base_url("https://api.github.com")
class GitHubApi(SyncConsumer):
    """Wraps the release endpoints of the GitHub API."""

    @get("/repos/{owner}/{name}/releases/latest")
    def latest_release(self, owner: str, name: str) -> GitHubReleaseInfo:  # type: ignore[empty-body]
        """Fetches the latest release of a repo."""

    @get("/repos/{owner}/{name}/releases/tags/{tag}")
    def release(self, owner: str, name: str, tag: str) -> GitHubReleaseInfo:  # type: ignore[empty-body]
        """Fetches the release of a repo with the given tag."""


class Files(SyncConsumer):
    """Wraps file downloads from a single host."""

    @get("/{path}")
    def download(self, path: Annotated[str, RawPath]) -> Downloader:  # type: ignore[empty-body]
        """Binds the download of a path on the host."""


def data_home() -> Path:
    """Returns `$XDG_DATA_HOME`."""
    return Path(os.getenv("XDG_DATA_HOME") or Path.home() / ".local" / "share")


def pmg_home() -> Path:
    """Returns the directory of the install records, the staging dirs, and the user specs."""
    if home := os.getenv("PMG_HOME"):
        return Path(home)
    return data_home() / "pmg"


def spec_dirs() -> list[Path]:
    """Returns the directories searched for specs, in order."""
    dirs = [pmg_home() / "specs", Path(__file__).parent / "specs"]
    if specs := os.getenv("PMG_SPECS_DIR"):
        dirs.insert(0, Path(specs))
    return dirs


def available_specs() -> dict[str, Path]:
    """Maps each package name to its spec, with earlier spec directories taking precedence."""
    specs: dict[str, Path] = {}
    # later entries override earlier ones, so the dirs go from back to front.
    for directory in reversed(spec_dirs()):
        specs |= {path.stem: path for path in sorted(directory.glob("*.toml"))}
    return specs


def layout() -> dict[str, Path]:
    """Maps each top-level dir of the shared staging layout to its install location."""
    data = data_home()
    return {
        "bin": Path(os.getenv("XDG_BIN_HOME") or Path.home() / ".local" / "bin"),
        "man": data / "man",
        "zsh": data / "zsh" / "site-functions",
        "bash": data / "bash-completion" / "completions",
        "fish": data / "fish" / "vendor_completions.d",
    }


def make_context(name: str, pkg: Package, tag: str) -> Context:
    """Resolves the template variables of a package."""
    data, bin_dir = data_home(), layout()["bin"]
    base = Context(tag=tag, data=data, bin=bin_dir, dir=data / name, dirs={})
    return Context(
        tag=tag,
        data=data,
        bin=bin_dir,
        dir=Path(render(pkg.dir, base)) if pkg.dir else base.dir,
        dirs={key: Path(render(value, base)) for key, value in pkg.dirs.items()},
    )


def decode(spec: str) -> Package:
    """Decodes a TOML package spec."""
    return msgspec.toml.decode(spec, type=Package)


def load_spec(name: str) -> Package:
    """Loads the spec of a package.

    Raises:
        PmgError: If the spec is missing or invalid.
    """
    path = available_specs().get(name)
    if path is None:  # pragma: no cover
        dirs = ", ".join(str(directory) for directory in spec_dirs())
        raise PmgError(f"no spec for {name} in {dirs}")
    try:
        return decode(path.read_text())
    except msgspec.ValidationError as e:  # pragma: no cover
        raise PmgError(f"invalid spec {path}: {e}") from e


def record_path(name: str) -> Path:
    """Returns the path of the install record of a package."""
    return pmg_home() / "installed" / f"{name}.json"


def load_records() -> dict[str, Record]:
    """Loads the install records of all installed packages."""
    records_dir = pmg_home() / "installed"
    if not records_dir.is_dir():
        return {}
    return {
        path.stem: msgspec.json.decode(path.read_bytes(), type=Record)
        for path in sorted(records_dir.glob("*.json"))
    }


def save_record(name: str, record: Record) -> None:
    """Writes the install record of a package atomically."""
    path = record_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_bytes(msgspec.json.format(msgspec.json.encode(record)))
    tmp_path.replace(path)


@functools.cache
def github_api() -> GitHubApi:
    """Returns the GitHub API client, authenticated if a token is set."""
    token = os.getenv(GH_TOKEN_ENV) or os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN")
    return GitHubApi(auth=BearerAuth(token) if token else None)


def split_repo(repo: str) -> tuple[str, str]:
    """Splits "owner/name" into owner and name."""
    owner, _, name = repo.partition("/")
    return owner, name


@functools.cache
def jinja_env() -> jinja2.Environment:
    """Returns the Jinja environment, which treats undefined variables as errors."""
    # renders file names and URLs, not HTML.
    return jinja2.Environment(undefined=jinja2.StrictUndefined, keep_trailing_newline=True)  # noqa: S701


def render(template: str, context: Context, **extra: str) -> str:
    """Renders a spec template with the variables of the package and extra variables."""
    return jinja_env().from_string(template).render(**context.variables(), **extra)


def glibc_version() -> tuple[int, ...] | None:
    """Returns the glibc version of the host, or None on musl and macOS."""
    with contextlib.suppress(ValueError, OSError):
        # glibc hosts always report a version, other hosts raise.
        if value := os.confstr("CS_GNU_LIBC_VERSION"):  # pragma: no branch
            return version_tuple(value.removeprefix("glibc "))
    return None


def detect_platform(min_glibc: tuple[int, ...] | None) -> Platform:
    """Returns the host platform, using the musl assets for glibc hosts older than `min_glibc`.

    Raises:
        PmgError: If the OS or architecture is unsupported.
    """
    system, machine = platform.system(), platform.machine()
    glibc = glibc_version()
    uses_musl = glibc is None or (min_glibc is not None and glibc < min_glibc)
    libc = {"Darwin": "macos", "Linux": "musl" if uses_musl else "glibc"}.get(system, system)
    host = HOST_PLATFORMS.get((libc, machine))
    if host is None:  # pragma: no cover
        raise PmgError(f"unsupported platform: {system} {machine}")
    return host


@functools.cache
def spec_shell() -> str:
    """Returns the shell for spec commands.

    bash where available, as dash before 0.5.13 (Ubuntu 24.04) lacks pipefail. Alpine has no bash,
    but its busybox sh has pipefail.
    """
    return shutil.which("bash") or "/bin/sh"


def run_shell(name: str, step: str, cmd: str, cwd: Path | None = None, **env: str) -> str:
    """Runs a spec command with `set -euo pipefail` and returns its output.

    Raises:
        PmgError: If the command fails.
    """
    try:
        # spec commands are shell commands by design.
        return subprocess.check_output(  # noqa: S602
            f"set -euo pipefail\n{cmd}",
            shell=True,
            executable=spec_shell(),
            cwd=cwd,
            text=True,
            env={**os.environ, **env},
        )
    except subprocess.CalledProcessError as e:
        raise PmgError(f"{step} of {name} failed with exit code {e.returncode}") from e


def fetch_release(name: str, pkg: Package) -> str:
    """Returns the latest tag.

    Raises:
        PmgError: If a release command fails or prints no tag.
    """
    rl = pkg.release
    if isinstance(rl, GitHubRelease):
        return github_api().latest_release(*split_repo(rl.repo)).tag_name
    if isinstance(rl, CommandRelease):
        tag = run_shell(name, "release command", rl.cmd).strip()
        if not tag:  # pragma: no cover
            raise PmgError(f"release command of {name} printed no tag")
        return tag
    return rl.tag


def download_file(url: str, dest: Path, checksum: str | None = None) -> Path:
    """Downloads `url` to `dest`, verifying `checksum` ("sha256:<hex>") if given."""
    parts = urlsplit(url)
    path = parts.path.lstrip("/") + (f"?{parts.query}" if parts.query else "")
    files = Files(base_url=f"{parts.scheme}://{parts.netloc}", follow_redirects=True)
    # overwrite, as a .part left by a failed checksum would otherwise be resumed.
    return files.download(path=path)(dest, checksum=checksum, overwrite=True)


def run_download(pkg: Package, context: Context, host: Platform, dl_dir: StrPath) -> Path:
    """Renders the url with the host asset and downloads the data to `dl_dir`.

    Raises:
        PmgError: If there is no asset for the host platform.
    """
    dl_dir = Path(dl_dir)
    dl = pkg.download
    if isinstance(dl, CommandDownload):  # pragma: no cover
        raise NotImplementedError("CommandDownload not yet supported")
    asset_template = pkg.assets.get(host)
    if asset_template is None:  # pragma: no cover
        raise PmgError(f"no asset for {host}")
    asset = render(asset_template, context)
    tag = context.tag
    if isinstance(dl, GitHubDownload):
        if not dl.repo:  # pragma: no cover
            raise RuntimeError("GitHubDownload's repo not set in __post_init__")
        info = github_api().release(*split_repo(dl.repo), tag=tag)
        found = next((a for a in info.assets if a.name == asset), None)
        if found is None:  # pragma: no cover
            raise PmgError(f"{dl.repo} {tag} has no asset {asset}")
        return download_file(found.browser_download_url, dl_dir / asset, found.digest)
    url = render(dl.url, context, asset=asset)
    return download_file(url, dl_dir / Path(urlsplit(url).path).name)


def unpack(archive: Path, dest: Path) -> Path:
    """Unpacks the archive into `dest` and returns the dir holding its content.

    Archives holding a single top-level dir return that dir. Other files count as a bare binary.
    """
    dest.mkdir(parents=True)
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zip_file:
            for info in zip_file.infolist():
                path = Path(zip_file.extract(info, dest))
                # zip stores unix permissions in the upper bits, but extract drops them.
                mode = (info.external_attr >> 16) & 0o777
                if mode and not info.is_dir():
                    path.chmod(mode)
    elif archive.name.endswith(TAR_SUFFIXES):
        with tarfile.open(archive) as tar_file:
            tar_file.extractall(dest, filter="data")
    else:
        shutil.copy2(archive, dest / archive.name)
        return dest
    entries = list(dest.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return dest


@contextlib.contextmanager
def target_layout(context: Context) -> Generator[Path]:
    """Creates a staging dir with the install layout, next to the installed files."""
    staging_root = pmg_home() / "tmp"
    staging_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=staging_root) as tmp:
        target = Path(tmp) / "target"
        for sub in [*layout(), "dir", *(f"dirs/{key}" for key in context.dirs)]:
            (target / sub).mkdir(parents=True)
        yield target


def stage_files(
    pkg: Package, context: Context, content: Path, asset: str, target: Path
) -> list[tuple[str, Path]]:
    """Places commands, links, man pages, and completion files in the staging dir.

    Returns:
        Commands of the generated completions, with the path for their output.

    Raises:
        PmgError: If a file is missing from the archive.
    """

    def source(template: str) -> Path:
        path = content / render(template, context, asset=asset)
        if not path.is_file():  # pragma: no cover
            raise PmgError(f"{asset} has no {path.relative_to(content)}")
        return path

    for bin_name, template in pkg.bin.items():
        dest = target / "bin" / bin_name
        shutil.copy2(source(template), dest)
        dest.chmod(dest.stat().st_mode | 0o111)
    for bin_name, template in pkg.links.items():
        (target / "bin" / bin_name).symlink_to(render(template, context))
    for template in pkg.man:
        page = source(template)
        section = Path(page.name.removesuffix(".gz")).suffix.removeprefix(".")
        (target / "man" / f"man{section}").mkdir(exist_ok=True)
        shutil.copy2(page, target / "man" / f"man{section}" / page.name)
    generated: list[tuple[str, Path]] = []
    for command, completions in pkg.completions.items():
        for shell, file_name in COMPLETION_NAMES.items():
            completion = getattr(completions, shell)
            dest = target / shell / file_name.format(command)
            if isinstance(completion, str):
                shutil.copy2(source(completion), dest)
            elif completion is not None:
                generated.append((completion.cmd, dest))
    return generated


def run_install(name: str, pkg: Package, context: Context, archive: Path, target: Path) -> None:
    """Unpacks the archive and places the files and dirs of the package in the staging dir.

    Raises:
        PmgError: If a file is missing from the archive or a spec command fails.
    """
    content = unpack(archive, target.parent / "unpacked")
    generated = stage_files(pkg, context, content, archive.name, target)
    if pkg.content:
        (target / "dir").rmdir()
        shutil.move(content, target / "dir")
    if pkg.post_install:
        run_shell(
            name, "post_install", render(pkg.post_install, context), target, PREFIX=str(target)
        )
    staged_path = f"{target / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}"
    for cmd, dest in generated:
        output = run_shell(name, "completion", cmd, target, PREFIX=str(target), PATH=staged_path)
        dest.write_text(output)


def track_installed_files(target: Path) -> list[Path]:
    """Returns the files in the shared layout of the staging dir, relative to it."""
    return sorted(
        path.relative_to(target)
        for root in layout()
        for path in (target / root).rglob("*")
        if path.is_file() or path.is_symlink()
    )


def owned_dirs(target: Path, context: Context) -> dict[Path, Path]:
    """Maps the staged dirs of the package that have content to their install locations."""
    staged = {target / "dir": context.dir} | {
        target / "dirs" / key: path for key, path in context.dirs.items()
    }
    return {path: dest for path, dest in staged.items() if any(path.iterdir())}


def destination(relative: Path) -> Path:
    """Returns the install location of a file in the staging dir.

    Raises:
        PmgError: If the file is outside the layout.
    """
    roots = layout()
    if relative.parts[0] not in roots:  # pragma: no cover
        raise PmgError(f"{relative} is outside the install layout {sorted(roots)}")
    return roots[relative.parts[0]].joinpath(*relative.parts[1:])


def verify_no_overwrites(name: str, files: list[Path], dirs: list[Path]) -> None:
    """Checks that no file or dir of the package would replace an existing one.

    Raises:
        PmgError: If an install location is already taken.
    """
    for dest in [*(destination(relative) for relative in files), *dirs]:
        if dest.exists() or dest.is_symlink():
            raise PmgError(f"{dest} exists and does not belong to {name}")


def move_data(target: Path, files: list[Path], dirs: dict[Path, Path]) -> list[Path]:
    """Moves the staged files and dirs to their install locations and returns those locations."""
    moves = [(target / relative, destination(relative)) for relative in files]
    moves += dirs.items()
    moved: list[Path] = []
    try:
        for staged, dest in moves:
            dest.parent.mkdir(parents=True, exist_ok=True)
            # a rename, so atomic, as the staging dir is on the same filesystem.
            shutil.move(staged, dest)
            moved.append(dest)
    except BaseException:
        rewind_state(moved)
        raise
    return moved


def remove_path(path: Path) -> None:
    """Removes a file, a symlink, or a dir tree, if it exists."""
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def rewind_state(moved: list[Path]) -> None:
    """Removes the files and dirs moved by a failed install."""
    for path in moved:
        remove_path(path)


def resolve_install_order(names: list[str]) -> list[str]:
    """Returns the packages and all their dependencies, each dependency before its dependents.

    Raises:
        PmgError: If the dependencies form a cycle.
    """
    sorter: graphlib.TopologicalSorter[str] = graphlib.TopologicalSorter()
    pending = list(names)
    seen: set[str] = set()
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        deps = load_spec(name).deps
        sorter.add(name, *deps)
        pending.extend(deps)
    try:
        return list(sorter.static_order())
    except graphlib.CycleError as e:
        raise PmgError(f"dependency cycle: {' -> '.join(e.args[1])}") from e


def install_package(name: str, explicit: bool) -> None:
    """Installs a package without its dependencies, or marks an installed one as explicit."""
    record = load_records().get(name)
    if record is not None:
        if explicit and not record.explicit:
            record.explicit = True
            save_record(name, record)
        return
    pkg = load_spec(name)
    tag = fetch_release(name, pkg)
    context = make_context(name, pkg, tag)
    host = detect_platform(pkg.min_glibc_tuple)
    with target_layout(context) as target:
        archive = run_download(pkg, context, host, target.parent / "download")
        run_install(name, pkg, context, archive, target)
        files = track_installed_files(target)
        dirs = owned_dirs(target, context)
        verify_no_overwrites(name, files, list(dirs.values()))
        moved = move_data(target, files, dirs)
        try:
            record = Record(
                tag=tag,
                explicit=explicit,
                deps=pkg.deps,
                files=[str(path) for path in moved[: len(files)]],
                dirs=[str(path) for path in moved[len(files) :]],
            )
            save_record(name, record)
        except BaseException:
            rewind_state(moved)
            raise
    logger.info("installed %s %s", name, tag)


def uninstall_package(name: str, record: Record) -> None:
    """Runs the uninstall hook of a package and removes its files and record.

    Raises:
        PmgError: If the uninstall hook fails.
    """
    pkg = load_spec(name) if name in available_specs() else None
    if pkg and pkg.uninstall:
        run_shell(name, "uninstall", render(pkg.uninstall, make_context(name, pkg, record.tag)))
    for path in [*record.files, *record.dirs]:
        remove_path(Path(path))
    record_path(name).unlink()
    logger.info("uninstalled %s", name)


def uninstall_packages(names: list[str]) -> None:
    """Uninstalls packages, dependents before their dependencies.

    Raises:
        PmgError: If a package is not installed or another installed package depends on it.
    """
    records = load_records()
    removing = set(names)
    if missing := sorted(removing - records.keys()):  # pragma: no cover
        raise PmgError(f"not installed: {', '.join(missing)}")
    for name, record in records.items():
        if name in removing:
            continue
        if needed := sorted(removing.intersection(record.deps)):
            raise PmgError(f"{name} depends on {', '.join(needed)}")
    sorter = graphlib.TopologicalSorter(
        {name: [dep for dep in records[name].deps if dep in removing] for name in removing}
    )
    for name in reversed(list(sorter.static_order())):
        uninstall_package(name, records[name])


def find_orphans(records: dict[str, Record]) -> list[str]:
    """Returns the packages neither requested directly nor needed by a requested package."""
    required: set[str] = set()
    pending = [name for name, record in records.items() if record.explicit]
    while pending:
        name = pending.pop()
        if name in required or name not in records:
            continue
        required.add(name)
        pending.extend(records[name].deps)
    return sorted(records.keys() - required)


@contextlib.contextmanager
def exit_on_error() -> Generator[None]:
    """Logs a `PmgError` and exits with code 1."""
    try:
        yield
    except PmgError as e:
        logger.error("error: %s", e)  # noqa: TRY400
        raise SystemExit(1) from e


def install(names: list[str]) -> None:
    """Installs packages and their dependencies.

    Args:
        names: Names of the packages to install.
    """
    with exit_on_error():
        requested = set(names)
        for name in resolve_install_order(names):
            install_package(name, explicit=name in requested)


def uninstall(names: list[str]) -> None:
    """Uninstalls packages; their dependencies stay until `autoremove`.

    Args:
        names: Names of the packages to uninstall.
    """
    with exit_on_error():
        uninstall_packages(names)


def autoremove() -> None:
    """Uninstalls dependencies that no directly installed package needs anymore."""
    with exit_on_error():
        if orphans := find_orphans(load_records()):
            uninstall_packages(orphans)


def list_installed() -> None:
    """Lists the installed packages with their tags."""
    for name, record in load_records().items():
        kind = "explicit" if record.explicit else "dependency"
        sys.stdout.write(f"{name} {record.tag} {kind}\n")
