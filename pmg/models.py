"""Package specs, install records, and GitHub API responses."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal, NotRequired, TypeAlias, TypedDict

import msgspec

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
    dir: str | None = None
    """Package dir, if not {{ data }}/<name>."""
    dirs: dict[str, str] = {}
    """Extra dirs owned by the package, available as {{ dirs.<key> }}."""
    content: bool = False
    """Whether the unpacked archive becomes the package dir."""
    bin: dict[str, str] = {}
    """Name in the bin dir mapped to the path in the archive, without a single top-level dir."""
    links: dict[str, str] = {}
    """Name in the bin dir mapped to the target of a symlink."""
    man: list[str] = []
    """Paths of man pages in the archive; the file extension is the section."""
    completions: dict[str, Completions] = {}
    """Command mapped to its completion scripts."""
    check: Check
    post_install: Command | None = None
    """Shell command run in the staging dir `PREFIX`, e.g. to patchelf.

    The staging dir has `bin`, `man`, `zsh`, `bash`, `fish`, `dir`, and `dirs/<key>`.
    """
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
    """Stores the installed state of one version of a package."""

    name: str
    tag: str
    explicit: bool
    """Whether the package was requested directly rather than pulled in as a dependency."""
    active: bool
    """Whether the plain names in the shared layout link to this version."""
    installed_at: float
    deps: list[str]
    files: list[str]
    """Absolute paths of the installed files."""
    dirs: list[str] = []
    """Absolute paths of the dirs owned by the package."""

    @property
    def key(self) -> str:
        """Name and tag as name@tag, which also names the record file."""
        return f"{self.name}@{self.tag}"


class Context(msgspec.Struct, kw_only=True):
    """Stores the values of the template variables for a package."""

    tag: str
    data: Path
    bin: Path
    dir: Path
    dirs: dict[str, Path]

    def variables(self) -> dict[str, object]:
        """Template variables, with paths as strings."""
        return {
            "tag": self.tag,
            "version": self.tag.removeprefix("v"),
            "data": str(self.data),
            "bin": str(self.bin),
            "dir": str(self.dir),
            "dirs": {key: str(path) for key, path in self.dirs.items()},
        }


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


def version_tuple(version: str) -> tuple[int, ...]:
    """Converts a dot-separated version to an integer tuple."""
    return tuple(int(part) for part in version.split("."))
