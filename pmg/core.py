"""Resolving, installing, and uninstalling packages from their specs.

Package specs are TOML files named after the package, searched in `$PMG_SPECS_DIR`, then in
`$PMG_HOME/specs`, then in the registry, which `pmg update` downloads from the zshsetup/pmg-specs
repo. Templates in a spec are Jinja templates with
{{ tag }} (the release tag, e.g. "v0.26.1"), {{ version }} (the tag without a leading "v"),
{{ arch }} (the machine as `uname -m` prints it), {{ data }} (`$XDG_DATA_HOME`), {{ bin }} (the bin
dir), {{ dir }} (the package dir), {{ dirs.<key> }} (the extra dirs of the package), and, for
files and download commands, {{ asset }} (the asset name).

Packages install everything else into their own dirs, `$PMG_HOME/packages/<name>@<tag>`, which
they own as a whole. Their commands go to `$PMG_HOME/bin/<cmd>@<tag>`, and the plain names of the
commands, man pages, and completions of the active version link into the shared layout below
`~/.local`, so PATH has no versioned names.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import functools
import graphlib
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import IO, TYPE_CHECKING, TypeVar
from urllib.parse import urlsplit

# typing has override only from Python 3.12 on.
from typing_extensions import override

from pmg.packaging_utils import requirements, tag_version

if TYPE_CHECKING:
    import tarfile
    from collections.abc import Awaitable, Callable, Generator, Iterable

    import jinja2
    from _typeshed import StrPath
    from packaging.specifiers import SpecifierSet
    from packaging.version import Version
    from tqdm import tqdm

    from pmg.consumers import Files, GitHubApi
    from pmg.models import CondaFile, Context, Package, Platform, Record

GH_TOKEN_ENV = "PMG_GH_TOKEN"  # noqa: S105
REGISTRY_URL = "https://github.com/zshsetup/pmg-specs/archive/refs/heads/main.tar.gz"
HOST_PLATFORMS: dict[tuple[str, str], Platform] = {
    ("glibc", "x86_64"): "glibc_x64",
    ("glibc", "aarch64"): "glibc_arm64",
    ("musl", "x86_64"): "musl_x64",
    ("musl", "aarch64"): "musl_arm64",
    ("macos", "arm64"): "macos_arm64",
}
"""Platform for each libc and machine, as reported by `platform.machine`."""
ZSH_COMPLETION = """#compdef pmg
# asks pmg for the completions, like `_PMG_COMPLETE=source_zsh pmg` prints, but autoloadable
local completions
completions="$(env _TYPER_COMPLETE_ARGS="${words[1,$CURRENT]}" _PMG_COMPLETE=complete_zsh pmg)"
# pmg answers _files for paths, and also without a match, where only packages make sense; validate
# is the only command taking paths
[[ "$completions" == _files && "${words[2]}" != validate ]] || eval "$completions"
"""
"""zsh completion of pmg, as an autoloadable function."""
COMPLETION_NAMES = {"zsh": "_{}", "bash": "{}", "fish": "{}.fish"}
"""File name of the completion script of a command for each shell."""
CACHE_SECONDS = 3600
"""Age after which indexes of Alpine and conda packages are downloaded again."""
TAR_SUFFIXES = (".tar", ".tar.gz", ".tgz", ".tar.xz", ".txz", ".tar.bz2", ".tbz2", ".apk")
ZSTD_SUFFIXES = (".tar.zst", ".tzst")
MAX_DOWNLOADS = 6
"""Downloads at once, each with a bar of its own on a terminal."""

T = TypeVar("T")

logger = logging.getLogger(__package__)


class PmgError(Exception):
    """A package could not be resolved, installed, or uninstalled."""


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


def ensure_registry() -> None:
    """Downloads the registry on the first use of pmg."""
    if not registry_dir().exists():
        update_registry()


def is_glob(pattern: str) -> bool:
    """Checks whether a pattern is a glob, i.e. has `*`, `?`, or `[`."""
    return any(char in pattern for char in "*?[")


def glob_names(pattern: str, names: Iterable[str]) -> list[str]:
    """Returns the names the glob matches as a whole, ignoring case."""
    import fnmatch

    return [name for name in names if fnmatch.fnmatchcase(name.lower(), pattern.lower())]


def search_specs(pattern: str | None) -> list[str]:
    """Returns the names of the packages with a spec matching the pattern, ignoring case.

    A glob matches the whole name, any other pattern a part of it; without a pattern, all names
    match. The first use of pmg downloads the registry.
    """
    ensure_registry()
    if pattern is None:
        return list(available_specs())
    if is_glob(pattern):
        return glob_names(pattern, available_specs())
    return [name for name in available_specs() if pattern.lower() in name.lower()]


def expand_globs(args: list[str], names: Iterable[str]) -> list[str]:
    """Replaces the globs among name or name@tag arguments by the names they match, sorted.

    Raises:
        PmgError: If a glob has a tag or matches no name.
    """
    names = list(names)
    expanded: list[str] = []
    for arg in args:
        name, _, tag = arg.partition("@")
        if not is_glob(name):
            expanded.append(arg)
            continue
        if tag:
            raise PmgError(f"a glob cannot have a tag: {arg}")
        if not (matches := glob_names(name, names)):
            raise PmgError(f"no package matches {arg}")
        expanded += sorted(matches)
    # a name both given and matched by a glob counts once
    return list(dict.fromkeys(expanded))


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


def packages_dir() -> Path:
    """Returns the dir of the package dirs, as name@tag, unless a spec sets its own `dir`."""
    return pmg_home() / "packages"


def versions_bin() -> Path:
    """Returns the dir of the commands of all versions, as cmd@tag, which the bin dir links to.

    Outside the bin dir, so that PATH and its completions only have the plain names.
    """
    return pmg_home() / "bin"


def make_context(name: str, pkg: Package, tag: str) -> Context:
    """Resolves the template variables of a package."""
    from pmg.models import Context

    data, bin_dir = data_home(), layout()["bin"]
    arch = platform.machine()
    spec = available_specs().get(name)
    spec_dir = spec.parent if spec else Path()
    base = Context(
        tag=tag,
        arch=arch,
        data=data,
        bin=bin_dir,
        dir=packages_dir() / name,
        dirs={},
        spec_dir=spec_dir,
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
    import msgspec

    from pmg.models import Package

    return msgspec.toml.decode(spec, type=Package)


def find_spec(name: str) -> Path:
    """Returns the spec file of a package, from the first spec dir that has one.

    Raises:
        PmgError: If no spec dir has one.
    """
    path = available_specs().get(name)
    # the first use of pmg downloads the registry, and a later one gets a spec added since
    if path is None:
        update_registry()
        path = available_specs().get(name)
    if path is None:  # pragma: no cover
        dirs = ", ".join(str(directory) for directory in spec_dirs())
        raise PmgError(f"no spec for {name} in {dirs}")
    return path


def load_spec(name: str) -> Package:
    """Loads the spec of a package.

    Raises:
        PmgError: If the spec is missing or invalid.
    """
    import msgspec

    path = find_spec(name)
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
        archive = run_async(download_file(url, Path(tmp) / "registry.tar.gz"))
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
    import msgspec

    from pmg.models import Record

    records_dir = pmg_home() / "installed"
    if not records_dir.is_dir():
        return {}
    records: dict[str, Record] = {}
    for path in sorted(records_dir.glob("*.json")):
        # a package installed at the same time may remove a record, e.g. after a failed test
        with contextlib.suppress(FileNotFoundError):
            records[path.stem] = msgspec.json.decode(path.read_bytes(), type=Record)
    return records


def save_record(record: Record) -> None:
    """Writes the install record of a package version atomically."""
    import msgspec

    path = record_path(record.key)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_bytes(msgspec.json.format(msgspec.json.encode(record)))
    tmp_path.replace(path)


class Session:
    """The HTTP clients of an event loop, which no other loop can use, and its cached downloads."""

    def __init__(self) -> None:
        """Starts without clients, which are created on their first use."""
        self.github: GitHubApi | None = None
        self.files: dict[str, Files] = {}
        """Clients by scheme and host, so that downloads from a host share its connections."""
        self.cached: dict[str, asyncio.Task[Path]] = {}
        """Downloads to the cache by URL, so that concurrent requests share one."""
        self.slots = asyncio.Semaphore(MAX_DOWNLOADS)

    async def close(self) -> None:
        """Closes the connections of the clients."""
        for consumer in [*([self.github] if self.github else []), *self.files.values()]:
            await consumer.session.aclose()


SESSION: contextvars.ContextVar[Session] = contextvars.ContextVar("SESSION")


def run_async(awaitable: Awaitable[T]) -> T:
    """Runs a coroutine in a new event loop, with HTTP clients of its own."""

    async def main() -> T:
        session = Session()
        # the task of main has its own context, which its tasks and threads copy
        SESSION.set(session)
        try:
            return await awaitable
        finally:
            await session.close()

    return asyncio.run(main())


def github_api() -> GitHubApi:
    """Returns the GitHub API client of the event loop, authenticated if a token is set."""
    from mxhttp import BearerAuth

    from pmg.consumers import GitHubApi

    session = SESSION.get()
    if session.github is None:
        token = os.getenv(GH_TOKEN_ENV) or os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN")
        session.github = GitHubApi(auth=BearerAuth(token) if token else None)
    return session.github


def split_repo(repo: str) -> tuple[str, str]:
    """Splits "owner/name" into owner and name."""
    owner, _, name = repo.partition("/")
    return owner, name


@functools.cache
def jinja_env() -> jinja2.Environment:
    """Returns the Jinja environment, which treats undefined variables as errors."""
    import jinja2

    # renders file names and URLs, not HTML.
    return jinja2.Environment(undefined=jinja2.StrictUndefined, keep_trailing_newline=True)  # noqa: S701


def render(template: str, context: Context, **extra: str) -> str:
    """Renders a spec template with the variables of the package and extra variables."""
    return jinja_env().from_string(template).render(**context.variables(), **extra)


def glibc_version() -> Version | None:
    """Returns the glibc version of the host, or None on musl and macOS."""
    from packaging.version import Version

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


def is_for_host(pkg: Package) -> bool:
    """Checks whether a package is for the host.

    Its platforms must include the host, and it must have an asset for the host if its download
    needs one, as a command download with assets does.
    """
    from pmg.models import ApkDownload, CommandDownload

    host = detect_platform(pkg.min_glibc_version)
    if pkg.platforms and host not in pkg.platforms:
        return False
    return (
        isinstance(pkg.download, ApkDownload)
        or (isinstance(pkg.download, CommandDownload) and not pkg.assets)
        or host in pkg.assets
    )


@functools.cache
def system_certificates() -> bool:
    """Checks whether the host has CA certificates where OpenSSL looks for them."""
    import ssl

    paths = ssl.get_default_verify_paths()
    capath = Path(paths.openssl_capath)
    return Path(paths.openssl_cafile).is_file() or (capath.is_dir() and any(capath.iterdir()))


@functools.cache
def spec_shell() -> str:
    """Returns the shell for spec commands.

    bash where available, as dash before 0.5.13 (Ubuntu 24.04) lacks pipefail. Alpine has no bash,
    but its busybox sh has pipefail.
    """
    return shutil.which("bash") or "/bin/sh"


def interactive() -> bool:
    """Checks whether stderr is a terminal, the only place pmg shows progress."""
    return sys.stderr.isatty()


@contextlib.contextmanager
def spinner(text: str) -> Generator[None]:
    """Shows a spinner with the text while the block runs, on a terminal."""
    if not interactive():
        yield
        return
    from rich.console import Console

    with Console(stderr=True).status(text):
        yield


class Board:
    """Shows a bar counting the finished packages, with the steps running for them.

    The bar is the first that tqdm places and the last it closes, so it stays on the first line
    with the bars of the downloads below it, log lines go above it, and the cursor ends at the
    start of the line.
    """

    def __init__(self, desc: str, total: int) -> None:
        """Shows the bar."""
        from tqdm import tqdm

        # without a rate, as packages take from no time to minutes
        self.bar: tqdm[None] = tqdm(
            total=total,
            desc=desc,
            leave=False,
            file=sys.stderr,
            bar_format="{desc}: {n}/{total} |{bar}| {elapsed}{postfix}",
        )
        self.running: list[str] = []
        self.lock = threading.Lock()

    @contextlib.contextmanager
    def step(self, text: str) -> Generator[None]:
        """Names the step after the bar while the block runs."""
        with self.lock:
            self.running.append(text)
            self.bar.set_postfix_str(", ".join(self.running))
        try:
            yield
        finally:
            with self.lock:
                self.running.remove(text)
                self.bar.set_postfix_str(", ".join(self.running))

    def advance(self) -> None:
        """Counts a package as done."""
        with self.lock:
            self.bar.update()

    def write(self, line: str) -> None:
        """Writes a line above the bars."""
        from tqdm import tqdm

        tqdm.write(line, file=sys.stderr)

    async def tick(self) -> None:
        """Updates the elapsed time every second, also while no step finishes."""
        while True:
            await asyncio.sleep(1)
            with self.lock:
                self.bar.refresh()


BOARD: contextvars.ContextVar[Board | None] = contextvars.ContextVar("BOARD", default=None)


class LogHandler(logging.Handler):
    """Writes log lines to stderr, above the bars while a board shows them."""

    @override
    def emit(self, record: logging.LogRecord) -> None:
        """Writes the line of a record."""
        try:
            line = self.format(record)
            board = BOARD.get()
            if board is None:
                # the current stderr, which a spinner may redirect to print above itself
                sys.stderr.write(f"{line}\n")
                sys.stderr.flush()
            else:
                board.write(line)
        except Exception:  # noqa: BLE001  # pragma: no cover
            self.handleError(record)


@contextlib.contextmanager
def activity(text: str) -> Generator[None]:
    """Shows the text while the block runs, on the board if there is one, else as a spinner."""
    board = BOARD.get()
    if board is None:
        with spinner(text):
            yield
        return
    with board.step(text):
        yield


def run_shell(
    name: str, step: str, cmd: str, cwd: Path | None = None, env: dict[str, str] | None = None
) -> str:
    """Runs a spec command with `set -euo pipefail` and returns its output.

    Its stderr is only shown if it fails.

    Raises:
        PmgError: If the command fails.
    """
    certificates: dict[str, str] = {}
    if not system_certificates():
        import certifi

        certificates = {"CURL_CA_BUNDLE": certifi.where(), "SSL_CERT_FILE": certifi.where()}

    if cwd is None:
        # not the current dir, where a spec dir holding uv.toml would configure uv
        cwd = pmg_home()
        cwd.mkdir(parents=True, exist_ok=True)
    # spec commands are shell commands by design; builds in post_install take minutes
    with activity(f"{step} of {name}"):
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
                # the certificates pmg downloads with, for hosts without their own
                **certificates,
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


async def cached_download(url: str) -> Path:
    """Downloads `url` to the cache, unless it was downloaded within the last hour.

    Concurrent calls for a URL share its download.
    """
    session = SESSION.get()
    if url not in session.cached:
        session.cached[url] = asyncio.create_task(refresh_cached(url))
    return await session.cached[url]


async def refresh_cached(url: str) -> Path:
    """Downloads `url` to the cache, unless the cached copy is younger than an hour."""
    import hashlib

    path = cache_dir() / hashlib.sha256(url.encode()).hexdigest()[:16]
    if path.exists() and time.time() - path.stat().st_mtime < CACHE_SECONDS:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    return await download_file(url, path)


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
def read_apk_index(index: Path) -> dict[str, str]:
    """Maps the packages in the downloaded index of an Alpine repo to their versions."""
    import tarfile

    with tarfile.open(index) as tar_file:
        member = tar_file.extractfile("APKINDEX")
        if member is None:  # pragma: no cover
            raise PmgError(f"{index} has no APKINDEX")
        text = member.read().decode()
    versions: dict[str, str] = {}
    # blocks of "X:value" lines, P is the package and V its version
    for block in text.split("\n\n"):
        fields = {line[0]: line[2:] for line in block.splitlines() if line[1:2] == ":"}
        if "P" in fields and "V" in fields:
            versions[fields["P"]] = fields["V"]
    return versions


async def apk_version(repo: str, package: str) -> str:
    """Returns the version of a package in an Alpine repo.

    Raises:
        PmgError: If the repo has no such package.
    """
    version = read_apk_index(await cached_download(f"{repo}/APKINDEX.tar.gz")).get(package)
    if version is None:  # pragma: no cover
        raise PmgError(f"{repo} has no package {package}")
    return version


async def conda_file(channel: str, package: str, version: str | None = None) -> CondaFile:
    """Returns the newest .conda file of a package, of `version` if given.

    Raises:
        PmgError: If the package has no such file.
    """
    import msgspec

    from pmg.models import CondaFile

    api = os.getenv("PMG_CONDA_API") or "https://api.anaconda.org"
    listing = await cached_download(f"{api}/package/{channel}/{package}/files")
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


async def fetch_release(name: str, pkg: Package, host: Platform) -> str:
    """Returns the latest tag.

    A release command runs in a thread, the other releases are HTTP requests.

    Raises:
        PmgError: If a release command fails or prints no tag.
    """
    from pmg.models import ApkRelease, CommandRelease, CondaRelease, GitHubRelease

    rl = pkg.release
    if isinstance(rl, GitHubRelease):
        return (await github_api().latest_release(*split_repo(rl.repo))).tag_name
    if isinstance(rl, CommandRelease):
        tag = (await asyncio.to_thread(run_shell, name, "release command", rl.cmd)).strip()
        if not tag:  # pragma: no cover
            raise PmgError(f"release command of {name} printed no tag")
        return tag
    if isinstance(rl, ApkRelease):
        return await apk_version(alpine_repo(), rl.package)
    if isinstance(rl, CondaRelease):
        return (await conda_file(rl.channel, asset_name(pkg, host))).version
    return rl.tag


async def download_file(url: str, dest: Path, checksum: str | None = None) -> Path:
    """Downloads `url` to `dest`, verifying `checksum` ("sha256:<hex>") if given.

    At most `MAX_DOWNLOADS` run at once, so that their bars fit on the terminal.
    """
    from pmg.consumers import Files, TransientProgress

    session = SESSION.get()
    parts = urlsplit(url)
    host = f"{parts.scheme}://{parts.netloc}"
    path = parts.path.lstrip("/") + (f"?{parts.query}" if parts.query else "")
    files = session.files.get(host)
    if files is None:
        files = session.files[host] = Files(base_url=host, follow_redirects=True)
    async with session.slots:
        download = await files.download(path=path)
        # overwrite, as a .part left by a failed checksum would otherwise be resumed.
        if not interactive():
            return await download(dest, checksum=checksum, overwrite=True)
        with TransientProgress(desc=dest.name, file=sys.stderr) as progress:
            return await download(dest, checksum=checksum, overwrite=True, on_progress=progress)


async def run_download(
    pkg: Package, context: Context, host: Platform, dl_dir: StrPath
) -> list[Path]:
    """Downloads the archives of the host platform to `dl_dir`, all at once.

    Raises:
        PmgError: If there is no asset for the host platform.
    """
    from pmg.models import ApkDownload, ApkRelease, CommandDownload, CondaDownload, GitHubDownload

    dl_dir = Path(dl_dir)
    dl, tag = pkg.download, context.tag
    if isinstance(dl, CommandDownload):
        # the command installs into the staging dir itself, see run_install
        return []
    if isinstance(dl, ApkDownload):
        repo = alpine_repo()
        release = pkg.release.package if isinstance(pkg.release, ApkRelease) else None
        versions = {
            package: tag if package == release else await apk_version(repo, package)
            for package in dl.packages
        }
        return list(
            await asyncio.gather(
                *(
                    download_file(
                        f"{repo}/{package}-{version}.apk", dl_dir / f"{package}-{version}.apk"
                    )
                    for package, version in versions.items()
                )
            )
        )
    asset = render(asset_name(pkg, host), context)
    if isinstance(dl, CondaDownload):
        file = await conda_file(dl.channel, asset, tag)
        mirror = os.getenv("PMG_CONDA_URL") or "https://conda.anaconda.org"
        checksum = f"sha256:{file.sha256}" if file.sha256 else None
        url = f"{mirror}/{dl.channel}/{file.basename}"
        return [await download_file(url, dl_dir / Path(file.basename).name, checksum)]
    if isinstance(dl, GitHubDownload):
        if not dl.repo:  # pragma: no cover
            raise RuntimeError("GitHubDownload's repo not set in __post_init__")
        info = await github_api().release(*split_repo(dl.repo), tag=tag)
        found = next((a for a in info.assets if a.name == asset), None)
        if found is None:  # pragma: no cover
            raise PmgError(f"{dl.repo} {tag} has no asset {asset}")
        return [await download_file(found.browser_download_url, dl_dir / asset, found.digest)]
    url = render(dl.url, context, asset=asset)
    return [await download_file(url, dl_dir / Path(urlsplit(url).path).name)]


def extract_tar(tar_file: tarfile.TarFile, dest: Path) -> None:
    """Extracts all members into `dest` with the data filter, which refuses unsafe members.

    Raises:
        PmgError: If Python lacks the filter, which 3.10.12 and 3.11.4 backported.
    """
    import tarfile

    if not hasattr(tarfile, "data_filter"):  # pragma: no cover
        raise PmgError("extracting archives safely needs Python 3.10.12, 3.11.4, or newer")
    tar_file.extractall(dest, filter="data")


def extract_tar_zst(fileobj: IO[bytes], dest: Path) -> None:
    """Extracts a zstd-compressed tar stream into `dest`."""
    import tarfile

    import zstandard

    with (
        zstandard.ZstdDecompressor().stream_reader(fileobj) as reader,
        tarfile.open(fileobj=reader, mode="r|") as tar_file,
    ):
        extract_tar(tar_file, dest)


def unpack(archive: Path, dest: Path) -> None:
    """Unpacks the archive into `dest`; other files count as a bare binary.

    Alpine packages lose their metadata files, conda packages keep only their payload.
    """
    import tarfile
    import zipfile

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
            extract_tar(tar_file, dest)
        if archive.name.endswith(".apk"):
            for path in dest.glob(".*"):
                remove_path(path)
    else:
        shutil.copy2(archive, dest / archive.name)


def unpack_all(archives: list[Path], dest: Path) -> None:
    """Unpacks the archives into `dest`, one after another."""
    for archive in archives:
        unpack(archive, dest)


def unpack_apart(archives: list[Path], dest: Path) -> None:
    """Unpacks the archives into `dest` in a process of its own.

    tarfile handles each member in Python, which holds the GIL, so threads unpacking together
    would take turns. A spawned process, as forking one with threads may deadlock.
    """
    if not archives:
        return
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor

    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=1, mp_context=context) as pool:
        pool.submit(unpack_all, archives, dest).result()


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


def stage_man_pages(
    templates: list[str], context: Context, content: Path, asset: str, target: Path
) -> None:
    """Places the man pages each path or glob matches in the archive in the staging dir.

    Raises:
        PmgError: If a path or glob matches no file.
    """
    for template in templates:
        pattern = render(template, context, asset=asset)
        if not (pages := sorted(path for path in content.glob(pattern) if path.is_file())):
            raise PmgError(f"{asset} has no {pattern}")
        for page in pages:
            section = Path(page.name.removesuffix(".gz")).suffix.removeprefix(".")
            (target / "man" / f"man{section}").mkdir(exist_ok=True)
            shutil.copy2(page, target / "man" / f"man{section}" / page.name)


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
    stage_man_pages(pkg.man, context, content, asset, target)
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
    """Renders the environment of the package, without the variables that render empty."""
    env = {key: render(value, context) for key, value in pkg.env.items()}
    return {key: value for key, value in env.items() if value}


def prepended(pkg: Package, context: Context) -> dict[str, list[str]]:
    """Renders the entries of the path-like variables, without those that render empty."""
    rendered = {
        key: [entry for entry in (render(value, context) for value in values) if entry]
        for key, values in pkg.prepend.items()
    }
    return {key: entries for key, entries in rendered.items() if entries}


def command_env(pkg: Package, context: Context) -> dict[str, str]:
    """Returns the environment of the spec commands, with the entries before the current values."""
    env = package_env(pkg, context)
    for key, entries in prepended(pkg, context).items():
        env[key] = os.pathsep.join([*entries, *filter(None, [os.getenv(key)])])
    return env


def installed_context(record: Record, pkg: Package, records: dict[str, Record]) -> Context:
    """Resolves the template variables of an installed version, with its dependencies."""
    context = make_context(record.name, pkg, record.tag)
    context.deps = dependency_vars(pkg, detect_platform(pkg.min_glibc_version), records)
    return context


def staged_context(context: Context, target: Path) -> Context:
    """Returns the context with the package dirs in the staging dir."""
    import msgspec

    dirs = {key: target / "dirs" / key for key in context.dirs}
    return msgspec.structs.replace(context, dir=target / "dir", dirs=dirs)


def run_install(
    name: str, pkg: Package, context: Context, archives: list[Path], target: Path
) -> None:
    """Unpacks the archives and places the files and dirs of the package in the staging dir.

    Release archives lose a single top-level dir; Alpine and conda packages keep their layout.
    A command download runs its command instead, with the environment of the package and, if the
    package has assets, {{ asset }}.

    Raises:
        PmgError: If a file is missing from the archives or a spec command fails.
    """
    from pmg.models import CommandDownload, GitHubDownload, UrlDownload

    env = command_env(pkg, staged_context(context, target)) | {"PREFIX": str(target)}
    if isinstance(pkg.download, CommandDownload):
        host = detect_platform(pkg.min_glibc_version)
        extra = {"asset": render(asset_name(pkg, host), context)} if pkg.assets else {}
        cmd = render(pkg.download.cmd, context, **extra)
        run_shell(name, "download command", cmd, target, env)
    content = target.parent / "unpacked"
    content.mkdir()
    with activity(f"unpacking of {name}"):
        unpack_apart(archives, content)
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

    Commands get @tag appended in the versions bin, man pages and completions go to the version
    store.

    Raises:
        PmgError: If the file is outside the layout.
    """
    roots = layout()
    if relative.parts[0] not in roots:  # pragma: no cover
        raise PmgError(f"{relative} is outside the install layout {sorted(roots)}")
    if relative.parts[0] == "bin":
        return versioned(versions_bin().joinpath(*relative.parts[1:]), tag)
    return version_store(name, tag) / relative


