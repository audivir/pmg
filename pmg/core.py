"""Resolving, installing, and uninstalling packages from their specs.

Package specs are TOML files named after the package, searched in `$PMG_SPECS_DIR`, then in
`$PMG_HOME/specs`, then in the registry, which `pmg update` downloads from the audivir/pmg-specs
repo. Templates in a spec are Jinja templates with
{{ tag }} (the release tag, e.g. "v0.26.1"), {{ version }} (the tag without a leading "v"),
{{ arch }} (the machine as `uname -m` prints it), {{ data }} (`$XDG_DATA_HOME`), {{ bin }} (the bin
dir), {{ dir }} (the package dir), {{ dirs.<key> }} (the extra dirs of the package), and, for
files, {{ asset }} (the asset name).

Packages install their commands, man pages, and completions into the shared layout below `~/.local`
and everything else into their own dirs below `$XDG_DATA_HOME`, which they own as a whole.
"""

from __future__ import annotations

import contextlib
import functools
import graphlib
import hashlib
import logging
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from pathlib import Path
from typing import IO, TYPE_CHECKING, Annotated
from urllib.parse import urlsplit

import doctyper
import jinja2
import msgspec
import zstandard
from mxhttp import BearerAuth, Downloader, RawPath, SyncConsumer, base_url, get
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.version import Version

from pmg.models import (
    ApkDownload,
    ApkRelease,
    Check,
    CommandDownload,
    CommandRelease,
    CondaDownload,
    CondaFile,
    CondaRelease,
    Context,
    GitHubDownload,
    GitHubRelease,
    GitHubReleaseInfo,
    Package,
    Platform,
    Record,
    UrlDownload,
    requirements,
    tag_version,
)

if TYPE_CHECKING:
    from collections.abc import Generator

    from _typeshed import StrPath

GH_TOKEN_ENV = "PMG_GH_TOKEN"  # noqa: S105
REGISTRY_URL = "https://github.com/audivir/pmg-specs/archive/refs/heads/main.tar.gz"
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
CACHE_SECONDS = 3600
"""Age after which indexes of Alpine and conda packages are downloaded again."""
TAR_SUFFIXES = (".tar", ".tar.gz", ".tgz", ".tar.xz", ".txz", ".tar.bz2", ".tbz2", ".apk")
ZSTD_SUFFIXES = (".tar.zst", ".tzst")

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


def registry_dir() -> Path:
    """Returns the dir of the specs downloaded by `pmg update`."""
    return pmg_home() / "registry"


def spec_dirs() -> list[Path]:
    """Returns the directories searched for specs, in order."""
    dirs = [pmg_home() / "specs", registry_dir() / "specs"]
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


def versioned(path: Path, tag: str) -> Path:
    """Returns the path with @tag appended to its name."""
    return path.with_name(f"{path.name}@{tag}")


def version_store(name: str, tag: str) -> Path:
    """Returns the dir holding the man pages and completions of a package version."""
    return pmg_home() / "share" / f"{name}@{tag}"


def make_context(name: str, pkg: Package, tag: str) -> Context:
    """Resolves the template variables of a package."""
    data, bin_dir = data_home(), layout()["bin"]
    arch = platform.machine()
    spec = available_specs().get(name)
    spec_dir = spec.parent if spec else Path()
    base = Context(
        tag=tag, arch=arch, data=data, bin=bin_dir, dir=data / name, dirs={}, spec_dir=spec_dir
    )
    return Context(
        tag=tag,
        arch=arch,
        data=data,
        bin=bin_dir,
        spec_dir=spec_dir,
        dir=versioned(Path(render(pkg.dir, base)) if pkg.dir else base.dir, tag),
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
    # the first use of pmg downloads the registry
    if path is None and not registry_dir().exists():
        update_registry()
        path = available_specs().get(name)
    if path is None:  # pragma: no cover
        dirs = ", ".join(str(directory) for directory in spec_dirs())
        raise PmgError(f"no spec for {name} in {dirs}")
    try:
        return decode(path.read_text())
    except msgspec.ValidationError as e:  # pragma: no cover
        raise PmgError(f"invalid spec {path}: {e}") from e


def update_registry() -> None:
    """Replaces the registry with the specs of its repo, from `PMG_REGISTRY_URL`."""
    url = os.getenv("PMG_REGISTRY_URL") or REGISTRY_URL
    home = pmg_home()
    home.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=home) as tmp:
        archive = download_file(url, Path(tmp) / "registry.tar.gz")
        unpack(archive, Path(tmp) / "unpacked")
        if registry_dir().exists():
            registry_dir().rename(Path(tmp) / "old")
        strip_single_dir(Path(tmp) / "unpacked").rename(registry_dir())
    logger.info("updated the specs from %s", url)


