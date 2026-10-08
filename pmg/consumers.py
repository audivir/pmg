"""GitHub API consumer and a file downloader.

Imports `mxhttp` on imports, try not to import globally.
"""

from __future__ import annotations

from typing import Annotated

from mxhttp import (
    AsyncConsumer,
    AsyncDownloader,
    RawPath,
    Retry,
    TqdmProgress,
    base_url,
    get,
    retry,
)
from tqdm import tqdm

# typing has override only from Python 3.12 on.
from typing_extensions import override

from pmg.models import GitHubReleaseInfo  # noqa: TC001


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
        """Starts the bar, which is removed when closed instead of left in the output.

        tqdm places it on the first free line, below the bars of the other downloads and of
        the packages, instead of on the first line, which they share otherwise.
        """
        return tqdm(
            total=total,
            initial=initial,
            desc=desc or self.desc,
            unit=self.unit,
            unit_scale=self.unit_scale,
            unit_divisor=self.unit_divisor,
            mininterval=self.mininterval,
            file=self.file,
            leave=False,
        )


# a connect timeout or a server error retries with backoff, as downloads do on their own
@retry(Retry())
@base_url("https://api.github.com")
class GitHubApi(AsyncConsumer):
    """Wraps the release endpoints of the GitHub API."""

    @get("/repos/{owner}/{name}/releases/latest")
    async def latest_release(self, owner: str, name: str) -> GitHubReleaseInfo:  # type: ignore[empty-body]
        """Fetches the latest release of a repo."""

    @get("/repos/{owner}/{name}/releases/tags/{tag}")
    async def release(self, owner: str, name: str, tag: str) -> GitHubReleaseInfo:  # type: ignore[empty-body]
        """Fetches the release of a repo with the given tag."""


class Files(AsyncConsumer):
    """Wraps file downloads from a single host."""

    @get("/{path}")
    async def download(self, path: Annotated[str, RawPath]) -> AsyncDownloader:  # type: ignore[empty-body]
        """Binds the download of a path on the host."""