def active_links(record: Record) -> dict[Path, Path]:
    """Maps the plain paths in the shared layout to the files of the version they link to."""
    roots, store, commands = layout(), version_store(record.name, record.tag), versions_bin()
    links: dict[Path, Path] = {}
    for file in map(Path, record.files):
        if file.is_relative_to(store):
            relative = file.relative_to(store)
            links[roots[relative.parts[0]].joinpath(*relative.parts[1:])] = file
        else:
            # the other files are the commands, as cmd@tag in the versions bin
            relative = file.relative_to(commands)
            links[
                roots["bin"] / relative.with_name(relative.name.removesuffix(f"@{record.tag}"))
            ] = file
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
        # relative, e.g. bin/bat -> ../share/pmg/bin/bat@v0.26.1, so the layout can move as a whole
        link.symlink_to(os.path.relpath(target, link.parent))
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


external_checks: dict[str, tuple[bool, str | None]] = {}
"""Results of `check_external` by package, as an install asks for a package more than once."""


def check_external(name: str, pkg: Package) -> tuple[bool, str | None]:
    """Detects a copy of the package that pmg did not install.

    The files and libraries of the check must exist, and its command must run. Without any of
    them, the first command of the package, or else its name, is looked up in PATH.

    Returns:
        Whether there is a copy, and its version if the check found one.
    """
    from pmg.models import Check

    context = make_context(name, pkg, "external")
    check = pkg.check or Check()
    files = [Path(render(file, context)) for file in check.files]
    libs = [render(lib, context) for lib in check.libs]
    if not all(file.exists() for file in files) or (libs and not libs_load(libs)):
        return False, None
    cmd = [render(arg, context) for arg in check.cmd] if check.cmd else None
    if cmd is None and not files and not libs:
        # packages that build their commands in post_install have none in the spec
        command = next(iter([*pkg.bin, *pkg.links]), name)
        cmd = [command, *check.args]
    if cmd is None:
        return True, None
    return run_version_command(cmd, check.regex, check.dev_tool)


