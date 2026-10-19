#!/usr/bin/env bash
# Check the Windows host volume that backs the WSL distro's ext4.vhdx.
set -euo pipefail

HOST_MOUNT=${AUTOALPHA_HOST_MOUNT:-/mnt/c}
MIN_FREE_GIB=${AUTOALPHA_MIN_HOST_FREE_GIB:-20}

if [[ ! "$MIN_FREE_GIB" =~ ^[1-9][0-9]{0,5}$ ]] || (( MIN_FREE_GIB > 1048576 )); then
  echo "FATAL: AUTOALPHA_MIN_HOST_FREE_GIB must be an integer from 1 to 1048576." >&2
  exit 2
fi
if [ ! -d "$HOST_MOUNT" ]; then
  echo "FATAL: host volume mount is unavailable: $HOST_MOUNT" >&2
  exit 2
fi

available_kib=$(df -Pk -- "$HOST_MOUNT" | awk 'NR == 2 {print $4}')
if [[ ! "$available_kib" =~ ^[0-9]+$ ]]; then
  echo "FATAL: unable to read available space for $HOST_MOUNT" >&2
  exit 2
fi
available_gib=$((available_kib / 1024 / 1024))
echo "Host volume $HOST_MOUNT: ${available_gib} GiB available (alert threshold: ${MIN_FREE_GIB} GiB)."

if (( available_kib < MIN_FREE_GIB * 1024 * 1024 )); then
  echo "CRITICAL: host volume $HOST_MOUNT is below the configured free-space threshold." >&2
  exit 1
fi
