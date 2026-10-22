# mcpsmith is now toolsmelt

This package only installs [toolsmelt](https://pypi.org/project/toolsmelt/) and keeps old names working:

- `import mcpsmith.runtime` (used by servers generated with mcpsmith) gives `toolsmelt.runtime`;
- the `mcpsmith` command runs `toolsmelt`;
- saved connections in `~/.mcpsmith`, `MCPSMITH_*` environment variables and keychain entries keep working.

New projects: `uvx toolsmelt setup`. See https://github.com/sepehr071/toolsmelt
