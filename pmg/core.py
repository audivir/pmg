"""Resolving, installing, and uninstalling packages from their specs.

Package specs are TOML files named after the package, searched in `$PMG_SPECS_DIR`, then in
`$PMG_HOME/specs`, then in the specs shipped with pmg. Templates in a spec are Jinja templates with
{{ tag }} (the release tag, e.g. "v0.26.1"), {{ version }} (the tag without a leading "v"), and
{{ asset }} (the asset file name).
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
from typing import TYPE_CHECKING, Annotated, Literal, NotRequired, TypeAlias, TypedDict
from urllib.parse import urlsplit

import jinja2
import msgspec
from mxhttp import BearerAuth, Downloader, RawPath, SyncConsumer, base_url, get

if TYPE_CHECKING:
    from collections.abc import Generator

    from _typeshed import StrPath

Command: TypeAlias = str
Platform: TypeAlias = Literal["glibc_x64", "glibc_arm64", "musl_x64", "musl_arm64", "macos_arm64"]

GH_TOKEN_ENV = "PMG_GH_TOKEN"  # noqa: S105
HOST_PLATFORMS: dict[tuple[str, str], Platform] = {
    ("glibc", "x86_64"): "glibc_x64",
    ("glibc", "aarch64"): "glibc_arm64",
    ("musl", "x86_64"): "musl_x64",
    ("musl", "aarch64"): "musl_arm64",
    ("macos", "arm64"): "macos_arm64",
}
"""Platform for each libc and machine, as reported by `platform.machine`."""
TAR_SUFFIXES = (".tar", ".tar.gz", ".tgz", ".tar.xz", ".txz", ".tar.bz2", ".tbz2")

logger = logging.getLogger("pmg")


class PmgError(Exception):
    """A package could not be resolved, installed, or uninstalled."""


class BaseStruct(msgspec.Struct, forbid_unknown_fields=True, kw_only=True):
    """Base class for spec and state structs, rejecting unknown fields."""


class GitHubRelease(BaseStruct, tag="github", kw_only=True):
    """Stores the GitHub repo whose latest release is the latest version."""

    repo: str


class CommandRelease(BaseStruct, tag="command", kw_only=True):
    """Stores a shell command printing the latest tag."""

    cmd: Command


class StaticRelease(BaseStruct, tag="static", kw_only=True):
    """Stores a fixed tag."""

    tag: str


Release: TypeAlias = GitHubRelease | CommandRelease | StaticRelease


class GitHubDownload(BaseStruct, tag="github", kw_only=True):
    """Stores the GitHub repo whose release assets are downloaded."""

    repo: str | None = None
    """Repo of the release, if it differs from the repo of a GitHub release spec."""


class UrlDownload(BaseStruct, tag="url", kw_only=True):
    """Stores the URL template of the download."""

    url: str


class CommandDownload(BaseStruct, tag="command", kw_only=True):
    """Stores the shell command of an installer like the one of rustup."""

    cmd: Command


Download: TypeAlias = GitHubDownload | UrlDownload | CommandDownload


class Assets(TypedDict):
    """Stores the asset file name for each platform; a missing platform is unsupported."""

    glibc_x64: NotRequired[str]
    glibc_arm64: NotRequired[str]
    musl_x64: NotRequired[str]
    musl_arm64: NotRequired[str]
    macos_arm64: NotRequired[str]


class External(BaseStruct, kw_only=True):
    """Stores the package names in system package managers."""

    brew: str | None = None
    apt: str | None = None
    apk: str | None = None
    dnf: str | None = None
    yum: str | None = None


class Check(BaseStruct, kw_only=True):
    """Stores how to read the version of the installed binary."""

    args: list[str] = msgspec.field(default_factory=lambda: ["--version"])
    regex: str = r"\d+(?:\.\d+)+"
    """Pattern whose first match in the output is the version."""


class Package(BaseStruct, kw_only=True):
    """Stores the spec of a package."""

    release: Release
    download: Download
    deps: list[str] = []
    external: External
    min_glibc: Annotated[str, msgspec.Meta(pattern=r"^\d+(\.\d+)*$")] | None = None
    """Oldest glibc for the glibc assets; older glibc hosts get the musl assets."""
    assets: Assets
    bin: dict[str, str] = {}
    """Name in the bin dir mapped to the path in the archive, without a single top-level dir."""
    check: Check
    post_install: Command | None = None
    """Shell command run in the staging dir, which is also in `PREFIX`, e.g. to patchelf."""
    uninstall: Command | None = None
    """Shell command for extra cleanup, run before the installed files are removed."""

    def __post_init__(self) -> None:
        """Takes the download repo from the release if it is not set."""
        dl, rl = self.download, self.release
        if isinstance(dl, GitHubDownload) and not dl.repo:
            if not isinstance(rl, GitHubRelease):  # pragma: no cover
                raise ValueError(
                    "GitHubDownload needs to specify the repo if GitHubRelease is not used"
                )
            dl.repo = rl.repo

    @property
    def min_glibc_tuple(self) -> tuple[int, ...] | None:
        """Oldest glibc for the glibc assets as an integer tuple."""
        if not self.min_glibc:
            return None
        return version_tuple(self.min_glibc)


class Record(BaseStruct, kw_only=True):
    """Stores the installed state of a package."""

    tag: str
    explicit: bool
    """Whether the package was requested directly rather than pulled in as a dependency."""
    deps: list[str]
    files: list[str]
    """Absolute paths of the installed files."""


class GitHubAsset(msgspec.Struct, kw_only=True):
    """Stores a release asset from the GitHub API."""

    name: str
    browser_download_url: str
    digest: str | None = None
    """Checksum as "sha256:<hex>", only for assets uploaded since mid 2025."""


class GitHubReleaseInfo(msgspec.Struct, kw_only=True):
    """Stores a release from the GitHub API."""

    tag_name: str
    assets: list[GitHubAsset]


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


def pmg_home() -> Path:
    """Returns the directory of the install records, the staging dirs, and the user specs."""
    if home := os.getenv("PMG_HOME"):
        return Path(home)
    return Path(os.getenv("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "pmg"


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
    """Maps each top-level dir of the staging layout to its install location."""
    return {"bin": Path(os.getenv("XDG_BIN_HOME") or Path.home() / ".local" / "bin")}


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


def render(template: str, tag: str, **extra: str) -> str:
    """Renders a spec template with the tag, the version, and extra variables."""
    return jinja_env().from_string(template).render(tag=tag, version=tag.removeprefix("v"), **extra)


def glibc_version() -> tuple[int, ...] | None:
    """Returns the glibc version of the host, or None on musl and macOS."""
    with contextlib.suppress(ValueError, OSError):
        # glibc hosts always report a version, other hosts raise.
        if value := os.confstr("CS_GNU_LIBC_VERSION"):  # pragma: no branch
            return version_tuple(value.removeprefix("glibc "))
    return None


def version_tuple(version: str) -> tuple[int, ...]:
    """Converts a dot-separated version to an integer tuple."""
    return tuple(int(part) for part in version.split("."))


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


def fetch_release(pkg: Package) -> str:
    """Returns the latest tag."""
    rl = pkg.release
    if isinstance(rl, GitHubRelease):
        return github_api().latest_release(*split_repo(rl.repo)).tag_name
    if isinstance(rl, CommandRelease):
        # spec commands are shell commands by design.
        return subprocess.check_output(rl.cmd, shell=True, text=True).strip()  # noqa: S602
    return rl.tag


def download_file(url: str, dest: Path, checksum: str | None = None) -> Path:
    """Downloads `url` to `dest`, verifying `checksum` ("sha256:<hex>") if given."""
    parts = urlsplit(url)
    path = parts.path.lstrip("/") + (f"?{parts.query}" if parts.query else "")
    files = Files(base_url=f"{parts.scheme}://{parts.netloc}", follow_redirects=True)
    # overwrite, as a .part left by a failed checksum would otherwise be resumed.
    return files.download(path=path)(dest, checksum=checksum, overwrite=True)


def run_download(pkg: Package, tag: str, host: Platform, dl_dir: StrPath) -> Path:
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
    asset = render(asset_template, tag)
    if isinstance(dl, GitHubDownload):
        if not dl.repo:  # pragma: no cover
            raise RuntimeError("GitHubDownload's repo not set in __post_init__")
        info = github_api().release(*split_repo(dl.repo), tag=tag)
        found = next((a for a in info.assets if a.name == asset), None)
        if found is None:  # pragma: no cover
            raise PmgError(f"{dl.repo} {tag} has no asset {asset}")
        return download_file(found.browser_download_url, dl_dir / asset, found.digest)
    url = render(dl.url, tag, asset=asset)
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
def target_layout() -> Generator[Path]:
    """Creates a staging dir with the install layout, next to the installed files."""
    staging_root = pmg_home() / "tmp"
    staging_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=staging_root) as tmp:
        target = Path(tmp) / "target"
        for sub in layout():
            (target / sub).mkdir(parents=True)
        yield target


def run_install(name: str, pkg: Package, tag: str, archive: Path, target: Path) -> None:
    """Unpacks the archive and places the files of the package in the staging dir.

    Raises:
        PmgError: If a file is missing from the archive or `post_install` fails.
    """
    content = unpack(archive, target.parent / "unpacked")
    for bin_name, path_template in pkg.bin.items():
        source = content / render(path_template, tag, asset=archive.name)
        if not source.is_file():  # pragma: no cover
            raise PmgError(f"{archive.name} has no {source.relative_to(content)}")
        dest = target / "bin" / bin_name
        shutil.copy2(source, dest)
        dest.chmod(dest.stat().st_mode | 0o111)
    if pkg.post_install:
        try:
            subprocess.check_call(  # noqa: S602
                pkg.post_install, shell=True, cwd=target, env={**os.environ, "PREFIX": str(target)}
            )
        except subprocess.CalledProcessError as e:
            raise PmgError(f"post_install of {name} failed with exit code {e.returncode}") from e


def track_installed_files(target: Path) -> list[Path]:
    """Returns the files in the staging dir, relative to it."""
    return sorted(
        path.relative_to(target)
        for path in target.rglob("*")
        if path.is_file() or path.is_symlink()
    )


def destination(relative: Path) -> Path:
    """Returns the install location of a file in the staging dir.

    Raises:
        PmgError: If the file is outside the layout.
    """
    roots = layout()
    if relative.parts[0] not in roots:  # pragma: no cover
        raise PmgError(f"{relative} is outside the install layout {sorted(roots)}")
    return roots[relative.parts[0]].joinpath(*relative.parts[1:])


def verify_no_overwrites(name: str, files: list[Path]) -> None:
    """Checks that no file of the package would replace an existing one.

    Raises:
        PmgError: If an install location is already taken.
    """
    for relative in files:
        dest = destination(relative)
        if dest.exists() or dest.is_symlink():
            raise PmgError(f"{dest} exists and does not belong to {name}")


def move_data(target: Path, files: list[Path]) -> list[Path]:
    """Moves the staged files to their install locations and returns those locations."""
    moved: list[Path] = []
    try:
        for relative in files:
            dest = destination(relative)
            dest.parent.mkdir(parents=True, exist_ok=True)
            # a rename, so atomic, as the staging dir is on the same filesystem.
            shutil.move(target / relative, dest)
            moved.append(dest)
    except BaseException:
        rewind_state(moved)
        raise
    return moved


def rewind_state(moved: list[Path]) -> None:
    """Removes the files moved by a failed install."""
    for path in moved:
        path.unlink(missing_ok=True)


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
    tag = fetch_release(pkg)
    host = detect_platform(pkg.min_glibc_tuple)
    with target_layout() as target:
        archive = run_download(pkg, tag, host, target.parent / "download")
        run_install(name, pkg, tag, archive, target)
        files = track_installed_files(target)
        verify_no_overwrites(name, files)
        moved = move_data(target, files)
        try:
            save_record(
                name,
                Record(tag=tag, explicit=explicit, deps=pkg.deps, files=[str(p) for p in moved]),
            )
        except BaseException:
            rewind_state(moved)
            raise
    logger.info("installed %s %s", name, tag)


def uninstall_package(name: str, record: Record) -> None:
    """Runs the uninstall hook of a package and removes its files and record.

    Raises:
        PmgError: If the uninstall hook fails.
    """
    hook = load_spec(name).uninstall if name in available_specs() else None
    if hook:
        try:
            subprocess.check_call(hook, shell=True)  # noqa: S602
        except subprocess.CalledProcessError as e:  # pragma: no cover
            raise PmgError(f"uninstall of {name} failed with exit code {e.returncode}") from e
    for path in record.files:
        Path(path).unlink(missing_ok=True)
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