def record_path(key: str) -> Path:
    """Returns the path of the install record of a package version, named name@tag."""
    return pmg_home() / "installed" / f"{key}.json"


def load_records() -> dict[str, Record]:
    """Loads the install records of all installed package versions, by name@tag."""
    records_dir = pmg_home() / "installed"
    if not records_dir.is_dir():
        return {}
    return {
        path.stem: msgspec.json.decode(path.read_bytes(), type=Record)
        for path in sorted(records_dir.glob("*.json"))
    }


def save_record(record: Record) -> None:
    """Writes the install record of a package version atomically."""
    path = record_path(record.key)
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


def glibc_version() -> Version | None:
    """Returns the glibc version of the host, or None on musl and macOS."""
    value: str | None = None
    with contextlib.suppress(ValueError, OSError):
        # only glibc knows the name, other hosts raise.
        value = os.confstr("CS_GNU_LIBC_VERSION")
    return Version(value.removeprefix("glibc ")) if value else None


def detect_platform(min_glibc: Version | None) -> Platform:
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


def run_shell(
    name: str, step: str, cmd: str, cwd: Path | None = None, env: dict[str, str] | None = None
) -> str:
    """Runs a spec command with `set -euo pipefail` and returns its output.

    Its stderr is only shown if it fails.

    Raises:
        PmgError: If the command fails.
    """
    if cwd is None:
        # not the current dir, where a spec dir holding uv.toml would configure uv
        cwd = pmg_home()
        cwd.mkdir(parents=True, exist_ok=True)
    # spec commands are shell commands by design.
    result = subprocess.run(  # noqa: S602
        f"set -euo pipefail\n{cmd}",
        shell=True,
        executable=spec_shell(),
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        # commands installed as dependencies are in the bin dir, which may not be in PATH.
        env={
            **os.environ,
            "PATH": f"{layout()['bin']}{os.pathsep}{os.getenv('PATH', '')}",
            **(env or {}),
        },
    )
    if result.returncode:
        # build warnings and progress only matter when the command fails
        stderr = "".join(result.stderr.splitlines(keepends=True)[-20:])
        raise PmgError(f"{step} of {name} failed with exit code {result.returncode}:\n{stderr}")
    return result.stdout


def cache_dir() -> Path:
    """Returns the cache dir of pmg in `$XDG_CACHE_HOME`."""
    return Path(os.getenv("XDG_CACHE_HOME") or Path.home() / ".cache") / "pmg"


def cached_download(url: str) -> Path:
    """Downloads `url` to the cache, unless it was downloaded within the last hour."""
    path = cache_dir() / hashlib.sha256(url.encode()).hexdigest()[:16]
    if path.exists() and time.time() - path.stat().st_mtime < CACHE_SECONDS:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    return download_file(url, path)


def alpine_repo() -> str:
    """Returns the URL of the main Alpine repo of the host release, or else of latest-stable."""
    mirror = os.getenv("PMG_ALPINE_MIRROR") or "https://dl-cdn.alpinelinux.org/alpine"
    release_file = Path("/etc/alpine-release")
    release = (
        f"v{'.'.join(release_file.read_text().split('.')[:2])}"
        if release_file.exists()
        else "latest-stable"
    )
    machine = {"arm64": "aarch64"}.get(platform.machine(), platform.machine())
    return f"{mirror}/{release}/main/{machine}"


@functools.cache
def apk_index(repo: str) -> dict[str, str]:
    """Maps the packages of an Alpine repo to their versions."""
    index = cached_download(f"{repo}/APKINDEX.tar.gz")
    with tarfile.open(index) as tar_file:
        member = tar_file.extractfile("APKINDEX")
        if member is None:  # pragma: no cover
            raise PmgError(f"{repo} has no APKINDEX")
        text = member.read().decode()
    versions: dict[str, str] = {}
    # blocks of "X:value" lines, P is the package and V its version
    for block in text.split("\n\n"):
        fields = {line[0]: line[2:] for line in block.splitlines() if line[1:2] == ":"}
        if "P" in fields and "V" in fields:
            versions[fields["P"]] = fields["V"]
    return versions


def apk_version(repo: str, package: str) -> str:
    """Returns the version of a package in an Alpine repo.

    Raises:
        PmgError: If the repo has no such package.
    """
    version = apk_index(repo).get(package)
    if version is None:  # pragma: no cover
        raise PmgError(f"{repo} has no package {package}")
    return version


