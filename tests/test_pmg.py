"""Integration tests running pmg as a CLI against a local HTTP server.

Set PMG_OFFLINE=1 to skip the test that installs bat from GitHub.
"""

from __future__ import annotations

import contextlib
import ctypes.util
import functools
import gzip
import hashlib
import http.server
import io
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import threading
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypeAlias

import msgspec
import pytest
import zstandard
from typing_extensions import override

import pmg.core
from pmg.core import detect_platform

if TYPE_CHECKING:
    from collections.abc import Iterator

ArchiveFormat: TypeAlias = Literal["tar.gz", "tar.zst", "zip", "bare"]

FIXTURE_SPECS = Path(__file__).parent / "fixtures" / "specs"
LINUX = ("glibc_x64", "glibc_arm64", "musl_x64", "musl_arm64")
PLATFORMS = ("glibc_x64", "glibc_arm64", "musl_x64", "musl_arm64", "macos_arm64")
LIBC = ctypes.util.find_library("c") or "libc.so.6"


def tar_bytes(entries: dict[str, tuple[bytes, int]], end: bool = True) -> bytes:
    """Builds an uncompressed tar, without the end-of-archive blocks if `end` is unset."""
    blocks = b""
    for path, (data, mode) in entries.items():
        tar_info = tarfile.TarInfo(path)
        tar_info.size, tar_info.mode = len(data), mode
        blocks += tar_info.tobuf(tarfile.GNU_FORMAT) + data + b"\0" * (-len(data) % 512)
    return blocks + b"\0" * 1024 if end else blocks


def apk_bytes(files: dict[str, tuple[bytes, int]]) -> bytes:
    # like Alpine: the metadata tar is left open, so the gzipped tars read as one
    control = tar_bytes({".PKGINFO": (b"pkgname = test\n", 0o644)}, end=False)
    return gzip.compress(control) + gzip.compress(tar_bytes(files))


def zip_bytes(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zip_file:
        for name, data in members.items():
            zip_file.writestr(name, data)
    return buffer.getvalue()


def alpine_repo(mirror: Path) -> Path:
    release_file = Path("/etc/alpine-release")
    release = (
        f"v{'.'.join(release_file.read_text().split('.')[:2])}"
        if release_file.exists()
        else "latest-stable"
    )
    machine = {"arm64": "aarch64"}.get(platform.machine(), platform.machine())
    return mirror / release / "main" / machine


def clean_environ(home: Path) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("XDG_", "PMG_HOME", "PMG_SPECS_DIR"))
    }
    # only the basics, so no command of the host counts as an external version
    env |= {"HOME": str(home), "PATH": f"/usr/bin{os.pathsep}/bin"}
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


class Env(msgspec.Struct):
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
    def packages(self) -> Path:
        return self.pmg_home / "packages"

    @property
    def system(self) -> Path:
        return self.root / "system"

    def add_system_command(self, name: str, output: str) -> None:
        self.system.mkdir(exist_ok=True)
        (self.system / name).write_text(f"#!/bin/sh\necho {output}\n")
        (self.system / name).chmod(0o755)

    def write_spec(self, name: str, text: str) -> None:
        self.specs.mkdir(exist_ok=True)
        (self.specs / f"{name}.toml").write_text(text)

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
        check: str | None = None,
        external: tuple[str, ...] = (),
        test: str | None = None,
    ) -> None:
        script_bytes = (script or f"#!/bin/sh\necho {name} {version}\n").encode()
        if archive_format in {"tar.gz", "tar.zst"}:
            asset = f"{name}-{{{{ version }}}}.{archive_format}"
            bin_path = "bin/" + name
            entries = {bin_path: (script_bytes, 0o755)} | {
                path: (text.encode(), 0o644) for path, text in (files or {}).items()
            }
            tar = tar_bytes({f"{name}-{version}/{path}": entry for path, entry in entries.items()})
            compress = gzip.compress if archive_format == "tar.gz" else zstandard.compress
            (self.assets / f"{name}-{version}.{archive_format}").write_bytes(compress(tar))
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
        lines = [f"deps = {json.dumps(list(deps))}", *spec, f"test = '{test or '{{ cmd }}'}'"]
        if check:
            lines.append(f"check = {check}")
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
            "[external]",
            *external,
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
        env |= {
            "PMG_HOME": str(self.pmg_home),
            "PATH": f"{self.system}{os.pathsep}{env['PATH']}",
            "PMG_ALPINE_MIRROR": f"{self.base_url}/alpine",
            "PMG_CONDA_API": f"{self.base_url}/conda-api",
            "PMG_CONDA_URL": f"{self.base_url}/conda",
            "PMG_REGISTRY_URL": f"{self.base_url}/registry.tar.gz",
        }
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
        # versions as cmd@tag are in the versions bin, the bin dir only has the plain names
        path = self.pmg_home / "bin" / name if "@" in name else self.bin / name
        return subprocess.check_output([path], text=True).strip()  # noqa: S603

    def installed(self) -> dict[str, str]:
        rows = (line.split() for line in self.pmg("list").stdout.splitlines())
        return {key: " ".join(state) for key, *state in rows}


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


@pytest.mark.parametrize("archive_format", ["tar.gz", "tar.zst", "zip", "bare"])
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


def test_man_page_globs(env: Env) -> None:
    env.add_package(
        "tool",
        files={
            "man/tool.1": ".TH TOOL 1\n",
            "man/tool-sub.1": ".TH SUB 1\n",
            "man/toolrc.5": ".TH RC 5\n",
        },
        spec=('man = ["man/*.1", "man/*.5"]',),
    )
    env.pmg("install", "tool")
    man = env.data / "man"
    assert all(
        path.is_file()
        for path in (
            man / "man1" / "tool.1",
            man / "man1" / "tool-sub.1",
            man / "man5" / "toolrc.5",
        )
    )


