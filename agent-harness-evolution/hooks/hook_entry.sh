#!/bin/sh
# Thin wrapper for settings-file installs: harness-evo hook <event>.
# Uses a pip-installed harness-evo if there is one, else the bundled launcher (bin/harness-evo),
# which needs only a Python 3.9+ and fails open without one.
event="${1:-}"
if command -v harness-evo >/dev/null 2>&1; then
  exec harness-evo hook "$event"
fi
case "$0" in */*) here=${0%/*} ;; *) here=. ;; esac
exec sh "$here/../bin/harness-evo" hook "$event"
