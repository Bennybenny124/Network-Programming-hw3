#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

rm -rf "$ROOT/client/play/"* "$ROOT/server/db/storage/"*

cp -a "$ROOT/_defaults/users.json"    "$ROOT/server/db/data/users.json"
cp -a "$ROOT/_defaults/games.json" "$ROOT/server/db/data/games.json"
cp -a "$ROOT/_defaults/comments.json" "$ROOT/server/db/data/comments.json"