def check_externals(names: Iterable[str]) -> None:
    """Checks for copies of the packages outside pmg all at once, as the checks run commands.

    Only packages without a version of pmg are checked, or with a recorded external one, which
    are those that `find_external` is asked for.
    """
    from concurrent.futures import ThreadPoolExecutor

    records = load_records().values()
    installed = {record.name for record in records if not record.external}
    external = {record.name for record in records if record.external}
    specs = {
        name: load_spec(name)
        for name in names
        if (name not in installed or name in external) and name not in external_checks
    }
    with ThreadPoolExecutor() as pool:
        results = pool.map(lambda name: check_external(name, specs[name]), specs)
        external_checks.update(zip(specs, results, strict=True))


def find_external(name: str, pkg: Package) -> Record | None:
    """Returns a record of a copy of the package that pmg did not install, see `check_external`."""
    from pmg.models import Record

    # installs check all packages up front, see check_externals
    found, version = external_checks.get(name) or external_checks.setdefault(
        name, check_external(name, pkg)
    )
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


def refresh_external(name: str, pkg: Package, versions: list[Record]) -> list[Record]:
    """Checks that the recorded external version is still there, e.g. not removed by brew.

    Returns:
        The versions without the external one if it is gone, whose record is then removed; a
        changed version of one still there is recorded.
    """
    recorded = next((version for version in versions if version.external), None)
    if recorded is None:
        return versions
    found = find_external(name, pkg)
    if found is None:
        record_path(recorded.key).unlink()
        logger.info("%s is no longer found outside pmg", name)
        return [version for version in versions if version is not recorded]
    if found.external_version != recorded.external_version:
        recorded.external_version = found.external_version
        save_record(recorded)
    return versions


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
    from packaging.specifiers import SpecifierSet

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
    from packaging.requirements import Requirement

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
    external: bool = True,
) -> bool:
    """Checks whether the package is not for the host, or an installed or external one is enough.

    An external one only counts if `external` is set.

    Raises:
        PmgError: If the package was requested directly but is not for the host.
    """
    if pkg.platforms and host not in pkg.platforms:
        if explicit:
            raise PmgError(f"{name} is only for {', '.join(pkg.platforms)}, not {host}")
        return True
    if tag is not None:
        return False
    versions = refresh_external(name, pkg, versions)
    if not explicit and any(satisfies(r.version_tag, specifier) for r in versions):
        return True
    return external and use_external(name, pkg, versions, explicit, specifier, record_external)