def test_man_page_glob_without_match(env: Env) -> None:
    env.add_package("tool", spec=('man = ["man/*.1"]',))
    assert "has no man/*.1" in env.pmg("install", "tool", ok=False).stderr
    assert env.installed() == {}


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
    assert not (env.packages / "tool-root@v1.0").exists()
    assert list(env.bin.iterdir()) == []


def test_extra_dirs_are_owned(env: Env) -> None:
    env.add_package(
        "tool",
        spec=('dirs = { cache = "{{ data }}/tool-cache-{{ arch }}" }',),
        post_install='echo state > "$PREFIX/dirs/cache/state"',
    )
    env.pmg("install", "tool")
    cache = env.data / f"tool-cache-{platform.machine()}"
    assert (cache / "state").read_text() == "state\n"
    # the package dir stays absent, as nothing was put into it
    assert not (env.packages / "tool@v1.0").exists()
    env.pmg("uninstall", "tool")
    assert not cache.exists()


def test_install_refuses_foreign_package_dir(env: Env) -> None:
    env.add_package("tool", spec=("content = true",))
    (env.packages / "tool@v1.0").mkdir(parents=True)
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
    env.add_package("tool", post_install="echo build error >&2 && exit 3")
    stderr = env.pmg("install", "tool", ok=False).stderr
    # the output of a failing command is shown
    assert "exit code 3" in stderr
    assert "build error" in stderr
    assert not (env.bin / "tool").exists()
    assert env.installed() == {}
    assert list((env.pmg_home / "tmp").iterdir()) == []


def test_failed_test_leaves_nothing(env: Env) -> None:
    env.add_package("tool", test="{{ cmd }} && echo test error >&2 && exit 4")
    stderr = env.pmg("install", "tool", ok=False).stderr
    assert "test of tool failed with exit code 4" in stderr
    assert "test error" in stderr
    assert env.installed() == {}
    assert not list(env.bin.glob("*"))
    assert not list((env.pmg_home / "bin").glob("*"))
    assert list((env.pmg_home / "tmp").iterdir()) == []


def test_test_runs_the_new_version(env: Env) -> None:
    # neither the active version nor a copy outside pmg answer {{ cmd }} and {{ cmds }}
    env.add_system_command("tool", "tool 3.1")
    for version in ("1.0", "2.0"):
        env.add_package(
            "tool",
            version=version,
            post_install='cp "$PREFIX/bin/tool" "$PREFIX/bin/tool-copy"',
            test=(
                'test "$({{ cmd }})" = "tool {{ version }}" && '
                'test "$({{ cmds["tool-copy"] }})" = "tool {{ version }}"'
            ),
        )
    env.pmg("install", "--no-external", "tool@v1.0")
    env.pmg("install", "--no-external", "tool@v2.0")
    assert env.installed() == {"tool@v1.0": "explicit active", "tool@v2.0": "explicit"}


def test_test_of_commands_in_paths(env: Env) -> None:
    # only executables count, and entries that do not exist are skipped
    env.add_package(
        "tool",
        bin_entry=False,
        files={"bin/notes.txt": "not a command"},
        spec=("content = true", 'paths = ["{{ dir }}/bin", "{{ dir }}/missing"]'),
        test='test "$({{ cmd }})" = "tool 1.0" && test -z "{{ cmds.get("notes.txt", "") }}"',
    )
    env.pmg("install", "tool")
    assert env.installed() == {"tool@v1.0": "explicit active"}


def test_failed_move_removes_files_moved_before(env: Env) -> None:
    # bin/a-copy moves first, then bin/sub/b fails, as a foreign file named sub is in the way
    env.add_package(
        "tool",
        post_install="cp bin/tool bin/a-copy && mkdir bin/sub && cp bin/tool bin/sub/b",
    )
    commands = env.pmg_home / "bin"
    commands.mkdir(parents=True)
    (commands / "sub").write_text("mine")
    env.pmg("install", "tool", ok=False)
    assert sorted(path.name for path in commands.iterdir()) == ["sub"]
    assert not env.bin.exists() or not any(env.bin.iterdir())
    assert env.installed() == {}


def test_failed_record_write_removes_installed_files(env: Env) -> None:
    env.add_package("tool")
    env.pmg_home.mkdir(parents=True)
    # a file where the dir of the records belongs
    (env.pmg_home / "installed").write_text("")
    env.pmg("install", "tool", ok=False)
    assert not (env.bin / "tool").exists()


def test_post_install_builds_from_content_with_spec_files(env: Env) -> None:
    env.add_package(
        "tool",
        files={"src/tool.in": "#!/bin/sh\necho built\n"},
        bin_entry=False,
        post_install=(
            'cat "$CONTENT/src/tool.in" "{{ spec_dir }}/tool.extra" > "$PREFIX/bin/tool" && '
            'chmod +x "$PREFIX/bin/tool"'
        ),
    )
    (env.specs / "tool.extra").write_text("echo from spec dir\n")
    env.pmg("install", "tool")
    assert env.run_bin("tool") == "built\nfrom spec dir"


def test_post_install_files_are_installed_and_tracked(env: Env) -> None:
    env.add_package(
        "tool", post_install='echo warning >&2 && cp "$PREFIX/bin/tool" "$PREFIX/bin/tool-copy"'
    )
    # the output of a succeeding command is not
    assert "warning" not in env.pmg("install", "tool").stderr
    assert env.run_bin("tool-copy") == "tool 1.0"
    env.pmg("uninstall", "tool")
    assert list(env.bin.iterdir()) == []


def test_dependency_cycle(env: Env) -> None:
    env.add_package("a", deps=("b",))
    env.add_package("b", deps=("a",))
    assert "dependency cycle" in env.pmg("install", "a", ok=False).stderr
    assert env.installed() == {}


STATIC_TOOL_SPEC = """{fields}
test = "{{{{ cmd }}}}"
[external]
[release]
type = "static"
tag = "v1.0"
[download]
{download}
[assets]
"""


