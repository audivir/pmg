"""Integration tests running pmg as a CLI against a local HTTP server.

Set PMG_OFFLINE=1 to skip the test that installs bat from GitHub.
"""

from __future__ import annotations

import dataclasses
import functools
import http.server
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import threading
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypeAlias, override

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

ArchiveFormat: TypeAlias = Literal["tar.gz", "zip", "bare"]

FIXTURE_SPECS = Path(__file__).parent / "fixtures" / "specs"
PLATFORMS = ("glibc_x64", "glibc_arm64", "musl_x64", "musl_arm64", "macos_arm64")


def clean_environ(home: Path) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("XDG_", "PMG_HOME", "PMG_SPECS_DIR"))
    }
    env["HOME"] = str(home)
    return env


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    @override
    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture(scope="module")
def server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[str, Path]]:
    root = tmp_path_factory.mktemp("assets")
    httpd = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(QuietHandler, directory=root)
    )
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}", root
    httpd.shutdown()
    httpd.server_close()


@dataclasses.dataclass
class Env:
    root: Path
    base_url: str
    assets: Path

    @property
    def home(self) -> Path:
        return self.root / "home"

    @property
    def specs(self) -> Path:
        return self.root / "specs"

    @property
    def bin(self) -> Path:
        return self.home / ".local" / "bin"

    @property
    def data(self) -> Path:
        return self.home / ".local" / "share"

    @property
    def pmg_home(self) -> Path:
        return self.root / "pmg-home"

    def add_package(  # noqa: PLR0913
        self,
        name: str,
        *,
        deps: tuple[str, ...] = (),
        archive_format: ArchiveFormat = "tar.gz",
        version: str = "1.0",
        release: str | None = None,
        min_glibc: str | None = None,
        glibc_asset: str | None = None,
        post_install: str | None = None,
        uninstall: str | None = None,
        script: str | None = None,
        files: dict[str, str] | None = None,
        spec: tuple[str, ...] = (),
        bin_entry: bool = True,
    ) -> None:
        script_bytes = (script or f"#!/bin/sh\necho {name} {version}\n").encode()
        if archive_format == "tar.gz":
            asset = f"{name}-{{{{ version }}}}.tar.gz"
            bin_path = "bin/" + name
            entries = {bin_path: (script_bytes, 0o755)} | {
                path: (text.encode(), 0o644) for path, text in (files or {}).items()
            }
            with tarfile.open(self.assets / f"{name}-{version}.tar.gz", "w:gz") as tar_file:
                for path, (data, mode) in entries.items():
                    tar_info = tarfile.TarInfo(f"{name}-{version}/{path}")
                    tar_info.size, tar_info.mode = len(data), mode
                    tar_file.addfile(tar_info, io.BytesIO(data))
        elif archive_format == "zip":
            asset = f"{name}-{{{{ version }}}}.zip"
            bin_path = name
            with zipfile.ZipFile(self.assets / f"{name}-{version}.zip", "w") as zip_file:
                zip_file.writestr(zipfile.ZipInfo("docs/"), b"")
                zip_info = zipfile.ZipInfo(name)
                zip_info.external_attr = 0o755 << 16
                zip_file.writestr(zip_info, script_bytes)
        else:
            asset = f"{name}-{{{{ version }}}}-linux"
            bin_path = "{{ asset }}"
            (self.assets / f"{name}-{version}-linux").write_bytes(script_bytes)
        lines = [f"deps = {json.dumps(list(deps))}", *spec]
        if bin_entry:
            lines.append(f'bin = {{ {name} = "{bin_path}" }}')
        # TOML literal strings, so the shell commands need no escaping
        if post_install:
            lines.append(f"post_install = '{post_install}'")
        if uninstall:
            lines.append(f"uninstall = '{uninstall}'")
        if min_glibc:
            lines.append(f'min_glibc = "{min_glibc}"')
        lines += [
            "check = {}",
            "[external]",
            "[release]",
            release or f'type = "static"\ntag = "v{version}"',
            "[download]",
            'type = "url"',
            f'url = "{self.base_url}/{{{{ asset }}}}"',
            "[assets]",
            *(
                f'{platform} = "{glibc_asset if glibc_asset and "glibc" in platform else asset}"'
                for platform in PLATFORMS
            ),
        ]
        self.specs.mkdir(exist_ok=True)
        (self.specs / f"{name}.toml").write_text("\n".join(lines) + "\n")

    def pmg(
        self, *args: str, ok: bool = True, specs_dir: bool = True
    ) -> subprocess.CompletedProcess[str]:
        env = clean_environ(self.home)
        env["PMG_HOME"] = str(self.pmg_home)
        if specs_dir:
            env["PMG_SPECS_DIR"] = str(self.specs)
        result = subprocess.run(  # noqa: S603
            [sys.executable, "-m", "pmg", *args],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert (result.returncode == 0) == ok, result.stderr
        return result

    def run_bin(self, name: str) -> str:
        return subprocess.check_output([self.bin / name], text=True).strip()  # noqa: S603

    def installed(self) -> dict[str, str]:
        rows = (line.partition(" ") for line in self.pmg("list").stdout.splitlines())
        return {key: state for key, _, state in rows}


@pytest.fixture
def env(tmp_path: Path, server: tuple[str, Path]) -> Env:
    base_url, assets = server
    assets_dir = assets / tmp_path.name
    assets_dir.mkdir()
    return Env(tmp_path, f"{base_url}/{tmp_path.name}", assets_dir)


def test_install_resolves_dependencies(env: Env) -> None:
    env.add_package("app", deps=("lib",))
    env.add_package("lib", deps=("base",))
    env.add_package("base")
    env.pmg("install", "app")
    assert env.run_bin("app") == "app 1.0"
    assert env.run_bin("base") == "base 1.0"
    expected = {
        "app@v1.0": "explicit active",
        "base@v1.0": "dependency active",
        "lib@v1.0": "dependency active",
    }
    assert env.installed() == expected
    # installing again leaves everything as it is
    env.pmg("install", "app")
    assert env.installed() == expected


@pytest.mark.parametrize("archive_format", ["tar.gz", "zip", "bare"])
def test_install_unpacks_archive_formats(env: Env, archive_format: ArchiveFormat) -> None:
    # tar.gz has a top-level dir to strip; zip must keep the executable bit it stores
    env.add_package("tool", archive_format=archive_format)
    env.pmg("install", "tool")
    assert env.run_bin("tool") == "tool 1.0"


def test_man_pages_and_completions(env: Env) -> None:
    env.add_package(
        "tool",
        files={
            "tool.1": ".TH TOOL 1\n",
            "comp/tool.zsh": "#compdef tool\n",
            "comp/tool.bash": "complete -F _tool tool\n",
            "comp/tool.fish": "complete -c tool\n",
        },
        spec=(
            'man = ["tool.1"]',
            (
                "completions.tool = "
                '{ zsh = "comp/tool.zsh", bash = "comp/tool.bash", fish = "comp/tool.fish" }'
            ),
        ),
    )
    env.pmg("install", "tool")
    # installed under the names each shell looks for
    installed = [
        env.data / "man" / "man1" / "tool.1",
        env.data / "zsh" / "site-functions" / "_tool",
        env.data / "bash-completion" / "completions" / "tool",
        env.data / "fish" / "vendor_completions.d" / "tool.fish",
    ]
    assert all(path.is_file() for path in installed)
    env.pmg("uninstall", "tool")
    assert not any(path.exists() for path in installed)


def test_generated_completion_runs_staged_command(env: Env) -> None:
    env.add_package("tool", spec=('completions.tool = { zsh = { cmd = "tool" } }',))
    env.pmg("install", "tool")
    assert (env.data / "zsh" / "site-functions" / "_tool").read_text() == "tool 1.0\n"


def test_content_becomes_package_dir_with_links(env: Env) -> None:
    # like zig, the command finds its files relative to its real path, so bin gets a symlink
    env.add_package(
        "tool",
        script='#!/bin/sh\ncat "$(dirname "$(realpath "$0")")/../lib/data.txt"\n',
        files={"lib/data.txt": "from lib"},
        spec=(
            "content = true",
            'dir = "{{ data }}/tool-root"',
            'links = { tool = "{{ dir }}/bin/tool" }',
        ),
        bin_entry=False,
    )
    env.pmg("install", "tool")
    assert env.run_bin("tool") == "from lib"
    env.pmg("uninstall", "tool")
    assert not (env.data / "tool-root@v1.0").exists()
    assert list(env.bin.iterdir()) == []


def test_extra_dirs_are_owned(env: Env) -> None:
    env.add_package(
        "tool",
        spec=('dirs = { cache = "{{ data }}/tool-cache" }',),
        post_install='echo state > "$PREFIX/dirs/cache/state"',
    )
    env.pmg("install", "tool")
    assert (env.data / "tool-cache" / "state").read_text() == "state\n"
    # the package dir stays absent, as nothing was put into it
    assert not (env.data / "tool@v1.0").exists()
    env.pmg("uninstall", "tool")
    assert not (env.data / "tool-cache").exists()


def test_install_refuses_foreign_package_dir(env: Env) -> None:
    env.add_package("tool", spec=("content = true",))
    (env.data / "tool@v1.0").mkdir(parents=True)
    assert "exists and does not belong to tool" in env.pmg("install", "tool", ok=False).stderr
    assert not (env.bin / "tool").exists()


def test_versions_side_by_side(env: Env) -> None:
    for version in ("1.0", "2.0"):
        env.add_package(
            "tool", version=version, files={"tool.1": ".TH TOOL 1\n"}, spec=('man = ["tool.1"]',)
        )
    env.pmg("install", "tool@v1.0", "tool@v2.0")
    # a given tag only becomes active if no other version is
    assert env.run_bin("tool") == "tool 1.0"
    assert env.run_bin("tool@v2.0") == "tool 2.0"
    env.pmg("use", "tool@v2.0")
    assert env.run_bin("tool") == "tool 2.0"
    assert "tool@v2.0" in str((env.data / "man" / "man1" / "tool.1").resolve())
    env.pmg("uninstall", "tool@v2.0")
    # the remaining version takes over
    assert env.run_bin("tool") == "tool 1.0"
    # the latest version, v2.0 from the spec, becomes active
    env.pmg("install", "tool")
    assert env.installed() == {"tool@v1.0": "explicit", "tool@v2.0": "explicit active"}
    env.pmg("uninstall", "tool@v1.0")
    env.pmg("use", "tool@v2.0")
    assert env.installed() == {"tool@v2.0": "explicit active"}
    env.pmg("uninstall", "tool")
    assert env.installed() == {}
    assert list(env.bin.iterdir()) == []


def test_command_release_sets_the_version(env: Env) -> None:
    env.add_package("tool", version="2.5", release='type = "command"\ncmd = "echo v2.5"')
    env.pmg("install", "tool")
    assert env.run_bin("tool") == "tool 2.5"
    assert env.installed() == {"tool@v2.5": "explicit active"}


def test_failing_command_in_release_pipeline_fails(env: Env) -> None:
    # like a missing curl in a pipeline whose last command succeeds
    env.add_package("tool", release='type = "command"\ncmd = "missing-command | cat"')
    assert "release command of tool failed" in env.pmg("install", "tool", ok=False).stderr
    assert env.installed() == {}


def test_old_glibc_gets_musl_asset(env: Env) -> None:
    # on glibc hosts, the glibc asset would fail with a 404
    env.add_package("tool", min_glibc="99.0", glibc_asset="missing.tar.gz")
    env.pmg("install", "tool")
    assert env.run_bin("tool") == "tool 1.0"


def test_specs_dir_comes_before_pmg_home(env: Env) -> None:
    env.add_package("tool", version="1.0")
    home_specs = env.pmg_home / "specs"
    home_specs.mkdir(parents=True)
    shutil.move(env.specs / "tool.toml", home_specs / "tool.toml")
    env.add_package("tool", version="2.0")
    env.pmg("install", "tool")
    assert env.run_bin("tool") == "tool 2.0"
    env.pmg("uninstall", "tool")
    env.pmg("install", "tool", specs_dir=False)
    assert env.run_bin("tool") == "tool 1.0"


def test_autoremove_removes_orphans_transitively(env: Env) -> None:
    env.add_package("app", deps=("lib",))
    env.add_package("lib", deps=("base",))
    env.add_package("base")
    env.pmg("install", "app")
    env.pmg("uninstall", "app")
    assert env.installed() == {"base@v1.0": "dependency active", "lib@v1.0": "dependency active"}
    env.pmg("autoremove")
    assert env.installed() == {}
    assert list(env.bin.iterdir()) == []


def test_autoremove_keeps_shared_dependencies(env: Env) -> None:
    env.add_package("app", deps=("lib", "base"))
    env.add_package("lib", deps=("base",))
    env.add_package("base")
    env.pmg("install", "app")
    env.pmg("autoremove")
    assert set(env.installed()) == {"app@v1.0", "lib@v1.0", "base@v1.0"}


def test_autoremove_keeps_dependency_requested_directly(env: Env) -> None:
    env.add_package("app", deps=("lib",))
    env.add_package("lib")
    env.pmg("install", "app", "lib")
    env.pmg("uninstall", "app")
    env.pmg("autoremove")
    assert env.installed() == {"lib@v1.0": "explicit active"}


def test_install_promotes_dependency_to_explicit(env: Env) -> None:
    env.add_package("app", deps=("lib",))
    env.add_package("lib")
    env.pmg("install", "app")
    env.pmg("install", "lib")
    env.pmg("uninstall", "app")
    env.pmg("autoremove")
    assert env.installed() == {"lib@v1.0": "explicit active"}


def test_dependency_version_specifier(env: Env) -> None:
    for version in ("1.0", "2.0"):
        env.add_package("lib", version=version)
    env.add_package("app", deps=("lib>=2.0",))
    env.pmg("install", "lib@v1.0")
    # v1.0 is too old for app, so the latest lib comes in next to it
    env.pmg("install", "app")
    assert env.installed() == {
        "app@v1.0": "explicit active",
        "lib@v1.0": "explicit",
        "lib@v2.0": "dependency active",
    }
    assert "app depends on lib>=2.0" in env.pmg("uninstall", "lib@v2.0", ok=False).stderr
    env.pmg("uninstall", "lib@v1.0")


def test_dependency_version_not_released(env: Env) -> None:
    env.add_package("lib")
    env.add_package("app", deps=("lib>=3.0",))
    stderr = env.pmg("install", "app", ok=False).stderr
    assert "needs lib>=3.0, the latest release is v1.0" in stderr


def test_uninstall_refuses_needed_dependency(env: Env) -> None:
    env.add_package("app", deps=("lib",))
    env.add_package("lib")
    env.pmg("install", "app")
    assert "app depends on lib" in env.pmg("uninstall", "lib", ok=False).stderr
    # removing both at once is fine, dependents go first
    env.pmg("uninstall", "lib", "app")
    assert env.installed() == {}


def test_uninstall_hook_runs_before_files_are_removed(env: Env) -> None:
    env.add_package("tool", uninstall='test -x "$HOME/.local/bin/tool" && touch "$HOME/hook-ran"')
    env.pmg("install", "tool")
    env.pmg("uninstall", "tool")
    assert (env.home / "hook-ran").exists()
    assert not (env.bin / "tool").exists()


def test_install_refuses_to_overwrite_foreign_file(env: Env) -> None:
    env.add_package("tool")
    env.bin.mkdir(parents=True)
    (env.bin / "tool").write_text("mine")
    assert "exists and does not belong to tool" in env.pmg("install", "tool", ok=False).stderr
    assert (env.bin / "tool").read_text() == "mine"
    assert [path.name for path in env.bin.iterdir()] == ["tool"]
    assert env.installed() == {}


def test_failed_post_install_leaves_nothing(env: Env) -> None:
    env.add_package("tool", post_install="exit 3")
    assert "exit code 3" in env.pmg("install", "tool", ok=False).stderr
    assert not (env.bin / "tool").exists()
    assert env.installed() == {}
    assert list((env.pmg_home / "tmp").iterdir()) == []


def test_failed_move_removes_files_moved_before(env: Env) -> None:
    # bin/a-copy moves first, then bin/sub/b fails, as a foreign file named sub is in the way
    env.add_package(
        "tool",
        post_install="cp bin/tool bin/a-copy && mkdir bin/sub && cp bin/tool bin/sub/b",
    )
    env.bin.mkdir(parents=True)
    (env.bin / "sub").write_text("mine")
    env.pmg("install", "tool", ok=False)
    assert sorted(path.name for path in env.bin.iterdir()) == ["sub"]
    assert env.installed() == {}


def test_failed_record_write_removes_installed_files(env: Env) -> None:
    env.add_package("tool")
    env.pmg_home.mkdir(parents=True)
    # a file where the dir of the records belongs
    (env.pmg_home / "installed").write_text("")
    env.pmg("install", "tool", ok=False)
    assert not (env.bin / "tool").exists()


def test_post_install_files_are_installed_and_tracked(env: Env) -> None:
    env.add_package("tool", post_install='cp "$PREFIX/bin/tool" "$PREFIX/bin/tool-copy"')
    env.pmg("install", "tool")
    assert env.run_bin("tool-copy") == "tool 1.0"
    env.pmg("uninstall", "tool")
    assert list(env.bin.iterdir()) == []


def test_dependency_cycle(env: Env) -> None:
    env.add_package("a", deps=("b",))
    env.add_package("b", deps=("a",))
    assert "dependency cycle" in env.pmg("install", "a", ok=False).stderr
    assert env.installed() == {}


@pytest.mark.skipif(os.getenv("PMG_OFFLINE") == "1", reason="PMG_OFFLINE=1")
@pytest.mark.parametrize(
    ("name", "args", "extra_files"),
    [
        ("bat", ["--version"], ["man/man1/bat.1", "zsh/site-functions/_bat"]),
        # zig only runs if it finds its lib dir through the symlink
        ("zig", ["version"], ["zig@*/lib"]),
    ],
)
def test_install_from_fixture_spec(
    tmp_path: Path, name: str, args: list[str], extra_files: list[str]
) -> None:
    env = clean_environ(tmp_path)
    env["PMG_SPECS_DIR"] = str(FIXTURE_SPECS)
    subprocess.check_call([sys.executable, "-m", "pmg", "install", name], env=env)  # noqa: S603
    subprocess.check_call([tmp_path / ".local" / "bin" / name, *args])  # noqa: S603
    data = tmp_path / ".local" / "share"
    assert all(list(data.glob(pattern)) for pattern in extra_files)
