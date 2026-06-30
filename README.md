# skill-regq

SoC Register Query — Interactive register definition lookup tool for Mercury, Venus, and other Realtek SoCs.

## Overview

`skill-regq` is a RealCoder skill that provides interactive register-level query capabilities. It allows you to:

- Search register definitions by **symbol/define name** (e.g. `VE_WAN_PORT_CTRL`)
- Search by **keyword** across register names, comments, and bitfields
- Look up by **hex address** (e.g. `0xf4300418`)
- View detailed bitfield definitions with encoding tables
- Read live hardware register values via **UART** or **SSH** (`devmem`)
- Support for **indirect-access registers** (GRAM, table memory)

## Installation

### From AI Plugin Hub (Marketplace)

1. Go to [AI Plugin Hub](https://devops.realtek.com/ai-plugin-hub)
2. Find `skill-regq` in the Marketplace
3. Click **Install** and copy the install command
4. Run the command in your terminal:

```bash
realcoder app install skill-regq
```

### Manual Installation

```bash
# Clone the repo
git clone https://github.com/a37805/skill-regq.git
cd skill-regq

# Run the installer
python install-skill.py
```

## Usage

Once installed, the skill is triggered automatically when you mention register symbols, addresses, or use `/regq`.

### Quick Start

```bash
# Prepare the environment (cache, deps, DB list)
regq_boot.py prepare

# Search by symbol
regq.py -db ~/.cache/regq/mercury.db --search-type symbol --search "VE_WAN"

# Search by address
regq.py -db ~/.cache/regq/mercury.db --address 0xf4300418

# Interactive TUI mode
regq.py -db ~/.cache/regq/mercury.db

# Live hardware reading via UART
regq.py -db ~/.cache/regq/mercury.db --address 0xf4300418 --uart COM3 --baud 115200

# Live hardware reading via SSH
regq.py -db ~/.cache/regq/mercury.db --address 0xf4300418 --ssh 192.168.1.1
```

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `REGQ_BASE_URL` | regq file server base URL | `http://192.168.61.147:28683/regq` |

## Requirements

- Python 3.8+
- RealCoder CLI

### Optional Dependencies (for live hardware reads)

- `pyserial` — UART `devmem` reads
- `paramiko` — SSH `devmem` reads
- `windows-curses` — TUI mode on Windows (installed automatically)

## License

Internal — Realtek Semiconductor Corp.