def version_commands(pkg: Package, context: Context, record: Record) -> dict[str, Path]:
    """Maps the plain names of the commands of an installed version to their files.

    These are its commands in the versions bin, and the executables in its PATH entries.
    """
    commands: dict[str, Path] = {}
    for file in map(Path, record.files):
        if file.parent == versions_bin():
            commands.setdefault(file.name.removesuffix(f"@{record.tag}"), file)
    for entry in (Path(render(path, context)) for path in pkg.paths):
        if entry.is_dir():
            for file in sorted(entry.iterdir()):
                if file.is_file() and os.access(file, os.X_OK):
                    commands.setdefault(file.name, file)
    return commands


def run_test(name: str, pkg: Package, context: Context, record: Record, work: Path) -> None:
    """Runs the test of an installed version, with {{ cmd }} and {{ cmds }} as its commands.

    {{ cmd }} is the main command, the one named like the package or else the first of `bin` and
    `links`; {{ cmds["<name>"] }} are all commands, also those in `paths`. They are links with the
    plain names, as some commands only run under their own name.

    Raises:
        PmgError: If the test fails or uses a command the version lacks.
    """
    import jinja2

    commands = version_commands(pkg, context, record)
    links = work / "bin"
    links.mkdir(parents=True)
    for command, file in commands.items():
        (links / command).symlink_to(file)
    cmds = {command: str(links / command) for command in commands}
    main = next((c for c in [name, *pkg.bin, *pkg.links] if c in cmds), next(iter(cmds), None))
    extra = {"cmds": cmds} | ({"cmd": cmds[main]} if main else {})
    try:
        test = jinja_env().from_string(pkg.test).render(**context.variables(), **extra)
    except jinja2.UndefinedError as e:  # pragma: no cover
        raise PmgError(f"the test of {name} uses a command it lacks: {e}") from e
    paths = [*(render(path, context) for path in pkg.paths), str(layout()["bin"])]
    env = command_env(pkg, context) | {"PATH": os.pathsep.join([*paths, os.getenv("PATH", "")])}
    run_shell(name, "test", test, work, env)