def conda_file(channel: str, package: str, version: str | None = None) -> CondaFile:
    """Returns the newest .conda file of a package, of `version` if given.

    Raises:
        PmgError: If the package has no such file.
    """
    api = os.getenv("PMG_CONDA_API") or "https://api.anaconda.org"
    listing = cached_download(f"{api}/package/{channel}/{package}/files")
    files = [
        file
        for file in msgspec.json.decode(listing.read_bytes(), type=list[CondaFile])
        # conda-forge publishes placeholder builds as 9999
        if file.basename.endswith(".conda") and file.version != "9999"
        if tag_version(file.version) is not None and version in {None, file.version}
    ]
    if not files:  # pragma: no cover
        raise PmgError(f"{channel} has no .conda file of {package} {version or ''}")
    return max(files, key=lambda file: (tag_version(file.version), file.upload_time))


def asset_name(pkg: Package, host: Platform) -> str:
    """Returns the asset template of the host platform.

    Raises:
        PmgError: If there is no asset for the host platform.
    """
    template = pkg.assets.get(host)
    if template is None:  # pragma: no cover
        raise PmgError(f"no asset for {host}")
    return template


def fetch_release(name: str, pkg: Package, host: Platform) -> str:
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
    if isinstance(rl, ApkRelease):
        return apk_version(alpine_repo(), rl.package)
    if isinstance(rl, CondaRelease):
        return conda_file(rl.channel, asset_name(pkg, host)).version
    return rl.tag


def download_file(url: str, dest: Path, checksum: str | None = None) -> Path:
    """Downloads `url` to `dest`, verifying `checksum` ("sha256:<hex>") if given."""
    parts = urlsplit(url)
    path = parts.path.lstrip("/") + (f"?{parts.query}" if parts.query else "")
    files = Files(base_url=f"{parts.scheme}://{parts.netloc}", follow_redirects=True)
    # overwrite, as a .part left by a failed checksum would otherwise be resumed.
    return files.download(path=path)(dest, checksum=checksum, overwrite=True)


def run_download(pkg: Package, context: Context, host: Platform, dl_dir: StrPath) -> list[Path]:
    """Downloads the archives of the host platform to `dl_dir`.

    Raises:
        PmgError: If there is no asset for the host platform.
    """
    dl_dir = Path(dl_dir)
    dl, tag = pkg.download, context.tag
    if isinstance(dl, CommandDownload):
        # the command installs into the staging dir itself, see run_install
        return []
    if isinstance(dl, ApkDownload):
        repo = alpine_repo()
        release = pkg.release.package if isinstance(pkg.release, ApkRelease) else None
        versions = {
            package: tag if package == release else apk_version(repo, package)
            for package in dl.packages
        }
        return [
            download_file(f"{repo}/{package}-{version}.apk", dl_dir / f"{package}-{version}.apk")
            for package, version in versions.items()
        ]
    asset = render(asset_name(pkg, host), context)
    if isinstance(dl, CondaDownload):
        file = conda_file(dl.channel, asset, tag)
        mirror = os.getenv("PMG_CONDA_URL") or "https://conda.anaconda.org"
        checksum = f"sha256:{file.sha256}" if file.sha256 else None
        url = f"{mirror}/{dl.channel}/{file.basename}"
        return [download_file(url, dl_dir / Path(file.basename).name, checksum)]
    if isinstance(dl, GitHubDownload):
        if not dl.repo:  # pragma: no cover
            raise RuntimeError("GitHubDownload's repo not set in __post_init__")
        info = github_api().release(*split_repo(dl.repo), tag=tag)
        found = next((a for a in info.assets if a.name == asset), None)
        if found is None:  # pragma: no cover
            raise PmgError(f"{dl.repo} {tag} has no asset {asset}")
        return [download_file(found.browser_download_url, dl_dir / asset, found.digest)]
    url = render(dl.url, context, asset=asset)
    return [download_file(url, dl_dir / Path(urlsplit(url).path).name)]


def extract_tar_zst(fileobj: IO[bytes], dest: Path) -> None:
    """Extracts a zstd-compressed tar stream into `dest`."""
    with (
        zstandard.ZstdDecompressor().stream_reader(fileobj) as reader,
        tarfile.open(fileobj=reader, mode="r|") as tar_file,
    ):
        tar_file.extractall(dest, filter="data")


