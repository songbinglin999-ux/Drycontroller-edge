#!/usr/bin/env bash

# Acquire the same process lock used by the OTA release engine. The descriptor
# remains open in this shell and is inherited by the active-release initializer.
factory_release_lock_acquire() {
  local lock_path="${1:?release lock path is required}"
  mkdir -p "$(dirname "$lock_path")"
  exec 9>"$lock_path"
  chmod 0600 "$lock_path"
  python3 -c 'import fcntl; fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)' 9>&9 || return 75
}