class Deps:
    """The dependencies of a package that `run_units` installs or upgrades at the same time."""

    def __init__(
        self,
        name: str = "",
        done: dict[str, asyncio.Event] | None = None,
        failed: set[str] | None = None,
        skip_failed: bool = False,
    ) -> None:
        """Takes the events set when each is done, none for a package installed on its own.

        Args:
            name: Name of the package.
            done: Events of the dependencies, set when they are done.
            failed: Packages that failed, filled while the dependencies are done.
            skip_failed: Whether the package is skipped if a dependency failed.
        """
        self.name = name
        self.done = done or {}
        self.failed = failed if failed is not None else set()
        self.skip_failed = skip_failed

    @property
    def pending(self) -> bool:
        """Whether some are not done yet."""
        return not all(event.is_set() for event in self.done.values())

    async def ready(self) -> None:
        """Waits until all are done.

        Raises:
            PmgError: If one failed and the package is skipped then.
        """
        for event in self.done.values():
            await event.wait()
        if self.skip_failed and (broken := [dep for dep in self.done if dep in self.failed]):
            raise PmgError(f"skipped {self.name}, as {', '.join(broken)} failed")


async def fetch_release_early(name: str, pkg: Package, host: Platform, deps: Deps) -> str:
    """Returns the latest tag, see `fetch_release`, without waiting for the dependencies first.

    A release command may use a dependency, e.g. curl, but mostly finds what it uses already
    there, so it runs at once, and once more after the dependencies if it failed while they were
    pending; it only prints the tag, so an early run is harmless. Releases over HTTP never wait.
    """
    from pmg.models import CommandRelease

    if isinstance(pkg.release, CommandRelease) and deps.pending:
        with contextlib.suppress(PmgError):
            return await fetch_release(name, pkg, host)
        await deps.ready()
    return await fetch_release(name, pkg, host)