def unpack(archive: Path, dest: Path) -> None:
    """Unpacks the archive into `dest`; other files count as a bare binary.

    Alpine packages lose their metadata files, conda packages keep only their payload.
    """
    dest.mkdir(parents=True, exist_ok=True)
    if archive.name.endswith(".conda"):
        with zipfile.ZipFile(archive) as zip_file:
            payload = next(name for name in zip_file.namelist() if name.startswith("pkg-"))
            with zip_file.open(payload) as fileobj:
                extract_tar_zst(fileobj, dest)
    elif archive.name.endswith(ZSTD_SUFFIXES):
        with archive.open("rb") as fileobj:
            extract_tar_zst(fileobj, dest)
    elif archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zip_file:
            for info in zip_file.infolist():
                path = Path(zip_file.extract(info, dest))
                # zip stores unix permissions in the upper bits, but extract drops them.
                mode = (info.external_attr >> 16) & 0o777
                if mode and not info.is_dir():
                    path.chmod(mode)
    elif archive.name.endswith(TAR_SUFFIXES):
        # an .apk is gzipped tars one after another: signature, .PKGINFO, and files
        with tarfile.open(archive) as tar_file:
            tar_file.extractall(dest, filter="data")
        if archive.name.endswith(".apk"):
            for path in dest.glob(".*"):
                remove_path(path)
    else:
        shutil.copy2(archive, dest / archive.name)


def strip_single_dir(root: Path) -> Path:
    """Returns the only entry of `root` if it is a dir, else `root`."""
    entries = list(root.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return root


def prune(root: Path, keep: list[str], remove: list[str]) -> None:
    """Removes the files of the package dir that `keep` misses, and those that `remove` matches."""
    if keep:
        kept = {path for pattern in keep for path in root.glob(pattern)}
        # children come before their parents, so emptied dirs can go too
        for path in sorted(root.rglob("*"), reverse=True):
            if path in kept or not kept.isdisjoint(path.parents):
                continue
            if not path.is_dir() or path.is_symlink():
                path.unlink()
            elif not any(path.iterdir()):
                path.rmdir()
    for pattern in remove:
        for path in list(root.glob(pattern)):
            remove_path(path)


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


def package_env(pkg: Package, context: Context) -> dict[str, str]:
    """Renders the environment of the package."""
    return {key: render(value, context) for key, value in pkg.env.items()}


def staged_context(context: Context, target: Path) -> Context:
    """Returns the context with the package dirs in the staging dir."""
    dirs = {key: target / "dirs" / key for key in context.dirs}
    return msgspec.structs.replace(context, dir=target / "dir", dirs=dirs)


def run_install(
    name: str, pkg: Package, context: Context, archives: list[Path], target: Path
) -> None:
    """Unpacks the archives and places the files and dirs of the package in the staging dir.

    Release archives lose a single top-level dir; Alpine and conda packages keep their layout.
    A command download runs its command instead, with the environment of the package.

    Raises:
        PmgError: If a file is missing from the archives or a spec command fails.
    """
    env = package_env(pkg, staged_context(context, target)) | {"PREFIX": str(target)}
    if isinstance(pkg.download, CommandDownload):
        run_shell(name, "download command", render(pkg.download.cmd, context), target, env)
    content = target.parent / "unpacked"
    content.mkdir()
    for archive in archives:
        unpack(archive, content)
    if isinstance(pkg.download, GitHubDownload | UrlDownload):
        content = strip_single_dir(content)
    asset = archives[0].name if archives else ""
    generated = stage_files(pkg, context, content, asset, target)
    if pkg.content:
        root = content
        if isinstance(pkg.content, str):
            matches = sorted(content.glob(pkg.content))
            if len(matches) != 1:  # pragma: no cover
                raise PmgError(f"content {pkg.content} of {name} matches {len(matches)} dirs")
            root = matches[0]
        (target / "dir").rmdir()
        shutil.move(root, target / "dir")
        prune(target / "dir", pkg.keep, pkg.remove)
        content = target / "dir"
    env["CONTENT"] = str(content)
    if pkg.post_install:
        run_shell(name, "post_install", render(pkg.post_install, context), target, env)
    staged_path = (
        f"{target / 'bin'}{os.pathsep}{layout()['bin']}{os.pathsep}{os.getenv('PATH', '')}"
    )
    for cmd, dest in generated:
        dest.write_text(run_shell(name, "completion", cmd, target, env | {"PATH": staged_path}))


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


def destination(relative: Path, name: str, tag: str) -> Path:
    """Returns the install location of a staged file of a package version.

    Commands get @tag appended, man pages and completions go to the version store.

    Raises:
        PmgError: If the file is outside the layout.
    """
    roots = layout()
    if relative.parts[0] not in roots:  # pragma: no cover
        raise PmgError(f"{relative} is outside the install layout {sorted(roots)}")
    if relative.parts[0] == "bin":
        return versioned(roots["bin"].joinpath(*relative.parts[1:]), tag)
    return version_store(name, tag) / relative


def active_links(record: Record) -> dict[Path, Path]:
    """Maps the plain paths in the shared layout to the files of the version they link to."""
    roots, store = layout(), version_store(record.name, record.tag)
    links: dict[Path, Path] = {}
    for file in map(Path, record.files):
        if file.is_relative_to(store):
            relative = file.relative_to(store)
            links[roots[relative.parts[0]].joinpath(*relative.parts[1:])] = file
        else:
            links[file.with_name(file.name.removesuffix(f"@{record.tag}"))] = file
    return links


def active_version(records: dict[str, Record], name: str) -> Record | None:
    """Returns the active version of a package."""
    return next((r for r in records.values() if r.name == name and r.active), None)


def verify_free(record: Record, paths: list[Path]) -> None:
    """Checks that none of the paths of a version exists yet.

    Raises:
        PmgError: If a path is already taken.
    """
    for path in paths:
        if path.exists() or path.is_symlink():
            raise PmgError(f"{path} exists and does not belong to {record.name}")


def verify_links_free(record: Record, current: Record | None) -> None:
    """Checks that the plain names of a version are free, apart from those of `current`."""
    replaced = set(active_links(current)) if current else set()
    verify_free(record, [link for link in active_links(record) if link not in replaced])


def unlink_active(record: Record) -> None:
    """Removes the plain names of a version from the shared layout."""
    for link in active_links(record):
        # a link is only missing if it was removed by hand.
        if link.is_symlink():  # pragma: no branch
            link.unlink()


def activate(record: Record, records: dict[str, Record]) -> None:
    """Links the plain names of a package to this version, replacing those of another version."""
    current = active_version(records, record.name)
    if current is not None and current.key == record.key:
        return
    verify_links_free(record, current)
    if current is not None:
        unlink_active(current)
        current.active = False
        save_record(current)
    for link, target in active_links(record).items():
        link.parent.mkdir(parents=True, exist_ok=True)
        # commands link to their version next to them, e.g. bat -> bat@v0.26.1.
        link.symlink_to(target.name if target.parent == link.parent else target)
    record.active = True
    save_record(record)


def move_data(target: Path, moves: list[tuple[Path, Path]]) -> list[Path]:
    """Moves staged files and dirs to their install locations and returns those locations."""
    moved: list[Path] = []
    try:
        for relative, dest in moves:
            dest.parent.mkdir(parents=True, exist_ok=True)
            # a rename, so atomic, as the staging dir is on the same filesystem.
            shutil.move(target / relative, dest)
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


def satisfies(tag: str | None, specifier: SpecifierSet) -> bool:
    """Checks whether the version in a release tag meets a version specifier."""
    if not specifier:
        return True
    version = tag_version(tag) if tag else None
    return version is not None and specifier.contains(version, prereleases=True)


def libs_load(libs: list[str]) -> bool:
    """Checks whether the dynamic loader finds all libraries.

    Loading runs the initialization code of a library, so it happens in a separate process.
    """
    code = "import ctypes, sys\nfor name in sys.argv[1:]:\n    ctypes.CDLL(name)"
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code, *libs], capture_output=True, check=False
    )
    return result.returncode == 0