def test_command_download(env: Env) -> None:
    # the command writes into the package dir, which is in the staging dir during the install
    command = (
        'test "$TOOL_HOME" = "$PREFIX/dir" && mkdir "$TOOL_HOME/bin" && '
        'printf "#!/bin/sh\\necho from command\\n" > "$TOOL_HOME/bin/tool" && '
        'chmod +x "$TOOL_HOME/bin/tool"'
    )
    env.write_spec(
        "tool",
        STATIC_TOOL_SPEC.format(
            fields='env = { TOOL_HOME = "{{ dir }}" }\nlinks = { tool = "{{ dir }}/bin/tool" }',
            download=f"type = \"command\"\ncmd = '{command}'",
        ),
    )
    env.pmg("install", "tool")
    assert env.run_bin("tool") == "from command"
    assert (env.packages / "tool@v1.0" / "bin" / "tool").exists()


def test_command_download_gets_the_asset(env: Env) -> None:
    command = (
        'printf "#!/bin/sh\\necho {{ asset }}\\n" > "$PREFIX/bin/tool" && '
        'chmod +x "$PREFIX/bin/tool"'
    )
    assets = "\n".join(
        f'{platform} = "tool-{{{{ version }}}}-{platform}"' for platform in PLATFORMS
    )
    env.write_spec(
        "tool",
        STATIC_TOOL_SPEC.format(fields="", download=f"type = \"command\"\ncmd = '{command}'")
        + assets,
    )
    env.pmg("install", "tool")
    assert env.run_bin("tool").startswith("tool-1.0-")


def test_env_and_paths(env: Env) -> None:
    # external versions are never active, so they print nothing
    env.add_system_command("other", "other 3.1")
    env.add_package("other", spec=('env = { OTHER = "x" }',))
    env.pmg("install", "other")
    assert env.pmg("env").stdout == ""
    env.add_package(
        "tool",
        bin_entry=False,
        spec=("content = true", 'env = { TOOL_HOME = "{{ dir }}" }', 'paths = ["{{ dir }}/bin"]'),
        post_install='test "$TOOL_HOME" = "$PREFIX/dir"',
    )
    env.pmg("install", "tool")
    package_dir = env.packages / "tool@v1.0"
    output = env.pmg("env").stdout
    assert output == f'export TOOL_HOME={package_dir}\nexport PATH={package_dir}/bin:"$PATH"\n'
    # shells source this file instead of running pmg
    assert (env.pmg_home / "env.sh").read_text() == output
    # the printed code sets up a shell that finds the command
    shell = subprocess.run(  # noqa: S603
        ["/bin/sh", "-c", f'{output}tool && echo "$TOOL_HOME"'],
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=True,
    )
    assert shell.stdout == f"tool 1.0\n{package_dir}\n"


def test_platform_deps(env: Env) -> None:
    host = detect_platform(None)
    other = next(platform for platform in PLATFORMS if platform != host)
    env.add_package("lib", spec=("content = true",))
    env.add_package("unused")
    env.add_package(
        "app",
        spec=(f'platform_deps = {{ {host} = ["lib"], {other} = ["unused"] }}',),
        # the dependency of the other platform is there, but empty
        post_install='echo "{{ deps.lib.version }} [{{ deps.unused.dir }}]" > "$PREFIX/dir/info"',
    )
    env.pmg("install", "app")
    assert set(env.installed()) == {"app@v1.0", "lib@v1.0"}
    assert (env.packages / "app@v1.0" / "info").read_text() == "v1.0 []\n"


def test_markers_in_deps(env: Env) -> None:
    env.add_package("lib")
    env.add_package("other")
    deps = (f"lib; sys_platform == '{sys.platform}'", "other; sys_platform == 'none'")
    env.add_package("app", deps=deps)
    env.pmg("install", "app")
    assert set(env.installed()) == {"app@v1.0", "lib@v1.0"}


@pytest.mark.parametrize("external", [False, True])
def test_dependency_template_variables(env: Env, external: bool) -> None:
    if external:
        env.add_system_command("lib", "lib 3.1")
    env.add_package("lib", spec=("content = true",))
    env.add_package(
        "app",
        deps=("lib",),
        post_install='echo "{{ deps.lib.dir }} {{ deps.lib.version }}" > "$PREFIX/dir/lib-info"',
    )
    env.pmg("install", "app")
    info = (env.packages / "app@v1.0" / "lib-info").read_text()
    # an external dependency has no package dir
    assert info == (" 3.1\n" if external else f"{env.packages / 'lib@v1.0'} v1.0\n")


def test_env_with_dependencies(env: Env) -> None:
    # like the library path of rustup, which only musl hosts get from musl-libs; what renders
    # empty is not set, so it cannot clear a variable of the shell
    env.add_package("lib", spec=("content = true",))
    env.add_package(
        "app",
        deps=("lib",),
        spec=(
            'env = { APP_HOME = "{{ deps.lib.dir }}", APP_EMPTY = "" }',
            'prepend = { APP_LIBS = ["{{ deps.lib.dir }}/lib", ""], APP_NONE = [""] }',
            """uninstall = 'test "$APP_LIBS" = "{{ deps.lib.dir }}/lib"'""",
        ),
    )
    env.pmg("install", "app")
    lib = env.packages / "lib@v1.0"
    output = env.pmg("env").stdout
    assert (
        output == f'export APP_HOME={lib}\nexport APP_LIBS={lib}/lib"${{APP_LIBS:+:$APP_LIBS}}"\n'
    )
    # the entries go before a value of the shell
    shell = subprocess.run(  # noqa: S603
        ["/bin/sh", "-c", f'{output}echo "$APP_LIBS"'],
        env={"APP_LIBS": "/old"},
        capture_output=True,
        text=True,
        check=True,
    )
    assert shell.stdout == f"{lib}/lib:/old\n"
    env.pmg("uninstall", "app")