async def install_package(  # noqa: PLR0913, PLR0917
    name: str,
    explicit: bool,
    tag: str | None = None,
    specifier: SpecifierSet | None = None,
    external: bool = True,
    deps: Deps | None = None,
) -> None:
    """Installs a version of a package without its dependencies.

    Installs the latest version, or `tag`. The latest version becomes active; a given tag only if
    no other version is active. An installed version is only marked as explicit if requested.

    The release and the downloads start at once, see `fetch_release_early`; the install steps run
    in a thread once the dependencies are ready, as they may use them.

    Args:
        name: Name of the package.
        explicit: Whether the package was requested directly.
        tag: Release tag to install instead of the latest one.
        specifier: Versions its dependents accept; an installed one satisfies a dependency.
        external: Whether a version found outside pmg is enough.
        deps: Dependencies installed at the same time.

    Raises:
        PmgError: If the package is not for the host, or its latest release does not meet the
            specifier.
    """
    from packaging.specifiers import SpecifierSet

    deps = deps or Deps()
    specifier = specifier or SpecifierSet()
    records = load_records()
    pkg = load_spec(name)
    host = detect_platform(pkg.min_glibc_version)
    versions = [record for record in records.values() if record.name == name]
    if nothing_to_install(name, pkg, host, versions, explicit, tag, specifier, external=external):
        return
    current = active_version(records, name)
    should_activate = tag is None or current is None
    if tag is None:
        tag = await fetch_release_early(name, pkg, host, deps)
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
        archives = await run_download(pkg, context, host, target.parent / "download")
        await deps.ready()
        records = load_records()
        context.deps = dependency_vars(pkg, host, records)
        await asyncio.to_thread(
            install_staged, name, pkg, context, archives, target, explicit, should_activate
        )
    logger.info("installed %s %s", name, tag)


