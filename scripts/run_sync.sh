#!/bin/zsh
set -euo pipefail
project_dir="${0:A:h:h}"
cd "$project_dir"
set -a
source "$project_dir/local.env"
set +a
exec /opt/homebrew/bin/python3 -m radar sync --url "$RADAR_SERVER_URL" --vault "$OBSIDIAN_VAULT"