def test_upgrade_replaces_active_version(env: Env) -> None:
    env.add_package("tool", version="1.0")
    env.pmg("install", "tool")
    env.add_package("tool", version="2.0")
    env.pmg("upgrade")
    assert env.installed() == {"tool@v2.0": "explicit active"}
    assert env.run_bin("tool") == "tool 2.0"
    assert "tool v2.0 is up to date" in env.pmg("upgrade", "tool").stderr


def test_upgrade_keeps_version_a_dependent_needs(env: Env) -> None:
    env.add_package("tool", version="1.0")
    env.add_package("app", deps=("tool<2",))
    env.pmg("install", "app")
    env.add_package("tool", version="2.0")
    assert "kept tool@v1.0: app depends on tool<2" in env.pmg("upgrade", "tool").stderr
    assert env.installed() == {
        "app@v1.0": "explicit active",
        "tool@v1.0": "dependency",
        "tool@v2.0": "dependency active",
    }


def test_upgrade_in_place_and_external(env: Env) -> None:
    env.add_system_command("other", "other 3.1")
    env.add_package("other")
    spec = ("content = true", """upgrade = 'echo upgraded >> "{{ dir }}/marker"'""")
    env.add_package("tool", spec=spec)
    env.pmg("install", "tool", "other")
    marker = env.packages / "tool@v1.0" / "marker"
    # the latest release is installed, so the upgrade command does not run
    assert "tool v1.0 is up to date" in env.pmg("upgrade").stderr
    assert not marker.exists()
    env.add_package("tool", version="2.0", spec=spec)
    assert "upgraded tool in place to v2.0" in env.pmg("upgrade").stderr
    assert marker.read_text() == "upgraded\n"
    # the record keeps the release of the upgrade, so it runs only once
    assert "tool v2.0 is up to date" in env.pmg("upgrade", "tool").stderr
    assert marker.read_text() == "upgraded\n"
    # external versions are left to their package manager
    assert env.installed() == {
        "other@external": "explicit external 3.1",
        "tool@v1.0": "explicit active upgraded to v2.0",
    }


def write_registry(env: Env, version: str, name: str = "tool") -> None:
    spec = STATIC_TOOL_SPEC.format(
        fields=f"""post_install = 'cp "{{{{ spec_dir }}}}/{name}/script" "$PREFIX/bin/{name}"'""",
        download='type = "url"\nurl = "' + env.base_url + '/{{ asset }}"',
    ).replace('tag = "v1.0"', f'tag = "v{version}"')
    spec += "".join(f'{platform} = "script-{{{{ version }}}}"\n' for platform in PLATFORMS)
    (env.assets / f"script-{version}").write_text("")
    script = f"#!/bin/sh\necho registry {version}\n".encode()
    entries = {
        f"pmg-specs-main/specs/{name}.toml": (spec.encode(), 0o644),
        # files a spec needs go into a dir named like it
        f"pmg-specs-main/specs/{name}/script": (script, 0o755),
    }
    (env.assets / "registry.tar.gz").write_bytes(gzip.compress(tar_bytes(entries)))


def test_registry(env: Env) -> None:
    write_registry(env, "1.0")
    # the first install downloads the registry
    env.pmg("install", "tool", specs_dir=False)
    assert env.run_bin("tool") == "registry 1.0"
    write_registry(env, "2.0")
    assert "updated the specs" in env.pmg("update").stderr
    env.pmg("upgrade", specs_dir=False)
    assert env.run_bin("tool") == "registry 2.0"
    # a spec added to the registry since its download is fetched without pmg update
    write_registry(env, "3.0", name="new-tool")
    assert "updated the specs" in env.pmg("install", "new-tool", specs_dir=False).stderr
    assert env.installed()["new-tool@v3.0"] == "explicit active"


