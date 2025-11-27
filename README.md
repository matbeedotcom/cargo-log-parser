# Cargo Log Parser

A Claude Code skill for parsing and filtering `cargo` build logs. Extract errors, warnings, and diagnostics with regex-based filtering by file path, message, or error code.

## Installation

### Method 1: Plugin Marketplace (Recommended for Claude Code)

```bash
# Add this repo as a marketplace
/plugin marketplace add YOUR_USERNAME/cargo-log-parser

# Install the plugin
/plugin install cargo-log-parser@cargo-log-parser-marketplace
```

### Method 2: Direct Plugin Install

```bash
# Clone and install directly
git clone https://github.com/YOUR_USERNAME/cargo-log-parser.git
/plugin add ./cargo-log-parser
```

### Method 3: Simple Skill (Manual)

```bash
# Clone to your skills directory
git clone https://github.com/YOUR_USERNAME/cargo-log-parser.git ~/.claude/skills/cargo-log-parser
```

### Method 4: Project-Level Skill

```bash
# Add to your project's skills
git clone https://github.com/YOUR_USERNAME/cargo-log-parser.git .claude/skills/cargo-log-parser
```

## Usage

Once installed, Claude automatically uses this skill when you're debugging Rust/cargo build errors.

### CLI Examples

```bash
# Pipe from cargo build
cargo build 2>&1 | python scripts/cargo_log_parser.py --errors

# Filter by file pattern
cargo build 2>&1 | python scripts/cargo_log_parser.py --errors --file "tests/.*"

# Filter by error message
cargo build 2>&1 | python scripts/cargo_log_parser.py --message "cannot find"

# Filter by error code
cargo build 2>&1 | python scripts/cargo_log_parser.py --code "E0425"

# Compact stream output
cargo build 2>&1 | python scripts/cargo_log_parser.py --errors --stream

# JSON output for programmatic use
cargo build 2>&1 | python scripts/cargo_log_parser.py --errors --json
```

### Key Flags

| Flag | Description |
|------|-------------|
| `-e, --errors` | Show only errors |
| `-w, --warnings` | Show only warnings |
| `-f, --file PATTERN` | Filter by file path regex |
| `-m, --message PATTERN` | Filter by message regex |
| `-c, --code PATTERN` | Filter by error code regex |
| `--stream` | Compact one-line output |
| `--raw` | Raw log text only |
| `--json` | JSON output |
| `--group-by-file` | Group by source file |
| `--group-by-code` | Group by error code |

### Common Regex Patterns

| Pattern | Matches |
|---------|---------|
| `tests/.*` | Test files |
| `src/.*` | Source files |
| `.*/mod\.rs` | Module files |
| `E03\d\d` | Borrow checker errors |
| `E04\d\d` | Name resolution errors |
| `unused_.*` | Unused warnings |

## Repository Structure

```
cargo-log-parser/
├── .claude-plugin/
│   ├── plugin.json         # Plugin manifest
│   └── marketplace.json    # Marketplace config
├── SKILL.md                # Skill instructions
├── scripts/
│   └── cargo_log_parser.py # The parser tool
└── README.md
```

## License

MIT
