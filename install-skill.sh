#!/bin/sh
# Linux / macOS convenience wrapper. The real, cross-platform installer is
# install-skill.py (stdlib only). This just forwards to it with python3.
#
#   sh install-skill.sh [--base-url URL] [--source ZIP] [--skills-dir DIR]
#
# Or install in one shot straight from the server:
#   curl -s http://192.168.61.147:28683/regq/install-skill.py | python3 -
exec python3 "$(dirname "$0")/install-skill.py" "$@"
