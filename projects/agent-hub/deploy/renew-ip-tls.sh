#!/bin/sh
# Server-only: check the short-lived IP certificate and reload the owned gateway.
set -eu
umask 077

edge=/opt/agent-hub/edge
test -f "$edge/.env"
. "$edge/.env"
: "${HUB_CERTBOT_IMAGE:?Missing pinned Certbot image}"
: "${HUB_PUBLIC_IP:?Missing public IP}"
: "${HUB_ACME_WEBROOT:?Missing ACME webroot}"

exec 9>"$edge/renew.lock"
flock -n 9 || exit 0
case "${1:-}" in
  --deploy-only) ;;
  '')
    docker run --rm --name agent-hub-certbot-renew \
      --memory=256m --cpus=0.5 --pids-limit=64 \
      --security-opt no-new-privileges:true --cap-drop ALL \
      -v "$edge/acme:/etc/letsencrypt" \
      -v "$edge/acme-work:/var/lib/letsencrypt" \
      -v "$edge/acme-logs:/var/log/letsencrypt" \
      -v "$HUB_ACME_WEBROOT/.well-known/acme-challenge:/var/www/hub-acme/.well-known/acme-challenge" \
      "$HUB_CERTBOT_IMAGE" renew --quiet --cert-name agent-hub-ip --no-random-sleep-on-renew
    ;;
  *) printf 'Usage: renew-ip-tls.sh [--deploy-only]\n' >&2; exit 2 ;;
esac

source="$edge/acme/live/agent-hub-ip"
test -s "$source/fullchain.pem"
test -s "$source/privkey.pem"
openssl x509 -in "$source/fullchain.pem" -noout -checkend 86400 >/dev/null
openssl x509 -in "$source/fullchain.pem" -noout -text | awk -v ip="$HUB_PUBLIC_IP" '
  /X509v3 Subject Alternative Name/ { getline; if (index($0, "IP Address:" ip)) found=1 }
  END { exit !found }
'
cert_key="$(openssl x509 -in "$source/fullchain.pem" -pubkey -noout | openssl sha256)"
private_key="$(openssl pkey -in "$source/privkey.pem" -pubout | openssl sha256)"
test "$cert_key" = "$private_key"

if [ -f "$edge/tls/fullchain.pem" ] && cmp -s "$source/fullchain.pem" "$edge/tls/fullchain.pem"; then
  printf 'IP certificate is current.\n'
  exit 0
fi
install -d -m 750 -o root -g 10001 "$edge/tls"
install -m 644 "$source/fullchain.pem" "$edge/tls/fullchain.pem.new"
install -m 400 -o 10001 -g 10001 "$source/privkey.pem" "$edge/tls/privkey.pem.new"
mv -f "$edge/tls/privkey.pem.new" "$edge/tls/privkey.pem"
mv -f "$edge/tls/fullchain.pem.new" "$edge/tls/fullchain.pem"
if [ "$(docker inspect --format '{{.State.Running}}' agent-hub-edge 2>/dev/null || true)" = true ]; then
  docker exec agent-hub-edge nginx -t
  docker exec agent-hub-edge nginx -s reload
fi
printf 'IP certificate deployed.\n'