def run_version_command(cmd: list[str], regex: str, dev_tool: bool) -> tuple[bool, str | None]:
    """Runs a command found in PATH without the bin dir of pmg.

    Returns:
        Whether the command ran, and the first match of `regex` in its output.
    """
    bin_dir = layout()["bin"].resolve()
    path = os.pathsep.join(
        entry
        for entry in os.getenv("PATH", "").split(os.pathsep)
        if entry and Path(entry).resolve() != bin_dir
    )
    executable = shutil.which(cmd[0], path=path)
    # the stubs of macOS only offer to install the developer tools, in a dialog when run
    stub = (
        executable is not None
        and dev_tool
        and platform.system() == "Darwin"
        and executable.startswith("/usr/bin/")
        and subprocess.run(
            ["/usr/bin/xcode-select", "-p"], capture_output=True, check=False
        ).returncode
        != 0
    )
    if executable is None or stub:
        return False, None
    try:
        result = subprocess.run(  # noqa: S603
            [executable, *cmd[1:]], capture_output=True, text=True, check=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover
        return False, None
    match = re.search(regex, result.stdout + result.stderr)
    return True, match.group() if match else None


def find_external(name: str, pkg: Package) -> Record | None:
    """Detects a copy of the package that pmg did not install.

    The files and libraries of the check must exist, and its command must run. Without any of
    them, the first command of the package, or else its name, is looked up in PATH.
    """
    context = make_context(name, pkg, "external")
    check = pkg.check or Check()
    files = [Path(render(file, context)) for file in check.files]
    libs = [render(lib, context) for lib in check.libs]
    if not all(file.exists() for file in files) or (libs and not libs_load(libs)):
        return None
    cmd = [render(arg, context) for arg in check.cmd] if check.cmd else None
    if cmd is None and not files and not libs:
        # packages that build their commands in post_install have none in the spec
        command = next(iter([*pkg.bin, *pkg.links]), name)
        cmd = [command, *check.args]
    version = None
    if cmd is not None:
        found, version = run_version_command(cmd, check.regex, check.dev_tool)
        if not found:
            return None
    return Record(
        name=name,
        tag="external",
        explicit=False,
        active=False,
        installed_at=time.time(),
        deps=[],
        files=[],
        external=True,
        external_version=version,
    )


def use_external(  # noqa: PLR0913, PLR0917
    name: str,
    pkg: Package,
    versions: list[Record],
    explicit: bool,
    specifier: SpecifierSet,
    record: bool,
) -> bool:
    """Checks for an external version that meets the specifier, found before or detected now.

    Returns:
        Whether an external version is used, which is recorded if `record` is set.
    """
    external = next((version for version in versions if version.external), None)
    if external is None and not versions:
        external = find_external(name, pkg)
    if external is None or not satisfies(external.version_tag, specifier):
        return False
    if record:
        external.explicit = external.explicit or explicit
        save_record(external)
        logger.info(
            "using %s found outside pmg", " ".join(filter(None, [name, external.external_version]))
        )
    return True


def resolve_install_order(names: list[str]) -> tuple[list[str], dict[str, SpecifierSet]]:
    """Resolves the packages and all their dependencies.

    Returns:
        Packages with each dependency before its dependents, and the combined version specifier
        of their dependents for each dependency.

    Raises:
        PmgError: If the dependencies form a cycle.
    """
    sorter: graphlib.TopologicalSorter[str] = graphlib.TopologicalSorter()
    specifiers: dict[str, SpecifierSet] = {}
    pending = list(names)
    seen: set[str] = set()
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        spec = load_spec(name)
        deps = requirements(package_deps(spec, detect_platform(spec.min_glibc_version)))
        sorter.add(name, *(dep.name for dep in deps))
        for dep in deps:
            specifiers[dep.name] = specifiers.get(dep.name, SpecifierSet()) & dep.specifier
            pending.append(dep.name)
    try:
        return list(sorter.static_order()), specifiers
    except graphlib.CycleError as e:
        raise PmgError(f"dependency cycle: {' -> '.join(e.args[1])}") from e


def package_deps(pkg: Package, host: Platform) -> list[str]:
    """Returns the dependencies of a package, with those for the platform of its assets."""
    return [*pkg.deps, *pkg.platform_deps.get(host, [])]


def dependency_vars(
    pkg: Package, host: Platform, records: dict[str, Record]
) -> dict[str, dict[str, str]]:
    """Returns the package dir and version of each dependency in use, for {{ deps }}.

    Dependencies not in use on the host, e.g. those of other platforms, are empty.
    """
    declared = [*pkg.deps, *(dep for deps in pkg.platform_deps.values() for dep in deps)]
    variables = {Requirement(dep).name: {"dir": "", "version": ""} for dep in declared}
    for dep in requirements(package_deps(pkg, host)):
        versions = [record for record in records.values() if record.name == dep.name]
        record = next((r for r in versions if r.active), None) or next(iter(versions), None)
        package_dir = ""
        if record is not None and not record.external:
            package_dir = str(make_context(dep.name, load_spec(dep.name), record.tag).dir)
        version = (record.version_tag or "") if record is not None else ""
        variables[dep.name] = {"dir": package_dir, "version": version}
    return variables


def nothing_to_install(  # noqa: PLR0913, PLR0917
    name: str,
    pkg: Package,
    host: Platform,
    versions: list[Record],
    explicit: bool,
    tag: str | None,
    specifier: SpecifierSet,
    record_external: bool = True,
) -> bool:
    """Checks whether the package is not for the host, or an installed or external one is enough.

    Raises:
        PmgError: If the package was requested directly but is not for the host.
    """
    if pkg.platforms and host not in pkg.platforms:
        if explicit:
            raise PmgError(f"{name} is only for {', '.join(pkg.platforms)}, not {host}")
        return True
    if tag is not None:
        return False
    if not explicit and any(satisfies(r.version_tag, specifier) for r in versions):
        return True
    return use_external(name, pkg, versions, explicit, specifier, record_external)


def install_package(
    name: str, explicit: bool, tag: str | None = None, specifier: SpecifierSet | None = None
) -> None:
    """Installs a version of a package without its dependencies.

    Installs the latest version, or `tag`. The latest version becomes active; a given tag only if
    no other version is active. An installed version is only marked as explicit if requested.

    Args:
        name: Name of the package.
        explicit: Whether the package was requested directly.
        tag: Release tag to install instead of the latest one.
        specifier: Versions its dependents accept; an installed one satisfies a dependency.

    Raises:
        PmgError: If the package is not for the host, or its latest release does not meet the
            specifier.
    """
    specifier = specifier or SpecifierSet()
    records = load_records()
    pkg = load_spec(name)
    host = detect_platform(pkg.min_glibc_version)
    versions = [record for record in records.values() if record.name == name]
    if nothing_to_install(name, pkg, host, versions, explicit, tag, specifier):
        return
    current = active_version(records, name)
    should_activate = tag is None or current is None
    if tag is None:
        tag = fetch_release(name, pkg, host)
        if not satisfies(tag, specifier):
            raise PmgError(f"a dependency needs {name}{specifier}, the latest release is {tag}")
    record = records.get(f"{name}@{tag}")
    if record is not None:
        if explicit and not record.explicit:
            record.explicit = True
            save_record(record)
        return
    context = make_context(name, pkg, tag)
    context.deps = dependency_vars(pkg, host, records)
    with target_layout(context) as target:
        archives = run_download(pkg, context, host, target.parent / "download")
        run_install(name, pkg, context, archives, target)
        dirs = owned_dirs(target, context)
        moves = [(path, destination(path, name, tag)) for path in track_installed_files(target)]
        record = Record(
            name=name,
            tag=tag,
            explicit=explicit,
            active=False,
            installed_at=time.time(),
            deps=package_deps(pkg, host),
            files=[str(dest) for _, dest in moves],
            dirs=[str(dest) for dest in dirs.values()],
        )
        verify_free(record, [*map(Path, record.files), *map(Path, record.dirs)])
        if should_activate:
            verify_links_free(record, current)
        dir_moves = [(path.relative_to(target), dest) for path, dest in dirs.items()]
        moved = move_data(target, [*moves, *dir_moves])
        try:
            save_record(record)
            if should_activate:
                activate(record, {**records, record.key: record})
        except BaseException:
            rewind_state(moved)
            # the record dir may be what failed.
            with contextlib.suppress(OSError):
                record_path(record.key).unlink(missing_ok=True)
            raise
    logger.info("installed %s %s", name, tag)


def uninstall_version(record: Record) -> None:
    """Runs the uninstall hook of a package version and removes its files, dirs, and record.

    Raises:
        PmgError: If the uninstall hook fails.
    """
    pkg = load_spec(record.name) if record.name in available_specs() else None
    # external versions only lose their record
    if pkg and pkg.uninstall and not record.external:
        context = make_context(record.name, pkg, record.tag)
        env = package_env(pkg, context)
        run_shell(record.name, "uninstall", render(pkg.uninstall, context), env=env)
    if record.active:
        unlink_active(record)
    for path in [*record.files, *record.dirs]:
        remove_path(Path(path))
    record_path(record.key).unlink()
    logger.info("uninstalled %s", record.key)


def uninstall_packages(args: list[str]) -> None:
    """Uninstalls all versions of packages, or single versions given as name@tag.

    Dependents go before their dependencies. If the active version of a package goes and others
    stay, the most recently installed of them becomes active.

    Raises:
        PmgError: If a package is not installed or another installed package depends on it.
    """
    records = load_records()
    removing: set[str] = set()
    for arg in args:
        name, _, tag = arg.partition("@")
        keys = {key for key, r in records.items() if r.name == name and tag in {"", r.tag}}
        if not keys:  # pragma: no cover
            raise PmgError(f"not installed: {arg}")
        removing |= keys
    remaining = {key: r for key, r in records.items() if key not in removing}
    names = {records[key].name for key in removing}
    gone = names - {r.name for r in remaining.values()}
    for record in remaining.values():
        broken = [
            str(dep)
            for dep in requirements(record.deps)
            if dep.name in names
            and not any(
                r.name == dep.name and satisfies(r.version_tag, dep.specifier)
                for r in remaining.values()
            )
        ]
        if broken:
            raise PmgError(f"{record.name} depends on {', '.join(sorted(broken))}")
    sorter = graphlib.TopologicalSorter(
        {
            name: {
                dep.name
                for key in removing
                if records[key].name == name
                for dep in requirements(records[key].deps)
            }
            & names
            for name in names
        }
    )
    for name in reversed(list(sorter.static_order())):
        for key in sorted(key for key in removing if records[key].name == name):
            uninstall_version(records[key])
    for name in names - gone:
        versions = [r for r in remaining.values() if r.name == name]
        if not any(r.active for r in versions):
            activate(max(versions, key=lambda r: r.installed_at), remaining)


def find_orphans(records: dict[str, Record]) -> list[str]:
    """Returns the versions of packages neither requested directly nor needed by one."""
    required: set[str] = set()
    pending = [record.name for record in records.values() if record.explicit]
    while pending:
        name = pending.pop()
        if name in required:
            continue
        required.add(name)
        pending.extend(
            dep.name for r in records.values() if r.name == name for dep in requirements(r.deps)
        )
    return sorted(key for key, record in records.items() if record.name not in required)


def upgrade_package(name: str) -> None:
    """Upgrades the active version of a package to the latest release.

    Packages with an upgrade command update in place. Others get the latest release next to the
    active version, which it replaces unless a dependent still needs it.
    """
    records = load_records()
    active = active_version(records, name)
    # external versions, which are never active, are left to their package manager
    if active is None:
        return
    pkg = load_spec(name)
    context = make_context(name, pkg, active.tag)
    if pkg.upgrade:
        run_shell(name, "upgrade", render(pkg.upgrade, context), env=package_env(pkg, context))
        logger.info("upgraded %s in place", name)
        return
    latest = fetch_release(name, pkg, detect_platform(pkg.min_glibc_version))
    if latest == active.tag:
        logger.info("%s %s is up to date", name, latest)
        return
    install_package(name, explicit=active.explicit, tag=latest)
    records = load_records()
    activate(records[f"{name}@{latest}"], records)
    try:
        uninstall_packages([active.key])
    except PmgError as e:
        logger.info("kept %s: %s", active.key, e)


@contextlib.contextmanager
def exit_on_error() -> Generator[None]:
    """Logs a `PmgError` and exits with code 1."""
    try:
        yield
    except PmgError as e:
        logger.error("error: %s", e)  # noqa: TRY400
        raise SystemExit(1) from e


def needs_installing(
    name: str, tags: list[str | None], explicit: bool, specifier: SpecifierSet
) -> bool:
    """Checks whether installing may add a version of the package, without recording anything."""
    pkg = load_spec(name)
    host = detect_platform(pkg.min_glibc_version)
    versions = [record for record in load_records().values() if record.name == name]
    return not all(
        nothing_to_install(name, pkg, host, versions, explicit, tag, specifier, False)
        for tag in tags
    )


def needed_packages(
    requested: dict[str, list[str | None]], order: list[str], specifiers: dict[str, SpecifierSet]
) -> set[str]:
    """Returns the requested packages and the dependencies of those that get installed."""
    needed = set(requested)
    # dependents come before their dependencies, so a package found outside pmg or not for the
    # host pulls in none of its dependencies
    for name in reversed(order):
        tags = requested.get(name, [None])
        if name in needed and needs_installing(
            name, tags, name in requested, specifiers.get(name, SpecifierSet())
        ):
            spec = load_spec(name)
            deps = package_deps(spec, detect_platform(spec.min_glibc_version))
            needed |= {dep.name for dep in requirements(deps)}
    return needed


def install(names: list[str]) -> None:
    """Installs packages and their dependencies.

    Args:
        names: Names of the packages to install, each optionally with a release tag as name@tag.
    """
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


def uninstall(names: list[str]) -> None:
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


def upgrade(names: Annotated[list[str] | None, doctyper.Argument()] = None) -> None:
    """Upgrades packages to their latest release.

    Args:
        names: Names of the packages to upgrade, all installed ones if none are given.
    """
    with exit_on_error():
        for name in sorted(set(names or (record.name for record in load_records().values()))):
            upgrade_package(name)


def print_env() -> None:
    """Prints shell code setting the environment and PATH entries of the active versions."""
    paths: list[str] = []
    for record in load_records().values():
        if not record.active or record.name not in available_specs():
            continue
        pkg = load_spec(record.name)
        context = make_context(record.name, pkg, record.tag)
        for key, value in package_env(pkg, context).items():
            print(f"export {key}={shlex.quote(value)}")  # noqa: T201
        paths += [render(path, context) for path in pkg.paths]
    if paths:
        print(f'export PATH={shlex.quote(os.pathsep.join(paths))}:"$PATH"')  # noqa: T201


def update() -> None:
    """Updates the specs from their repo."""
    with exit_on_error():
        update_registry()


def print_schema() -> None:
    """Prints the JSON schema of specs, e.g. for editors."""
    print(msgspec.json.format(msgspec.json.encode(msgspec.json.schema(Package))).decode())  # noqa: T201


def use(name: str) -> None:
    """Makes a version the one the plain command names, man pages, and completions link to.

    Args:
        name: Package version as name@tag.
    """
    with exit_on_error():
        records = load_records()
        record = records.get(name)
        if record is None:  # pragma: no cover
            raise PmgError(f"not installed: {name}")
        activate(record, records)


def list_installed() -> None:
    """Lists the installed package versions."""
    for key, record in load_records().items():
        words = [key, "explicit" if record.explicit else "dependency"]
        if record.active:
            words.append("active")
        if record.external:
            words += ["external", record.external_version or "unknown"]
        print(" ".join(words))  # noqa: T201
