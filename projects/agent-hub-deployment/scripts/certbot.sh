#!/bin/sh
set -eu
case "${1:-renew}" in
  issue)
    certbot certonly --non-interactive --agree-tos --email "$HUB_ACME_EMAIL" \
      --webroot --webroot-path /var/www/acme --cert-name agent-hub \
      "$HUB_ACME_IDENTIFIER_FLAG" "$HUB_PUBLIC_HOST" --preferred-profile "$HUB_ACME_PROFILE" \
      --deploy-hook 'python /deployment/publish-tls.py'
    ;;
  renew)
    while true; do
      if certbot renew --quiet --no-random-sleep-on-renew \
        --deploy-hook 'python /deployment/publish-tls.py'; then
        printf 'Certificate renewal check completed.\n'
      else
        printf 'Certificate renewal failed; retained the active certificate.\n' >&2
      fi
      sleep 21600 &
      wait $!
    done
    ;;
  *) exit 2 ;;
esac
