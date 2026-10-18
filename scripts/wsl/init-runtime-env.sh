#!/bin/bash
# Create or safely complete a private runtime environment file without echoing secrets.
set -euo pipefail
umask 077

mode=create
case "${1:-}" in
  "") ;;
  -h|--help) printf 'Usage: %s [--complete]\n' "$0"; exit 0 ;;
  --complete) mode=complete ;;
  *) echo "Usage: $0 [--complete]" >&2; exit 2 ;;
esac

if [ "$(id -u)" -eq 0 ]; then
  echo "FATAL: run this as the service user, not root." >&2
  exit 1
fi
if [ -z "${HOME:-}" ] || [ ! -d "$HOME" ] || [ -L "$HOME" ]; then
  echo "FATAL: HOME must be an existing, non-symlink directory." >&2
  exit 1
fi

config_dir="$HOME/.config"
env_dir="$config_dir/autoalpha"
env_file="$env_dir/runtime.env"
lock_file="$env_dir/.runtime-env.lock"
if [ -L "$config_dir" ] || [ -L "$env_dir" ]; then
  echo "FATAL: config directories must not be symlinks." >&2
  exit 1
fi
mkdir -p -m 700 -- "$env_dir"
chmod 700 -- "$env_dir"
if [ "$(stat -c '%u' -- "$env_dir")" != "$(id -u)" ]; then
  echo "FATAL: $env_dir must be owned by the current service user." >&2
  exit 1
fi
if [ -L "$lock_file" ]; then
  echo "FATAL: lock file must not be a symlink." >&2
  exit 1
fi
exec 9>"$lock_file"
chmod 600 -- "$lock_file"
flock -x 9

file_exists=false
original_identity=""
if [ -e "$env_file" ] || [ -L "$env_file" ]; then
  if [ "$mode" != complete ]; then
    echo "FATAL: $env_file already exists; use --complete to add missing credentials." >&2
    exit 1
  fi
  if [ -L "$env_file" ] || [ ! -f "$env_file" ]; then
    echo "FATAL: existing env path must be a regular, non-symlink file." >&2
    exit 1
  fi
  file_mode=$(stat -c '%a' -- "$env_file")
  file_owner=$(stat -c '%u' -- "$env_file")
  if [ "$file_owner" != "$(id -u)" ] || [[ ! "$file_mode" =~ ^[0-7]{3,4}$ ]] \
    || (( (8#$file_mode & 077) != 0 )); then
    echo "FATAL: existing env file must be owned by this user and private (0600 or stricter)." >&2
    exit 1
  fi
  if LC_ALL=C grep -q $'\r' "$env_file"; then
    echo "FATAL: existing env file contains CR characters; save it with LF line endings before completing." >&2
    exit 1
  fi
  if ! bash -n "$env_file"; then
    echo "FATAL: existing env file has invalid shell syntax." >&2
    exit 1
  fi
  file_exists=true
  original_identity=$(stat -c '%d:%i:%s:%Y' -- "$env_file")
elif [ "$mode" = complete ]; then
  echo "No existing env file; --complete will create it with all required values."
fi

# Check for a non-empty standard assignment without displaying or executing the
# existing file. Runtime loading remains the caller's separate responsibility.
file_has_value() {
  local key="$1"
  local line value found=false
  [ "$file_exists" = true ] || return 1
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      "$key="*) value=${line#*=} ;;
      "export $key="*) value=${line#*=} ;;
      *) continue ;;
    esac
    case "$value" in
      ""|"''"|"\"\"") continue ;;
    esac
    found=true
    break
  done < "$env_file"
  [ "$found" = true ]
}

env_value() {
  printenv -- "$1" 2>/dev/null || true
}

write_env() {
  printf '%s=%q\n' "$1" "$2" >> "$tmp_file"
}

has_llm_key=false
llm_name=""
llm_key=""
for name in DEEPSEEK_API_KEY AI_IDE_LLM_API_KEY AI_IDE_API_KEY OPENAI_API_KEY; do
  if file_has_value "$name"; then
    has_llm_key=true
    break
  fi
  value=$(env_value "$name")
  if [ -n "$value" ]; then
    llm_name="$name"
    llm_key="$value"
    has_llm_key=true
    break
  fi
done

if [ "$has_llm_key" != true ]; then
  printf '%s\n' \
    "Supported provider variables: DEEPSEEK_API_KEY, AI_IDE_LLM_API_KEY," \
    "AI_IDE_API_KEY, OPENAI_API_KEY. Use a newly rotated key."
  printf 'LLM key variable name: '
  IFS= read -r llm_name
  case "$llm_name" in
    DEEPSEEK_API_KEY|AI_IDE_LLM_API_KEY|AI_IDE_API_KEY|OPENAI_API_KEY) ;;
    *) echo "FATAL: unsupported LLM key variable name." >&2; exit 1 ;;
  esac
  read -r -s -p "Enter $llm_name (input hidden): " llm_key
  printf '\n'
  if [ -z "$llm_key" ]; then
    echo "FATAL: LLM key cannot be empty." >&2
    exit 1
  fi
  lower_key=${llm_key,,}
  case "$lower_key" in
    *your-deepseek-api-key*|*mock-api-key*|sk-在此*)
      echo "FATAL: placeholder values are not accepted." >&2
      exit 1
      ;;
  esac
  unset lower_key
