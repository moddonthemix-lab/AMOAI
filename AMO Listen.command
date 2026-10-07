#!/bin/bash
# Double-click to start hands-free mode ("Hey AMO"). Keep the window open (minimise it).
# To start it automatically: System Settings → General → Login Items → + → pick this file.
cd "$(dirname "$0")"
echo "AMO is listening — say \"Hey AMO\". Close this window to stop."
exec ./.venv/bin/amo listen
