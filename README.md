# pmg

A simple package manager for prebuilt binaries in the home directory. It installs release assets
from GitHub or other URLs into `~/.local/bin`, tracks the installed files and dependencies, and
works without admin rights.

## Prerequisites

- Python 3.10 or newer

## Installation

```bash
uv tool install git+https://github.com/audivir/pmg
```

Or run it without installing, with `uvx --from git+https://github.com/audivir/pmg pmg`.

## Usage

Each package is described by a TOML spec named after the package, for example `bat.toml`:

```toml
min_glibc = "2.18"
bin = { bat = "bat" }
man = ["bat.1"]
completions.bat = { zsh = "autocomplete/bat.zsh", bash = "autocomplete/bat.bash" }
check = {}

[external]
brew = "bat"

[release]
type = "github"
repo = "sharkdp/bat"

[download]
type = "github"

[assets]
glibc_x64 = "bat-{{ tag }}-x86_64-unknown-linux-gnu.tar.gz"
musl_x64 = "bat-{{ tag }}-x86_64-unknown-linux-musl.tar.gz"
macos_arm64 = "bat-{{ tag }}-aarch64-apple-darwin.tar.gz"
```

- Templates can use `{{ tag }}`, `{{ version }}` (the tag without a leading `v`), `{{ asset }}`,
  `{{ arch }}` (as `uname -m` prints it), `{{ data }}` (`$XDG_DATA_HOME`), `{{ bin }}`, `{{ dir }}`,
  and `{{ dirs.<key> }}`.
- `release` and `download` are of the type `github`, `url`, `command` (a command printing the tag,
  or installing into the staging directory), `static` (release only), `apk` (packages of the main Alpine repo, of the host release or else
  latest-stable), or `conda` (the newest `.conda` file of the asset package in a channel).
- glibc hosts older than `min_glibc` get the musl asset. `platforms` limits a package to some
  platforms, other hosts skip it as a dependency. `platform_deps` adds dependencies for the
  platform whose asset is used, e.g. a loader for the musl build on old glibc hosts.
- `bin` maps names in `~/.local/bin` to paths in the archive. A single top-level directory in
  the archive is stripped. `links` adds symlinks instead of copies.
- `man` lists man pages in the archive, the file extension is the section.
- `completions` maps a command to its completion script per shell, either a path in the archive
  or `{ cmd = "..." }`, a command printing it.
- Everything else belongs in the package directory `{{ dir }}`, by default `$XDG_DATA_HOME/<name>`
  (set `dir` to change it). `content = true` makes the unpacked archive the package directory, or
  `content = "<glob>"` its only matching subdirectory. `keep` and `remove` are globs of the files
  kept in and removed from it. `dirs` adds more directories. The package owns these directories as
  a whole.
- `check` detects a copy that pmg did not install: `files` must exist, the dynamic loader must find
  `libs`, and `cmd` must run. Without them, the first command of the package runs with `args`
  (`--version`). The version is the first match of `regex` in the output. Such an external version
  satisfies dependencies and is only recorded, pmg leaves its files alone. `dev_tool = true` marks
  commands that macOS ships as stubs in `/usr/bin` (`cc`, `git`, `make`, `python3`), which only
  count if the developer tools are installed.
- `post_install` runs a shell command in the staging directory `PREFIX`, with the unpacked archive
  in `CONTENT`, e.g. to build from source. `{{ spec_dir }}` is the directory of the spec, for files
  shipped next to it. `uninstall` runs before the files are removed, and `upgrade` updates a
  package in place that updates itself. Spec commands run with `set -euo pipefail` and the bin
  directory in `PATH`. Their output is only shown if they fail.
- `env` sets environment variables for the spec commands and, with `paths` as `PATH` entries, for
  the shell through `pmg env`. During an install, `{{ dir }}` and `{{ dirs.<key> }}` point to the
  staging directory there.
