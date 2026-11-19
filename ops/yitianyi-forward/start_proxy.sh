#!/bin/bash
set -eu

config=/root/autodl-tmp/futures_price_forecasting-forward/supervisord.conf
control=/root/miniconda3/bin/supervisorctl

if ! "$control" -c "$config" pid >/dev/null 2>&1; then
    /root/miniconda3/bin/supervisord -c "$config"
fi

status=$("$control" -c "$config" status futures-newsdesk-forward || true)
case "$status" in
    *RUNNING*|*STARTING*) printf '%s\n' "$status" ;;
    *) "$control" -c "$config" start futures-newsdesk-forward ;;
esac
