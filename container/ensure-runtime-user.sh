#!/bin/sh
set -eu

name=${1:?name}
uid=${2:?uid}
gid=${3:?gid}
home=${4:?home}

case "$uid:$gid" in
  *[!0-9:]*|:*|*:) echo "UID and GID must be non-negative integers" >&2; exit 1 ;;
esac

group_name=$(getent group "$gid" | cut -d: -f1 || true)
if [ -z "$group_name" ]; then
  groupadd --gid "$gid" "$name"
  group_name=$name
fi

user_name=$(getent passwd "$uid" | cut -d: -f1 || true)
if [ -z "$user_name" ]; then
  useradd --no-create-home --home-dir "$home" --shell /bin/bash --uid "$uid" --gid "$gid" "$name"
fi

install -d -o "$uid" -g "$gid" -m 700 "$home"
printf '%s:%s\n' "$uid" "$gid" > /run/runtime-identity