- `deps` entries can have environment markers like `"lib; sys_platform == 'linux'"`. In
  templates, `{{ deps["lib"].dir }}` and `{{ deps["lib"].version }}` are the package directory and
  version of a dependency in use, the directory is empty for an external one.

Packages share this layout:

| Files | Installed to |
|---|---|
| commands | `~/.local/bin` |
| man pages | `$XDG_DATA_HOME/man/man<section>` |
| zsh completions | `$XDG_DATA_HOME/zsh/site-functions/_<command>` |
| bash completions | `$XDG_DATA_HOME/bash-completion/completions/<command>` |
| fish completions | `$XDG_DATA_HOME/fish/vendor_completions.d/<command>.fish` |
| everything else | the package directory |

bash and fish find the completions on their own. zsh needs the directory in `fpath`, and mandoc
(Alpine) needs `MANPATH="$XDG_DATA_HOME/man:"`.

Specs are searched in `$PMG_SPECS_DIR`, then in `$PMG_HOME/specs`, then in the registry. `pmg update`
downloads the registry from [pmg-specs](https://github.com/audivir/pmg-specs) (or
`$PMG_REGISTRY_URL`), the first install does so on its own. `PMG_HOME` defaults to
`$XDG_DATA_HOME/pmg` and also holds the install records. `pmg schema` prints the JSON schema of
specs, which a first line like `#:schema https://raw.githubusercontent.com/audivir/pmg-specs/main/schema.json`
hands to editors and `taplo check`.

```bash
pmg install bat
pmg install bat@v0.25.0
pmg use bat@v0.25.0
pmg list
pmg uninstall bat@v0.25.0
pmg update
pmg upgrade
pmg autoremove
eval "$(pmg env)"
```

- `install` installs the latest release. `name@tag` installs the release with that tag, written as
  the project writes it (`bat@v0.25.0`, `zig@0.15.1`).
- Versions are installed side by side. Commands get `@tag` appended (`bat@v0.26.1`), and the package
  directory is `$XDG_DATA_HOME/<name>@<tag>`. The plain names of the commands, man pages, and
  completions link to the active version: the latest install, or a given tag if no other version
  is active. `use` switches the active version.
- `install` also installs the dependencies, listed in `deps` with optional version specifiers like
  `"lib>=1.2,<2"`. An installed version that meets them is enough, otherwise the latest release is
  installed. The version of a tag is its first number, e.g. `1.27.1` in `go1.27.1`.
- `uninstall` removes all versions of a package, or one given as `name@tag`. It refuses if a
  remaining package would miss a dependency. If the active version goes, the most recently
  installed of the others becomes active.
- `upgrade` installs the latest release of each active version next to it and makes it active. The
  old version goes unless a dependent still needs it. Packages with an `upgrade` command update in
  place, external versions are left to their package manager.
- `autoremove` removes dependencies that no directly installed package needs anymore.
- `env` prints shell code setting the environment and `PATH` entries of the active versions. Every
  command also writes it to `$PMG_HOME/env.sh`, which shells can source without starting pmg.
- `external` prints the names of a package in system package managers, as `manager name` lines.
- Spec commands on hosts without CA certificates get those pmg downloads with, in
  `CURL_CA_BUNDLE` and `SSL_CERT_FILE`.
- `list` shows each installed version, whether it was installed directly or as a dependency, and
  whether it is active.
- Set `PMG_GH_TOKEN` (or `GH_TOKEN`) to avoid the rate limit of the GitHub API.
- `_PMG_COMPLETE=source_zsh pmg` prints the zsh completion script, which completes commands,
  specs, and installed packages.
- `PMG_ALPINE_MIRROR`, `PMG_CONDA_API`, and `PMG_CONDA_URL` replace the Alpine mirror, the
  anaconda.org API, and the conda download server. Their indexes are cached for an hour in
  `$XDG_CACHE_HOME/pmg`.

The registry has specs for common tools and for `glibc` (for running glibc programs on musl hosts),
`musl` (the reverse), `musl-libs` (libstdc++ and libgcc_s for musl hosts), and `patchelf`.

## License

MIT