def install_staged(  # noqa: PLR0913, PLR0917
    name: str,
    pkg: Package,
    context: Context,
    archives: list[Path],
    target: Path,
    explicit: bool,
    should_activate: bool,
) -> None:
    """Installs the downloaded archives of a version from the staging dir, see `install_package`.

    The test runs once the files are in place; if it fails, they are removed again.
    """
    from pmg.models import Record

    tag = context.tag
    host = detect_platform(pkg.min_glibc_version)
    records = load_records()
    current = active_version(records, name)
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
        run_test(name, pkg, context, record, target.parent / "test")
        save_record(record)
        if should_activate:
            activate(record, {**records, record.key: record})
    except BaseException:
        rewind_state(moved)
        # the record dir may be what failed.
        with contextlib.suppress(OSError):
            record_path(record.key).unlink(missing_ok=True)
        raise


def uninstall_version(record: Record) -> None:
    """Runs the uninstall hook of a package version and removes its files, dirs, and record.

    Raises:
        PmgError: If the uninstall hook fails.
    """
    pkg = load_spec(record.name) if record.name in available_specs() else None
    # external versions only lose their record
    if pkg and pkg.uninstall and not record.external:
        context = installed_context(record, pkg, load_records())
        env = command_env(pkg, context)
        run_shell(record.name, "uninstall", render(pkg.uninstall, context), env=env)
    if record.active:
        unlink_active(record)
    for path in [*record.files, *record.dirs]:
        remove_path(Path(path))
    # the files are gone, but not the dirs holding them per kind
    shutil.rmtree(version_store(record.name, record.tag), ignore_errors=True)
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


async def upgrade_package(name: str, deps: Deps | None = None) -> None:
    """Upgrades the active version of a package to the latest release.

    Packages with an upgrade command update in place to a newer release, which their record keeps
    as upgraded_tag. Others get the latest release next to the active version, which it replaces
    unless a dependent still needs it. Like `install_package`, commands wait for `deps`.
    """
    import msgspec

    deps = deps or Deps()
    active = active_version(load_records(), name)
    # external versions, which are never active, are left to their package manager
    if active is None:
        return
    pkg = load_spec(name)
    latest = await fetch_release_early(name, pkg, detect_platform(pkg.min_glibc_version), deps)
    if latest == active.current_tag:
        logger.info("%s %s is up to date", name, latest)
        return
    if pkg.upgrade:
        await deps.ready()
        context = installed_context(active, pkg, load_records())
        cmd, env = render(pkg.upgrade, context), command_env(pkg, context)
        await asyncio.to_thread(run_shell, name, "upgrade", cmd, env=env)
        save_record(msgspec.structs.replace(active, upgraded_tag=latest))
        logger.info("upgraded %s in place to %s", name, latest)
        return
    await install_package(name, explicit=active.explicit, tag=latest, deps=deps)
    records = load_records()
    activate(records[f"{name}@{latest}"], records)
    try:
        await asyncio.to_thread(uninstall_packages, [active.key])
    except PmgError as e:
        logger.info("kept %s: %s", active.key, e)


async def run_units(
    desc: str,
    deps: dict[str, set[str]],
    unit: Callable[[str, Deps], Awaitable[None]],
    skip_dependents: bool,
) -> list[str]:
    """Runs the unit of each package at once, which waits for its dependencies where it asks to.

    A package is done as soon as its unit returns, also right away if it has nothing to do, which
    lets the units of its dependents go on. On a terminal, a board shows the progress.

    Args:
        desc: What the units do, for the board.
        deps: Dependencies of each package; those of other packages are not waited for.
        unit: Coroutine function of a package, getting its dependencies to wait for.
        skip_dependents: Whether the dependents of a failed package fail as well when they wait.

    Returns:
        The packages whose unit failed, in the order of `deps`.

    Raises:
        PmgError: If the dependencies form a cycle, which would wait forever.
    """
    try:
        graphlib.TopologicalSorter(deps).prepare()
    except graphlib.CycleError as e:
        raise PmgError(f"dependency cycle: {' -> '.join(e.args[1])}") from e
    done = {name: asyncio.Event() for name in deps}
    failed: set[str] = set()
    board = Board(desc, len(deps)) if interactive() and deps else None
    BOARD.set(board)

    async def run(name: str) -> None:
        events = {dep: done[dep] for dep in sorted(deps[name] & done.keys())}
        try:
            await unit(name, Deps(name, events, failed, skip_dependents))
        # any error only fails the package, as the others go on, e.g. a failed download
        except Exception as e:  # noqa: BLE001
            if isinstance(e, PmgError):
                logger.error("error: %s", e)  # noqa: TRY400
            else:
                # like the errors that end pmg, one line instead of a traceback
                lines = str(e).splitlines()
                logger.error("error: %s: %s", name, lines[0] if lines else type(e).__name__)  # noqa: TRY400
            failed.add(name)
        finally:
            done[name].set()
            if board is not None:
                board.advance()

    ticker = asyncio.create_task(board.tick()) if board is not None else None
    try:
        await asyncio.gather(*(run(name) for name in deps))
    finally:
        if ticker is not None and board is not None:
            ticker.cancel()
            board.bar.close()
        BOARD.set(None)
    return [name for name in deps if name in failed]