fi

db_name=""
db_value=""
for name in DATABASE_URL DB_PASSWORD; do
  if file_has_value "$name"; then
    db_name="$name"
    break
  fi
  value=$(env_value "$name")
  if [ -n "$value" ]; then
    db_name="$name"
    db_value="$value"
    break
  fi
done
if [ -z "$db_name" ]; then
  read -r -s -p "Enter PostgreSQL quantmind role password (input hidden): " db_value
  printf '\n'
  if [ -z "$db_value" ]; then
    echo "FATAL: DB password cannot be empty." >&2
    exit 1
  fi
  db_name=DB_PASSWORD
fi

internal_secret=""
jwt_secret=""
for name in INTERNAL_CALL_SECRET SECRET_KEY; do
  if file_has_value "$name"; then
    continue
  fi
  value=$(env_value "$name")
  if [ -n "$value" ]; then
    if [ "$name" = INTERNAL_CALL_SECRET ]; then internal_secret="$value"; else jwt_secret="$value"; fi
  elif [ "$name" = INTERNAL_CALL_SECRET ]; then
    internal_secret=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
  else
    jwt_secret=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
  fi
done

tmp_file=$(mktemp "$env_dir/.runtime.env.XXXXXX")
cleanup() {
  if [ -n "${tmp_file:-}" ] && [ -e "$tmp_file" ]; then
    rm -f -- "$tmp_file"
  fi
}
trap cleanup EXIT
chmod 600 -- "$tmp_file"
if [ "$file_exists" = true ]; then
  cp -- "$env_file" "$tmp_file"
  chmod 600 -- "$tmp_file"
fi

if [ -n "$internal_secret" ]; then write_env INTERNAL_CALL_SECRET "$internal_secret"; fi
if [ -n "$jwt_secret" ]; then write_env SECRET_KEY "$jwt_secret"; fi
if [ -n "$llm_name" ] && [ -n "$llm_key" ]; then write_env "$llm_name" "$llm_key"; fi
if [ -n "$db_name" ] && [ -n "$db_value" ]; then write_env "$db_name" "$db_value"; fi

unset internal_secret jwt_secret llm_key db_value value

if [ "$file_exists" = true ]; then
  if [ -L "$env_file" ] || [ "$(stat -c '%d:%i:%s:%Y' -- "$env_file")" != "$original_identity" ]; then
    echo "FATAL: env file changed during setup; refusing to replace it." >&2
    exit 1
  fi
  mv -f -- "$tmp_file" "$env_file"
  action=Updated
else
  if ! ln -- "$tmp_file" "$env_file"; then
    echo "FATAL: could not create $env_file; it may have appeared concurrently." >&2
    exit 1
  fi
  rm -f -- "$tmp_file"
  action=Created
fi
tmp_file=""
trap - EXIT
chmod 600 -- "$env_file"
printf '%s %s (mode 0600). Secret values were not printed.\n' "$action" "$env_file"
