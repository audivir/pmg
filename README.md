# pmg

A simple package manager for prebuilt binaries in the home directory. It installs release assets
from GitHub or other URLs into `~/.local/bin`, tracks the installed files and dependencies, and
works without admin rights.

## Prerequisites

- Python 3.10 or newer

## Installation

```bash
pip install pmg
```

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
  `{{ data }}` (`$XDG_DATA_HOME`), `{{ bin }}`, `{{ dir }}`, and `{{ dirs.<key> }}`.
- glibc hosts older than `min_glibc` get the musl asset.
- `bin` maps names in `~/.local/bin` to paths in the archive. A single top-level directory in
  the archive is stripped. `links` adds symlinks instead of copies.
- `man` lists man pages in the archive, the file extension is the section.
- `completions` maps a command to its completion script per shell, either a path in the archive
  or `{ cmd = "..." }`, a command printing it.
- Everything else belongs in the package directory `{{ dir }}`, by default `$XDG_DATA_HOME/<name>`
  (set `dir` to change it). `content = true` makes the unpacked archive the package directory,
  `dirs` adds more directories. The package owns these directories as a whole.
- `post_install` runs a shell command in the staging directory `PREFIX`.

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

Specs are searched in `$PMG_SPECS_DIR`, then in `$PMG_HOME/specs`, then in the specs shipped with
pmg. `PMG_HOME` defaults to `$XDG_DATA_HOME/pmg` and also holds the install records.

```bash
python -m pmg install bat
python -m pmg install bat@v0.25.0
python -m pmg use bat@v0.25.0
python -m pmg list
python -m pmg uninstall bat@v0.25.0
python -m pmg autoremove
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
- `autoremove` removes dependencies that no directly installed package needs anymore.
- `list` shows each installed version, whether it was installed directly or as a dependency, and
  whether it is active.
- Set `PMG_GH_TOKEN` (or `GH_TOKEN`) to avoid the rate limit of the GitHub API.

## License

MIT