@pytest.mark.skipif(shutil.which("zsh") is None, reason="no zsh")
@pytest.mark.parametrize(("command", "completes_files"), [("validate", True), ("install", False)])
def test_zsh_completion_completes_files_only_for_validate(
    tmp_path: Path, command: str, completes_files: bool
) -> None:
    # pmg answers _files for the paths of validate, as without a match
    fake_pmg = tmp_path / "pmg"
    fake_pmg.write_text("#!/bin/sh\necho _files\n")
    fake_pmg.chmod(0o755)
    # the completion as the body of _pmg, with an eval that shows what it would run
    script = f"""eval() {{ print -r -- "eval $*"; }}
_pmg() {{
{pmg.core.ZSH_COMPLETION}}}
words=(pmg {command} x)
CURRENT=3
_pmg"""
    result = subprocess.run(  # noqa: S603
        ["zsh", "-fc", script],  # noqa: S607
        env={**os.environ, "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        check=True,
    )
    assert (result.stdout == "eval _files\n") == completes_files


def test_completions(env: Env) -> None:
    env.add_package("tool", version="1.0")
    env.add_package("other")
    env.pmg("install", "tool@v1.0")

    def complete(line: str) -> str:
        env_vars = clean_environ(env.home) | {
            "PMG_HOME": str(env.pmg_home),
            "PMG_SPECS_DIR": str(env.specs),
            "_PMG_COMPLETE": "complete_zsh",
            "_TYPER_COMPLETE_ARGS": line,
            "PMG_REGISTRY_URL": f"{env.base_url}/registry.tar.gz",
        }
        return subprocess.check_output([sys.executable, "-m", "pmg"], env=env_vars, text=True)

    # pmg writes its completion next to those of its packages, so zsh finds it there
    completion = env.data / "zsh" / "site-functions" / "_pmg"
    assert completion.read_text() == env.pmg("completion").stdout
    # install completes the specs, uninstall the installed packages and their versions
    assert '"other"' in complete("pmg install ot")
    # a missing registry is downloaded for it
    write_registry(env, "1.0")
    shutil.rmtree(env.pmg_home / "registry", ignore_errors=True)
    registry_completion = complete("pmg install to")
    assert '"tool"' in registry_completion
    assert "updated the specs" not in registry_completion
    assert complete("pmg install to") == registry_completion
    assert '"tool"' in complete("pmg uninstall t")
    assert '"tool@v1.0"' in complete("pmg uninstall t")
    assert "other" not in complete("pmg uninstall ")


def test_show_prints_the_spec(env: Env) -> None:
    env.add_package("tool")
    assert env.pmg("show", "tool").stdout == (env.specs / "tool.toml").read_text()


def test_external_names(env: Env) -> None:
    env.add_package("tool", external=('brew = "tool-brew"', 'apt = "tool-apt"'))
    assert env.pmg("external", "tool").stdout == "brew tool-brew\napt tool-apt\n"


def test_spec_commands_get_certificates_without_system_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import certifi

    monkeypatch.setenv("PMG_HOME", str(tmp_path))
    monkeypatch.setattr(pmg.core, "system_certificates", lambda: False)
    output = pmg.core.run_shell("tool", "post_install", 'echo "$CURL_CA_BUNDLE $SSL_CERT_FILE"')
    assert output.split() == [certifi.where(), certifi.where()]


def test_progress_on_a_terminal(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PMG_HOME", str(env.pmg_home))
    monkeypatch.setattr(pmg.core, "interactive", lambda: True)
    (env.assets / "archive.tar.gz").write_bytes(b"x" * 100_000)
    dest = env.root / "archive.tar.gz"
    pmg.core.run_async(pmg.core.download_file(f"{env.base_url}/archive.tar.gz", dest))
    assert dest.stat().st_size == 100_000
    # the bar of the download, which it clears once done
    assert "archive.tar.gz" in capsys.readouterr().err
    # the spinner of a spec command leaves its output alone
    assert pmg.core.run_shell("tool", "post_install", "echo built") == "built\n"


def test_errors_show_no_traceback(env: Env) -> None:
    # the test server has no registry, so downloading it fails with a 404
    stderr = env.pmg("install", "z*", ok=False).stderr
    assert "Traceback" not in stderr
    assert stderr.startswith("error: Client error '404")
    assert len(stderr.splitlines()) == 1


@pytest.mark.parametrize(
    ("error", "code", "messages"),
    [(KeyboardInterrupt(), 130, []), (OSError(), 1, ["error: OSError"])],
)
def test_interrupts_and_errors_without_a_message_exit_quietly(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    error: BaseException,
    code: int,
    messages: list[str],
) -> None:
    import pmg.__main__
    import pmg.cli

    def raise_error() -> None:
        raise error

    monkeypatch.setattr(pmg.cli, "print_version", raise_error)
    monkeypatch.setattr(sys, "argv", ["pmg", "version"])
    with pytest.raises(SystemExit) as exit_info:
        pmg.__main__.main()
    assert exit_info.value.code == code
    assert caplog.messages == messages


def test_failed_package_skips_only_its_dependents(env: Env) -> None:
    env.add_package("fine")
    env.add_package("broken", post_install="exit 1")
    env.add_package("dependent", deps=("broken",))
    stderr = env.pmg("install", "fine", "dependent", ok=False).stderr
    assert "error: skipped dependent, as broken failed" in stderr
    assert "not installed: broken, dependent" in stderr
    assert set(env.installed()) == {"fine@v1.0"}


def test_failed_download_fails_only_its_package(env: Env) -> None:
    env.add_package("fine")
    env.add_package("gone")
    (env.assets / "gone-1.0.tar.gz").unlink()
    stderr = env.pmg("install", "fine", "gone", ok=False).stderr
    assert "error: gone: Client error '404" in stderr
    assert set(env.installed()) == {"fine@v1.0"}


def test_dependents_wait_for_dependencies(env: Env) -> None:
    # the release command of app runs the command of tool, which installs at the same time
    env.add_package("tool", post_install="sleep 0.5")
    release = 'type = "command"\ncmd = "tool | cut -d\' \' -f1 >/dev/null && echo v1.0"'
    env.add_package("app", deps=("tool",), release=release, test="{{ cmd }} && tool")
    env.pmg("install", "app")
    assert set(env.installed()) == {"app@v1.0", "tool@v1.0"}


def test_release_command_runs_before_dependencies_are_done(env: Env) -> None:
    # the release command needs no dependency, so the release, and with it the download, need
    # not wait for the build of one
    done, order = env.root / "dep-done", env.root / "order"
    env.add_package("dep", post_install=f"sleep 1 && touch {done}")
    release = f"""type = "command"
cmd = '''
if [ -e {done} ]; then echo late > {order}; else echo early > {order}; fi
echo v1.0
'''"""
    env.add_package("app", deps=("dep",), release=release)
    env.pmg("install", "app")
    assert order.read_text() == "early\n"
    assert set(env.installed()) == {"app@v1.0", "dep@v1.0"}


def test_failed_upgrade_does_not_stop_the_others(env: Env) -> None:
    env.add_package("tool")
    env.add_package("app", deps=("tool",))
    env.pmg("install", "app")
    env.add_package("tool", release='type = "command"\ncmd = "exit 1"')
    env.add_package("app", deps=("tool",), version="2.0")
    stderr = env.pmg("upgrade", ok=False).stderr
    assert "release command of tool failed" in stderr
    assert "not upgraded: tool" in stderr
    assert set(env.installed()) == {"app@v2.0", "tool@v1.0"}


def test_upgrade_refuses_dependency_cycle(env: Env) -> None:
    env.add_package("one")
    env.add_package("two")
    env.pmg("install", "one", "two")
    # records of specs whose dependencies changed since
    for name, dep in [("one", "two"), ("two", "one")]:
        path = env.pmg_home / "installed" / f"{name}@v1.0.json"
        record = json.loads(path.read_text())
        path.write_text(json.dumps(record | {"deps": [dep]}))
    assert "dependency cycle" in env.pmg("upgrade", ok=False).stderr


def read_terminal(fd: int) -> bytes:
    """Reads the output of a terminal, b"" at its end, which Linux reports as an error."""
    with contextlib.suppress(OSError):
        return os.read(fd, 65536)
    return b""  # pragma: no cover


def test_progress_of_packages_on_a_terminal(env: Env) -> None:
    import fcntl
    import pty
    import struct
    import termios

    import pyte

    env.add_package("dep", post_install="sleep 1.5")
    env.add_package("app", deps=("dep",))
    env.add_package("other", release='type = "command"\ncmd = "echo v1.0"')
    pmg_env = clean_environ(env.home) | {
        "PMG_HOME": str(env.pmg_home),
        "PMG_SPECS_DIR": str(env.specs),
    }
    columns, lines = 100, 20
    controller, terminal = pty.openpty()
    fcntl.ioctl(terminal, termios.TIOCSWINSZ, struct.pack("HHHH", lines, columns, 0, 0))
    with subprocess.Popen(
        [sys.executable, "-m", "pmg", "install", "app", "other"],
        env=pmg_env,
        stdin=terminal,
        stdout=terminal,
        stderr=terminal,
    ) as process:
        os.close(terminal)
        output = b""
        while data := read_terminal(controller):
            output += data
    os.close(controller)
    assert process.returncode == 0
    screen = pyte.Screen(columns, lines)
    pyte.ByteStream(screen).feed(output)
    shown = [line.rstrip() for line in screen.display if line.strip()]
    # the board showed the steps, then cleared itself and left the log lines
    assert b"installing" in output
    assert b"post_install of dep" in output
    assert sorted(shown) == ["installed app v1.0", "installed dep v1.0", "installed other v1.0"]
    assert screen.cursor.x == 0
    assert shown.index("installed dep v1.0") < shown.index("installed app v1.0")


def test_globs_skip_packages_not_for_the_host(env: Env) -> None:
    write_registry(env, "1.0")
    other = next(platform for platform in PLATFORMS if platform != detect_platform(None))
    env.add_package("zx", spec=(f'platforms = ["{other}"]',))
    env.add_package("zq")
    result = env.pmg("install", "z*")
    assert "skipped zx, not for this host" in result.stderr
    assert set(env.installed()) == {"zq@v1.0"}
    # requested by name, it is an error
    assert f"zx is only for {other}" in env.pmg("install", "zx", ok=False).stderr


def test_globs(env: Env) -> None:
    # names no host has, so none is found as external; the registry has "tool", which a missing
    # registry downloads for a glob
    write_registry(env, "1.0")
    for name in ("zx", "zx-extra", "zq"):
        env.add_package(name)
    env.pmg("install", "ZX*", "zq")
    assert set(env.installed()) == {"zx@v1.0", "zx-extra@v1.0", "zq@v1.0"}
    env.pmg("install", "t??l")
    assert "tool@v1.0" in env.installed()
    # globs match installed packages for upgrade and uninstall
    env.pmg("upgrade", "zx*")
    env.pmg("uninstall", "zx-*", "t*")
    assert set(env.installed()) == {"zx@v1.0", "zq@v1.0"}
    # a plain name matches only itself
    assert "no spec for z " in env.pmg("install", "z", ok=False).stderr
    assert "no package matches x*" in env.pmg("uninstall", "x*", ok=False).stderr
    assert "no package matches x*" in env.pmg("upgrade", "x*", ok=False).stderr
    assert "a glob cannot have a tag: zx*@v1.0" in env.pmg("install", "zx*@v1.0", ok=False).stderr


def test_version(env: Env) -> None:
    from pmg import __version__

    assert env.pmg("version").stdout == f"{__version__}\n"
    assert not __version__.startswith("v")


def test_search(env: Env) -> None:
    # the registry has "tool", which a missing registry downloads for the search
    write_registry(env, "1.0")
    for name in ("zq", "musl-libs", "Musl"):
        env.add_package(name)
    env.pmg("install", "zq")
    assert env.pmg("search").stdout == "Musl\nmusl-libs\ntool\nzq         v1.0 (active)\n"
    # a part of the name, ignoring case
    assert env.pmg("search", "MUSL").stdout == "Musl\nmusl-libs\n"
    # a glob matches the whole name
    assert env.pmg("search", "*-libs").stdout == "musl-libs\n"
    assert env.pmg("search", "z?").stdout == "zq  v1.0 (active)\n"
    assert env.pmg("search", "Q").stdout == "zq  v1.0 (active)\n"
    assert env.pmg("search", "s?").stdout == ""


def test_schema_file_is_current(env: Env) -> None:
    # spec repos and editors use the committed schema.json, which must follow the models
    committed = json.loads((Path(__file__).parents[1] / "schema.json").read_text())
    assert json.loads(env.pmg("schema").stdout) == committed


def test_validate(env: Env) -> None:
    (env.root / "bad.toml").write_text('binn = { tool = "tool" }\n')
    # the test is required
    no_test = (FIXTURE_SPECS / "bat.toml").read_text().replace('test = "{{ cmd }} --version"\n', "")
    (env.root / "no-test.toml").write_text(no_test)
    stderr = env.pmg("validate", str(env.root / "no-test.toml"), ok=False).stderr
    assert "missing required field `test`" in stderr
    # and must use the version under test, not whatever PATH finds
    (env.root / "path-test.toml").write_text(
        no_test.replace("[external]", 'test = "bat --version"\n[external]')
    )
    stderr = env.pmg("validate", str(env.root / "path-test.toml"), ok=False).stderr
    assert "the test must use the installed version" in stderr
    stderr = env.pmg(
        "validate", str(FIXTURE_SPECS / "bat.toml"), str(env.root / "bad.toml"), ok=False
    ).stderr
    assert "bad.toml" in stderr
    assert "binn" in stderr
    assert "1 specs are valid" in env.pmg("validate", str(FIXTURE_SPECS / "bat.toml")).stderr

    # a dir checks its specs; a missing file, broken TOML, and a dir without specs only count as
    # invalid, the other paths are still checked
    count = len(list(FIXTURE_SPECS.glob("*.toml")))
    assert f"{count} specs are valid" in env.pmg("validate", str(FIXTURE_SPECS)).stderr
    (env.root / "broken.toml").write_text("x = [\n")
    (env.root / "empty").mkdir()
    paths = ["missing.toml", "broken.toml", "empty", "bad.toml"]
    stderr = env.pmg("validate", *(str(env.root / p) for p in paths), ok=False).stderr
    for path in paths:
        assert f"{env.root / path}: " in stderr
    assert "Traceback" not in stderr


def test_content_subdir_with_keep_and_remove(env: Env) -> None:
    env.add_package(
        "tool",
        files={
            "sub/root/lib/a.so": "a",
            "sub/root/lib/b.a": "b",
            "sub/root/lib/deep/c.so": "c",
            "sub/root/doc/readme": "doc",
        },
        spec=('content = "sub/*"', 'keep = ["lib/*"]', 'remove = ["lib/*.a"]'),
        bin_entry=False,
        test='test -f "{{ dir }}/lib/a.so"',
    )
    env.pmg("install", "tool")
    root = env.packages / "tool@v1.0"
    # the kept dir lib/deep keeps its content, the emptied doc dir goes
    assert sorted(str(path.relative_to(root)) for path in root.rglob("*")) == [
        "lib",
        "lib/a.so",
        "lib/deep",
        "lib/deep/c.so",
    ]


def test_platforms(env: Env) -> None:
    others = [platform for platform in PLATFORMS if platform != detect_platform(None)]
    env.add_package("lib", spec=(f"platforms = {json.dumps(others)}",))
    env.add_package("app", deps=("lib",))
    # as a dependency, a package for other platforms is skipped
    env.pmg("install", "app")
    assert env.installed() == {"app@v1.0": "explicit active"}
    assert "lib is only for" in env.pmg("install", "lib", ok=False).stderr


def test_external_command(env: Env) -> None:
    env.add_system_command("tool", "tool 3.1")
    env.add_package("tool")
    env.add_package("app", deps=("tool",))
    env.add_package("old", deps=("tool<3",))
    env.pmg("install", "app")
    assert env.installed() == {
        "app@v1.0": "explicit active",
        "tool@external": "dependency external 3.1",
    }
    assert not (env.bin / "tool").exists()
    # the external 3.1 is too new for old, so pmg installs its own tool
    env.pmg("install", "old")
    assert env.run_bin("tool") == "tool 1.0"
    env.pmg("uninstall", "app", "old")
    env.pmg("autoremove")
    assert env.installed() == {}
    assert (env.system / "tool").exists()


def test_external_version_that_is_gone_or_changed(env: Env) -> None:
    env.add_system_command("tool", "tool 3.1")
    env.add_package("tool")
    env.add_package("app", deps=("tool",))
    env.pmg("install", "tool")
    env.add_system_command("tool", "tool 3.2")
    env.pmg("install", "app")
    assert env.installed()["tool@external"] == "explicit external 3.2"
    # removed by its package manager, so pmg installs its own instead of trusting the record
    (env.system / "tool").unlink()
    assert "tool is no longer found outside pmg" in env.pmg("install", "tool").stderr
    assert env.installed() == {"app@v1.0": "explicit active", "tool@v1.0": "explicit active"}
    assert env.run_bin("tool") == "tool 1.0"


def test_external_package_found_by_name(env: Env) -> None:
    # without commands in the spec, e.g. when post_install builds them, the name is the command
    env.add_system_command("tool", "tool 2.0")
    env.add_package("tool", bin_entry=False)
    env.pmg("install", "tool")
    assert env.installed() == {"tool@external": "explicit external 2.0"}


def test_no_external_installs_the_requested_package_itself(env: Env) -> None:
    # app and base are on the system; only app is requested, so base stays external
    env.add_system_command("app", "app 2.0")
    env.add_system_command("base", "base 3.0")
    env.add_package("app", deps=("lib",))
    env.add_package("lib", deps=("base",))
    env.add_package("base")
    env.pmg("install", "--no-external", "app")
    assert env.installed() == {
        "app@v1.0": "explicit active",
        "lib@v1.0": "dependency active",
        "base@external": "dependency external 3.0",
    }
    assert env.run_bin("app") == "app 1.0"


def test_external_package_pulls_in_no_dependencies(env: Env) -> None:
    env.add_system_command("app", "app 2.0")
    env.add_package("lib")
    env.add_package("app", deps=("lib",))
    env.pmg("install", "app")
    assert env.installed() == {"app@external": "explicit external 2.0"}


@pytest.mark.parametrize(
    ("check", "external"),
    [
        ('{ files = ["SYSTEM/marker"] }', True),
        (f'{{ libs = ["{LIBC}"] }}', True),
        ('{ libs = ["libmissing.so.1"] }', False),
    ],
)
def test_external_files_and_libs(env: Env, check: str, external: bool) -> None:
    env.add_system_command("marker", "")
    env.add_package("tool", check=check.replace("SYSTEM", str(env.system)))
    env.pmg("install", "tool")
    expected = {"tool@external": "explicit external unknown"}
    assert env.installed() == (expected if external else {"tool@v1.0": "explicit active"})


def test_alpine_packages(env: Env) -> None:
    repo = alpine_repo(env.assets / "alpine")
    repo.mkdir(parents=True)
    index = b"P:tool\nV:1.0-r0\n\nP:toollib\nV:2.0-r1\n\n"
    apkindex = tar_bytes({"APKINDEX": (index, 0o644)})
    (repo / "APKINDEX.tar.gz").write_bytes(gzip.compress(apkindex))
    tool = apk_bytes({"usr/bin/tool": (b"#!/bin/sh\necho alpine tool\n", 0o755)})
    (repo / "tool-1.0-r0.apk").write_bytes(tool)
    toollib = apk_bytes({"usr/lib/libtool.so.1": (b"lib", 0o644), "usr/share/doc": (b"doc", 0o644)})
    (repo / "toollib-2.0-r1.apk").write_bytes(toollib)
    env.write_spec(
        "tool",
        """content = true
test = "{{ cmd }}"
keep = ["usr/bin/*", "usr/lib/*"]
links = { tool = "{{ dir }}/usr/bin/tool" }
[external]
[release]
type = "apk"
package = "tool"
[download]
type = "apk"
packages = ["tool", "toollib"]
[assets]
""",
    )
    env.pmg("install", "tool")
    assert env.installed() == {"tool@1.0-r0": "explicit active"}
    assert env.run_bin("tool") == "alpine tool"
    # the index comes from the cache now
    (repo / "APKINDEX.tar.gz").unlink()
    env.pmg("uninstall", "tool")
    env.pmg("install", "tool")
    root = env.packages / "tool@1.0-r0"
    assert sorted(str(path.relative_to(root)) for path in root.rglob("*")) == [
        "usr",
        "usr/bin",
        "usr/bin/tool",
        "usr/lib",
        "usr/lib/libtool.so.1",
    ]


def test_conda_package(env: Env) -> None:
    sysroot = "x86_64-conda-linux-gnu/sysroot"
    payload = tar_bytes(
        {
            f"{sysroot}/lib64/libc.so.6": (b"libc", 0o644),
            f"{sysroot}/lib64/libc.a": (b"static", 0o644),
            f"{sysroot}/usr/include/stdio.h": (b"header", 0o644),
        }
    )
    conda = zip_bytes(
        {"pkg-sysroot.tar.zst": zstandard.compress(payload), "info-sysroot.tar.zst": b""}
    )
    (env.assets / "conda" / "cf" / "linux-64").mkdir(parents=True)
    (env.assets / "conda" / "cf" / "linux-64" / "sysroot-2.28-0.conda").write_bytes(conda)
    files = [
        # placeholder builds, other formats, and older versions are ignored
        {"version": "9999", "basename": "linux-64/sysroot-9999-0.conda", "upload_time": "3"},
        {"version": "2.28", "basename": "linux-64/sysroot-2.28-0.tar.bz2", "upload_time": "2"},
        {"version": "2.17", "basename": "linux-64/sysroot-2.17-0.conda", "upload_time": "2"},
        {
            "version": "2.28",
            "basename": "linux-64/sysroot-2.28-0.conda",
            "upload_time": "1",
            "sha256": hashlib.sha256(conda).hexdigest(),
        },
    ]
    api = env.assets / "conda-api" / "package" / "cf" / "sysroot"
    api.mkdir(parents=True)
    (api / "files").write_text(json.dumps(files))
    assets = "\n".join(f'{platform} = "sysroot"' for platform in PLATFORMS)
    env.write_spec(
        "sysroot",
        f"""content = "*-conda-linux-gnu/sysroot"
remove = ["lib64/*.a", "usr/include"]
post_install = 'cd "$PREFIX/dir" && ln -s lib64/libc.so.6 loader'
test = 'test -f "{{{{ dir }}}}/loader"'
[external]
[release]
type = "conda"
channel = "cf"
[download]
type = "conda"
channel = "cf"
[assets]
{assets}
""",
    )
    env.pmg("install", "sysroot")
    assert env.installed() == {"sysroot@2.28": "explicit active"}
    # the file list comes from the cache now
    (api / "files").unlink()
    env.pmg("uninstall", "sysroot")
    env.pmg("install", "sysroot")
    root = env.packages / "sysroot@2.28"
    assert sorted(str(path.relative_to(root)) for path in root.rglob("*")) == [
        "lib64",
        "lib64/libc.so.6",
        "loader",
        "usr",
    ]
    assert (root / "loader").read_text() == "libc"


def registry_spec(host_platforms: tuple[str, ...]) -> pytest.MarkDecorator:
    host = detect_platform(None)
    return pytest.mark.skipif(host not in host_platforms, reason=f"not for {host}")


@pytest.mark.skipif(os.getenv("PMG_OFFLINE") == "1", reason="PMG_OFFLINE=1")
@pytest.mark.parametrize(
    "name",
    [
        pytest.param("patchelf", marks=registry_spec(LINUX)),
        pytest.param("musl", marks=registry_spec(("glibc_x64", "glibc_arm64"))),
        "zig",
    ],
)
def test_install_from_registry(tmp_path: Path, name: str) -> None:
    subprocess.check_call(  # noqa: S603
        [sys.executable, "-m", "pmg", "install", name], env=clean_environ(tmp_path)
    )
    listed = subprocess.check_output(
        [sys.executable, "-m", "pmg", "list"], env=clean_environ(tmp_path), text=True
    )
    expected = {
        "patchelf": "bin/patchelf",
        "musl": "share/pmg/packages/musl@*/lib/ld-musl-*.so.1",
        # zig only runs if it finds its lib dir through the symlink
        "zig": "share/pmg/packages/zig@*/lib",
    }[name]
    # a system copy, like patchelf on GitHub's runners, counts as external instead
    assert f"{name}@external " in listed or list((tmp_path / ".local").glob(expected))


@pytest.mark.skipif(os.getenv("PMG_OFFLINE") == "1", reason="PMG_OFFLINE=1")
@pytest.mark.parametrize(
    ("name", "args", "extra_files"),
    [
        ("bat", ["--version"], ["man/man1/bat.1", "zsh/site-functions/_bat"]),
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
