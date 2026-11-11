#!/bin/sh
set -eu
nginx -t
nginx -g 'daemon off;' &
gateway_pid=$!
trap 'kill -QUIT "$gateway_pid" 2>/dev/null || true' TERM INT
previous=$(sha256sum /tls/current/fullchain.pem)
while kill -0 "$gateway_pid" 2>/dev/null; do
  sleep 30 &
  wait $! || true
  current=$(sha256sum /tls/current/fullchain.pem)
  if test "$current" != "$previous"; then
    nginx -t && nginx -s reload
    previous=$current
  fi
done
wait "$gateway_pid"
