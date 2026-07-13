# Cardinal Gemini CLI plugin marketplace

> [!NOTE]
> This repository is a **release mirror**. Development happens in
> [cardinal-agent-plugins](https://github.com/cardinalhq/cardinal-agent-plugins) — send PRs there.


This repository publishes one Gemini CLI plugin: `cardinal-gemini-plugin`.

The plugin source lives at [`plugins/cardinal-gemini-plugin`](./plugins/cardinal-gemini-plugin), and the marketplace manifest at [`.agents/plugins/marketplace.json`](./.agents/plugins/marketplace.json) exposes only that plugin.

## Install

Install as a Gemini CLI extension (recommended shape — see the parity spec
`docs/specs/gemini-parity.md`):

```bash
python3 plugins/cardinal-gemini-plugin/scripts/cardinal-connect
```

The connect script prints a Cardinal approval URL, waits for approval, and:

- Copies the extension bundle to `~/.gemini/extensions/cardinal/` (manifest + hooks + `GEMINI.md` context file).
- Merges the OTLP `telemetry` block into `~/.gemini/settings.json` so Gemini CLI's built-in exporter also ships to Cardinal.
- Writes credentials to `~/.gemini/cardinal-secrets.json` (`0600`).

Restart Gemini CLI after connecting so it reloads MCP and hook config.

## Connect (in Gemini CLI)

Ask Gemini CLI:

```text
Use cardinal-connect
```

## Alternative install: settings.json only

If you cannot (or would rather not) use Gemini CLI's extensions directory,
pass `--no-extension` and the script merges managed `mcpServers.cardinal`
and `hooks.*` entries directly into `~/.gemini/settings.json`:

```bash
python3 plugins/cardinal-gemini-plugin/scripts/cardinal-connect --no-extension
```

## License

Apache 2.0. See [LICENSE](./LICENSE).
