# Windows convenience wrapper. The real, cross-platform installer is
# install-skill.py (stdlib only). This just forwards to it with python.
#
#   powershell -ExecutionPolicy Bypass -File install-skill.ps1 [--base-url URL] ...
#
# Or install in one shot straight from the server:
#   (Invoke-WebRequest http://192.168.61.147:28683/regq/install-skill.py).Content | python -
python "$PSScriptRoot\install-skill.py" @args
