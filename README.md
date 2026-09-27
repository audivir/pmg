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

- Templates can use `{{ tag }}`, `{{ version }}` (the tag without a leading `v`), and
  `{{ asset }}`.
- glibc hosts older than `min_glibc` get the musl asset.
- `bin` maps names in `~/.local/bin` to paths in the archive. A single top-level directory in
  the archive is stripped.
- `post_install` runs a shell command in the staging directory, which is also in `PREFIX`.

Specs are read from `PMG_SPECS`, by default `$XDG_CONFIG_HOME/pmg/specs`.

```bash
python -m pmg install bat
python -m pmg list
python -m pmg uninstall bat
python -m pmg autoremove
```

- `install` also installs the dependencies, listed in `deps`.
- `uninstall` refuses while another installed package depends on the package.
- `autoremove` removes dependencies that no directly installed package needs anymore.
- Set `PMG_GH_TOKEN` (or `GH_TOKEN`) to avoid the rate limit of the GitHub API.

## License

MIT