def install_packages(requested: dict[str, list[str | None]], external: bool) -> list[str]:
    """Installs the requested packages and the dependencies they need, see `install_package`.

    A package that fails skips only itself and its dependents.

    Args:
        requested: Requested tags by package, None for the latest one.
        external: Whether a version found outside pmg is enough for the requested packages.

    Returns:
        The packages that failed or were skipped.
    """
    order, specifiers = resolve_install_order(list(requested))
    # the checks for copies outside pmg run a command each, so they run together up front
    check_externals(order)
    needed = needed_packages(requested, order, specifiers, external=external)
    deps = {name: dep_names(name) for name in order if name in needed}

    async def unit(name: str, deps: Deps) -> None:
        for tag in requested.get(name, []):
            await install_package(name, explicit=True, tag=tag, external=external, deps=deps)
        # dependencies, or requested packages whose dependents need other versions
        if name not in requested or name in specifiers:
            await install_package(name, explicit=False, specifier=specifiers.get(name), deps=deps)

    return run_async(run_units("installing", deps, unit, skip_dependents=True))


def upgrade_packages(names: Iterable[str]) -> list[str]:
    """Upgrades packages all at once, see `upgrade_package`.

    Returns:
        The packages that failed; the others are upgraded anyway, as the versions they depend on
        stay installed.
    """
    records = load_records()
    deps = {
        name: {dep.name for r in records.values() if r.name == name for dep in requirements(r.deps)}
        for name in sorted(names)
    }
    return run_async(run_units("upgrading", deps, upgrade_package, skip_dependents=False))


@contextlib.contextmanager
def exit_on_error() -> Generator[None]:
    """Logs a `PmgError` and exits with code 1, and updates the env file either way."""
    try:
        yield
    except PmgError as e:
        logger.error("error: %s", e)  # noqa: TRY400
        raise SystemExit(1) from e
    finally:
        write_shell_files()


def needs_installing(
    name: str, tags: list[str | None], explicit: bool, specifier: SpecifierSet, external: bool
) -> bool:
    """Checks whether installing may add a version of the package, without recording anything."""
    pkg = load_spec(name)
    host = detect_platform(pkg.min_glibc_version)
    versions = [record for record in load_records().values() if record.name == name]
    return not all(
        nothing_to_install(name, pkg, host, versions, explicit, tag, specifier, False, external)
        for tag in tags
    )


def dep_names(name: str) -> set[str]:
    """Returns the names of the dependencies of a package on the host."""
    spec = load_spec(name)
    return {
        dep.name
        for dep in requirements(package_deps(spec, detect_platform(spec.min_glibc_version)))
    }


def needed_packages(
    requested: dict[str, list[str | None]],
    order: list[str],
    specifiers: dict[str, SpecifierSet],
    external: bool = True,
) -> set[str]:
    """Returns the requested packages and the dependencies of those that get installed.

    Without `external`, versions found outside pmg do not count for the requested packages.
    """
    from packaging.specifiers import SpecifierSet

    needed = set(requested)
    # dependents come before their dependencies, so a package found outside pmg or not for the
    # host pulls in none of its dependencies
    for name in reversed(order):
        tags = requested.get(name, [None])
        if name in needed and needs_installing(
            name,
            tags,
            name in requested,
            specifiers.get(name, SpecifierSet()),
            external or name not in requested,
        ):
            spec = load_spec(name)
            deps = package_deps(spec, detect_platform(spec.min_glibc_version))
            needed |= {dep.name for dep in requirements(deps)}
    return needed


def env_code() -> str:
    """Returns shell code setting the environment and PATH entries of the active versions."""
    import shlex

    lines: list[str] = []
    paths: list[str] = []
    records = load_records()
    for record in records.values():
        if not record.active or record.name not in available_specs():
            continue
        pkg = load_spec(record.name)
        context = installed_context(record, pkg, records)
        lines += [
            f"export {key}={shlex.quote(value)}" for key, value in package_env(pkg, context).items()
        ]
        # before the value of the shell, which stays
        lines += [
            f'export {key}={shlex.quote(os.pathsep.join(entries))}"${{{key}:+:${key}}}"'
            for key, entries in prepended(pkg, context).items()
        ]
        paths += [render(path, context) for path in pkg.paths]
    if paths:
        lines.append(f'export PATH={shlex.quote(os.pathsep.join(paths))}:"$PATH"')
    return "".join(f"{line}\n" for line in lines)


def write_file(path: Path, text: str) -> None:
    """Writes a file atomically, creating its dir."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_text(text)
    tmp_path.replace(path)


def write_shell_files() -> None:
    """Writes the code of `pmg env` to `$PMG_HOME/env.sh` and the zsh completion of pmg.

    Shells source the env file instead of running pmg; the completion goes to the completions of
    the packages, so it needs no setup of its own.
    """
    write_file(pmg_home() / "env.sh", env_code())
    completion = layout()["zsh"] / "_pmg"
    if not completion.is_file() or completion.read_text() != ZSH_COMPLETION:
        write_file(completion, ZSH_COMPLETION)
