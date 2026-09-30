"""Package specs, install records, and GitHub API responses.

Imports `msgspec` on import, try not to import globally.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Literal, NotRequired, TypeAlias, TypedDict

import msgspec

from pmg.packaging_utils import requirements

if TYPE_CHECKING:
    from packaging.version import Version

Command: TypeAlias = str
Platform: TypeAlias = Literal["glibc_x64", "glibc_arm64", "musl_x64", "musl_arm64", "macos_arm64"]


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


class ApkRelease(BaseStruct, tag="apk", kw_only=True):
    """Stores the Alpine package whose version in the main repo is the latest version."""

    package: str


class CondaRelease(BaseStruct, tag="conda", kw_only=True):
    """Stores the conda channel whose newest build of the asset package is the latest version."""

    channel: str


Release: TypeAlias = GitHubRelease | CommandRelease | StaticRelease | ApkRelease | CondaRelease


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


class ApkDownload(BaseStruct, tag="apk", kw_only=True):
    """Stores the Alpine packages unpacked together into the package dir."""

    packages: list[str]


class CondaDownload(BaseStruct, tag="conda", kw_only=True):
    """Stores the conda channel of the asset package."""

    channel: str


Download: TypeAlias = GitHubDownload | UrlDownload | CommandDownload | ApkDownload | CondaDownload


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


class GeneratedCompletion(BaseStruct, kw_only=True):
    """Stores the shell command printing a completion script, run with the staged bin in PATH."""

    cmd: Command


Completion: TypeAlias = str | GeneratedCompletion


class Completions(BaseStruct, kw_only=True):
    """Stores the completion script of a command for each shell, as archive path or command."""

    zsh: Completion | None = None
    bash: Completion | None = None
    fish: Completion | None = None


class Check(BaseStruct, kw_only=True):
    """Stores how to detect a copy of the package that pmg did not install."""

    files: list[str] = []
    """Files that must exist."""
    libs: list[str] = []
    """Libraries the dynamic loader must find."""
    cmd: list[str] | None = None
    """Command printing the version; without it, files, and libs, the first command of the
    package, or else its name, with `args` is looked up in PATH."""
    args: list[str] = msgspec.field(default_factory=lambda: ["--version"])
    regex: str = r"\d+(?:\.\d+)+"
    """Pattern whose first match in the output is the version."""
    dev_tool: bool = False
    """Whether macOS ships the command as a stub in /usr/bin, which only counts with the developer
    tools installed."""


class Package(BaseStruct, kw_only=True):
    """Stores the spec of a package."""

    release: Release
    download: Download
    test: Command
    """Shell command checking an installed version, run before it is recorded.

    It must use the version itself, through {{ cmd }} (its main command), {{ cmds["<name>"] }}
    (any of its commands), {{ dir }}, or {{ dirs.<key> }}, so that no copy outside pmg passes it.
    """
    deps: list[str] = []
    """Names of the packages this one needs, each optionally with a version specifier."""
    platforms: list[Platform] = []
    """Platforms the package is for, if not all; other hosts skip it as a dependency."""
    platform_deps: dict[Platform, list[str]] = {}
    """Extra dependencies when the package uses the assets of a platform."""
    external: External
    min_glibc: str | None = None
    """Oldest glibc for the glibc assets; older glibc hosts get the musl assets."""
    assets: Assets = {}
    dir: str | None = None
    """Package dir, if not $PMG_HOME/packages/<name>; @tag is appended either way."""
    dirs: dict[str, str] = {}
    """Extra dirs owned by the package, available as {{ dirs.<key> }}."""
    content: bool | str = False
    """Whether the unpacked archive, or its subdir matching this glob, becomes the package dir."""
    keep: list[str] = []
    """Globs of the only files kept in the package dir."""
    remove: list[str] = []
    """Globs of files removed from the package dir."""
    bin: dict[str, str] = {}
    """Name in the bin dir mapped to the path in the archive, without a single top-level dir."""
    links: dict[str, str] = {}
    """Name in the bin dir mapped to the target of a symlink."""
    man: list[str] = []
    """Paths of man pages in the archive; the file extension is the section."""
    completions: dict[str, Completions] = {}
    """Command mapped to its completion scripts."""
    check: Check | None = None
    """How to detect a copy that pmg did not install; the defaults of `Check` if not set."""
    post_install: Command | None = None
    """Shell command run in the staging dir `PREFIX`, e.g. to patchelf or to build from source.

    `CONTENT` is the unpacked archive, or the package dir if it became that.

    The staging dir has `bin`, `man`, `zsh`, `bash`, `fish`, `dir`, and `dirs/<key>`.
    """
    uninstall: Command | None = None
    """Shell command for extra cleanup, run before the installed files are removed."""
    upgrade: Command | None = None
    """Shell command updating the package in place, for packages that update themselves."""
    env: dict[str, str] = {}
    """Environment of the spec commands and, printed by `pmg env`, of the shell.

    During an install, {{ dir }} and {{ dirs.<key> }} point to the staging dir.
    """
    paths: list[str] = []
    """PATH entries printed by `pmg env`, e.g. for commands the package installs itself."""

    def __post_init__(self) -> None:
        """Validates deps, `min_glibc`, and the test, and takes the download repo from the release.

        Raises:
            ValueError: If a dependency, `min_glibc`, the test, or the download repo is invalid.
        """
        from packaging.version import Version

        requirements([*self.deps, *(dep for deps in self.platform_deps.values() for dep in deps)])
        check_test(self.test)
        if self.min_glibc:
            Version(self.min_glibc)
        dl, rl = self.download, self.release
        if isinstance(dl, GitHubDownload) and not dl.repo:
            if not isinstance(rl, GitHubRelease):  # pragma: no cover
                raise ValueError(
                    "GitHubDownload needs to specify the repo if GitHubRelease is not used"
                )
            dl.repo = rl.repo

    @property
    def min_glibc_version(self) -> Version | None:
        """Oldest glibc for the glibc assets as a version."""
        from packaging.version import Version

        return Version(self.min_glibc) if self.min_glibc else None


TEST_VARIABLES = frozenset({"cmd", "cmds", "dir", "dirs"})
"""Template variables naming the files of the version under test."""


def check_test(test: str) -> None:
    """Checks that a test uses the version under test through its template variables.

    Raises:
        ValueError: If the test uses none of `TEST_VARIABLES`.
    """
    import jinja2
    import jinja2.meta

    used = jinja2.meta.find_undeclared_variables(jinja2.Environment().parse(test))  # noqa: S701
    if not used & TEST_VARIABLES:
        names = ", ".join(f"{{{{ {name} }}}}" for name in sorted(TEST_VARIABLES))
        raise ValueError(f"the test must use the installed version through one of {names}")


class Record(BaseStruct, kw_only=True):
    """Stores the installed state of one version of a package."""

    name: str
    tag: str
    explicit: bool
    """Whether the package was requested directly rather than pulled in as a dependency."""
    active: bool
    """Whether the plain names in the shared layout link to this version."""
    installed_at: float
    deps: list[str]
    """Dependencies of the version, as in the spec."""
    files: list[str]
    """Absolute paths of the installed files."""
    dirs: list[str] = []
    """Absolute paths of the dirs owned by the package."""
    external: bool = False
    """Whether the version was found outside pmg, which then leaves its files alone."""
    external_version: str | None = None
    """Version printed by the command of an external version."""

    @property
    def version_tag(self) -> str | None:
        """Tag, or the version of an external version, compared against version specifiers."""
        return self.external_version if self.external else self.tag

    @property
    def key(self) -> str:
        """Name and tag as name@tag, which also names the record file."""
        return f"{self.name}@{self.tag}"


class Context(msgspec.Struct, kw_only=True):
    """Stores the values of the template variables for a package."""

    tag: str
    arch: str
    data: Path
    bin: Path
    dir: Path
    dirs: dict[str, Path]
    spec_dir: Path
    """Dir of the spec, for files shipped next to it."""
    deps: dict[str, dict[str, str]] = {}
    """Package dir and version of each dependency in use, empty if external or skipped."""

    def variables(self) -> dict[str, object]:
        """Template variables, with paths as strings."""
        return {
            "tag": self.tag,
            "version": self.tag.removeprefix("v"),
            "arch": self.arch,
            "data": str(self.data),
            "bin": str(self.bin),
            "dir": str(self.dir),
            "dirs": {key: str(path) for key, path in self.dirs.items()},
            "spec_dir": str(self.spec_dir),
            "deps": self.deps,
        }


class GitHubAsset(msgspec.Struct, kw_only=True):
    """Stores a release asset from the GitHub API."""

    name: str
    browser_download_url: str
    digest: str | None = None
    """Checksum as "sha256:<hex>", only for assets uploaded since mid 2025."""


class CondaFile(msgspec.Struct, kw_only=True):
    """Stores a file of a package from the anaconda.org API."""

    version: str
    basename: str
    """Path in the channel, e.g. linux-64/sysroot_linux-64-2.28-h4a8ded7_9.conda."""
    upload_time: str
    sha256: str | None = None


class GitHubReleaseInfo(msgspec.Struct, kw_only=True):
    """Stores a release from the GitHub API."""

    tag_name: str
    assets: list[GitHubAsset]
