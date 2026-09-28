"""GitHub API consumer and a file downloader.

Imports `mxhttp` on imports, try not to import globally.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, override

from mxhttp import Downloader, RawPath, SyncConsumer, TqdmProgress, base_url, get

from pmg.models import GitHubReleaseInfo  # noqa: TC001

if TYPE_CHECKING:
    from tqdm import tqdm


class TransientProgress(TqdmProgress):
    """Shows the progress of a download in a bar that disappears once the download is done."""

    @override
    def start(
        self,
        initial: int,
        total: int | None,
        *,
        position: int = 0,
        desc: str | None = None,
        leave: bool = True,
    ) -> tqdm:
        """Starts the bar, which is removed when closed instead of left in the output."""
        return super().start(initial, total, position=position, desc=desc, leave=False)


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
