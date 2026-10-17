# WinUse

**[中文说明](README.zh-CN.md)** · English

**A unified desktop-capability facade for AI agents.** cua-driver is the engine; WinUse is the steering wheel.

WinUse drives nothing itself — it injects no input, captures no pixels and owns no window handles. It does one thing: translate [cua-driver](https://github.com/trycua/cua)'s driver-level primitives into coarse-grained tools that an agent can call **safely, cheaply and precisely**.

---

## The problem

Handing the raw driver to an agent costs you three ways:

| | |
|---|---|
| **Expensive** | The default route is "screenshot + vision model" — roughly 2,100–2,700 tokens per look |
| **Imprecise** | On mixed-DPI multi-monitor setups, screenshot pixels and native coordinates must be converted; get the conversion wrong and the click lands somewhere else |
| **Risky** | Any agent can call the driver directly, bypassing approval gates |

WinUse's answer:

1. **Text first** — read the Windows UI Automation (UIA) tree by default. A screenshot is an explicit escalation, never the default path. Interactive elements are a minority of the tree (measured ≈38%), so read-only text is trimmed away.
2. **Elements before coordinates** — semantic actions (`Invoke` / `ValuePattern`) are independent of coordinates, DPI and screen. Pixel clicks are the fallback for self-drawn surfaces only (canvas, video, WebGL, custom-rendered UI).
3. **Act → guard → verify** — dangerous actions pass an internal confirmation gate and are written to an audit log. After acting you get a deterministic assertion, not "it seems to have moved".

## Why it is cheaper — measured, not claimed

| How the state was fetched | Tokens |
|---|---|
| 4,804-element window: full snapshot, then trimmed | ≈ 14,788 |
| Same window: projected on demand with `query` | ≈ 2,285 |

The saving is a projection, not a lossy truncation: you ask only for the rows you need, and element indexes stay valid inside the same snapshot.

## Tools (22)

### Read — see what is there without looking at pixels

| Tool | What it does |
|---|---|
| `winuse_state` | Text-first state of one window: interactive elements (with `element_index`, `element_token`, `snapshot_id`), window list, real foreground window |
| `winuse_find` | Fuzzy element lookup by text — one wrong character still hits |
| `winuse_read` | Read several elements' values/states in a single round trip |
| `winuse_display` | Per-monitor physical rectangles, work areas, per-monitor scale and DPI, window ownership |
| `winuse_screenshot` | Capture to a file (explicit escalation; the only pixel path) |
| `winuse_ocr` | Windows built-in OCR — no network, no third-party dependency |
| `winuse_search` | Whole-disk file search through Everything (`es.exe`), read-only |

### Act — one gesture, addressed by element

| Tool | What it does |
|---|---|
| `winuse_act` | Element-level action: `click` / `invoke` / `toggle` / `select` / `expand` / `set_value` / `type` / `key` / `scroll` / `activate` — **dry-run by default** |
| `winuse_drag` | Drag (hold → move → release), element-to-element or by coordinates |
| `winuse_multi` | Batch several interface actions in one call — **batch input counts as dangerous** |
| `winuse_menu` | Invoke menu items by path, resolved layer by layer |
| `winuse_window` | Window lifecycle: minimise / maximise / restore / always-on-top / move / resize / close |
| `winuse_app` | Application lifecycle: launch (without stealing focus) / list / kill |

### Wait, verify and system facts

| Tool | What it does |
|---|---|
| `winuse_wait` | Conditional wait: window appears/disappears, title matches, element appears, text appears |
| `winuse_verify` | Deterministic assertions against one window (predicate array, three-state result — `unknown` never means success) |
| `winuse_system` | OS / CPU / memory / disks / power, plus top processes by memory |
| `winuse_session` | Session and desktop state: locked or not, secure desktop, which capabilities are usable |
| `winuse_virtual_desktop` | Which virtual desktop a window belongs to, and whether it is the current one |
| `winuse_registry` | Read the registry (read-only by default; sensitive hives refused) |
| `winuse_clipboard` | Read/write clipboard — `read` lists types only unless text is requested |
| `winuse_notify` | Windows toast notification — dry-run by default |
| `winuse_audit` | Read WinUse's own audit log (read-only) |

## Requirements

| Requirement | Necessity | Notes |
|---|---|---|
| Windows 10 / 11 | required | WinUse is Windows-only by design |
| Python ≥ 3.10 | required | **Zero third-party runtime dependencies** — standard library only (Windows API via `ctypes`) |
| [`cua-driver`](https://github.com/trycua/cua) ≥ 0.28.0 | **required** | The only driver. Auto-detected from `~/.cua-driver/packages/current` → `%LOCALAPPDATA%/Programs/Cua/...` → `PATH`, or set `WINUSE_CUA_DRIVER` |
| `powershell.exe` (Windows PowerShell 5.1) | required for OCR / toast | PowerShell 7 (`pwsh.exe`) **cannot** substitute — it has no built-in WinRT projection |
| `msg.exe` | optional | Fallback channel for notifications only |

"Zero dependencies" is a deliberate design constraint, not a gap: the core and every facade use the standard library only, so any Python runs it and nothing leaks into the environment outside the project.

## Install

```bash
git clone https://github.com/freehul/winuse.git
cd winuse
python -m winuse doctor              # self-check: config -> driver -> one real read-only probe
python -m winuse doctor --no-probe   # driver version only, no real call
```

No installation step is needed to use the CLI. To install it as a package (and to run the tests):

```bash
python -m pip install -e ".[test]"   # test deps are declared here, never at runtime
python -m pytest -q
```

`scripts/verify.sh` runs everything in one go (compile -> tests -> warnings-as-errors):

```bash
sh scripts/verify.sh                 # PYTHON='py -3.11' to pick an interpreter
sh scripts/verify.sh --doctor        # also run one real read-only self-check
```

Optional: copy `.env.example` to `.env` to override defaults. `.env` is never committed.

## Use it from your agent

### Any MCP client

```bash
python -m winuse mcp serve           # MCP over stdio
python -m winuse mcp tools           # list the tool table
```

### Hermes (plugin adapter)

```bash
python adapters/hermes/install.py --home <HERMES_HOME>
hermes plugins enable winuse
hermes plugins list                  # confirm it is enabled
```

See `adapters/hermes/README.md` for the deployment model and its two safety gates.

### Plain CLI

```bash
python -m winuse state               # text-first state of the foreground window
python -m winuse display             # multi-monitor facts
python -m winuse act --help          # every action is a dry run until --execute
```

## Safety model

- **Dry-run by default.** `winuse act` prints the command it would send; only `--execute` (CLI) or `execute: true` (MCP) really touches the desktop.
- **Confirmation gate inside the core.** Destructive and bulk operations require an explicit `confirm`, and the gate lives in the core — not in the facades — so a new client cannot forget it.
- **Audit log.** Every executed action is appended to `~/.winuse/audit.jsonl` (path and rotation are configurable).
- **Snapshot binding.** An `element_index` is only valid inside the `snapshot_id` it came from; actions without the matching snapshot are refused (`SNAPSHOT_REQUIRED`), which prevents a stale index from clicking the wrong element.
- **One driver, no second set.** A second driver would collide on focus and coordinate conventions and bypass approval gates.
- **No resident HTTP/SSE server.** In-process use goes through a subprocess; external use goes through MCP over stdio — nothing is exposed on a port.

## Project layout

```
winuse/
  config.py          configuration (reads the project's own .env, deliberately not process env)
  cli.py             facade 3: CLI (fallback + troubleshooting)
  driver/
    cua.py           cua-driver wrapper — the only IO boundary
    errors.py        stable error codes with human-readable messages
adapters/hermes/     Hermes plugin adapter — protocol translation only, no business logic
scripts/             verify.sh, end-to-end smoke, git-hook installer, Windows wrapper
tests/               unit tests; always against a fake driver, never the real desktop
```

Design contracts, backlog and research notes are maintained privately and are **not** part of this repository — the code and this README are the published surface.

## Running the tests

Unit tests never touch the real driver or the desktop: `tests/fake_driver.py` and per-case stub drivers stand in for it. Real-machine checks go through `python -m winuse doctor` and controlled probes.

```bash
python -m pytest -q            # the only acceptance command
python -m pytest -q -W error   # warnings as errors: protocol/resource problems show up here first
```

## Contributing

Issues and pull requests are welcome. Project rules (zero third-party dependencies, one driver, TDD, data hygiene) live in `AGENTS.md`; the acceptance entry point is `sh scripts/verify.sh` — a change is not done until it is green.

Data hygiene is enforced by a commit hook: `sh scripts/install_git_hooks.sh` enables it once per machine.

## License

MIT — see [LICENSE](LICENSE).
