"""Requirement and version parsing using `packaging`.

Does not import any transient dependencies on import, can be imported globally.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from packaging.requirements import Requirement
    from packaging.version import Version


def requirements(deps: list[str]) -> list[Requirement]:
    """Parses dependencies like "lib>=1.2,<2" or "lib; sys_platform == 'linux'" for the host.

    Dependencies whose environment marker does not match the host are left out.

    Raises:
        ValueError: If a dependency is invalid or has extras or a URL.
    """
    from packaging.requirements import Requirement

    parsed = [Requirement(dep) for dep in deps]
    for requirement in parsed:
        if requirement.extras or requirement.url:  # pragma: no cover
            raise ValueError(
                f"only a name, a version specifier, and a marker are allowed: {requirement}"
            )
    return [
        requirement
        for requirement in parsed
        if not requirement.marker or requirement.marker.evaluate()
    ]


def tag_version(tag: str) -> Version | None:
    """Returns the first version in a release tag, e.g. 1.27.1 in go1.27.1."""
    from packaging.version import InvalidVersion, Version

    match = re.search(r"\d+(?:\.\d+)*", tag)
    if match is None:  # pragma: no cover
        return None
    try:
        return Version(match.group())
    except InvalidVersion:  # pragma: no cover
        return None
