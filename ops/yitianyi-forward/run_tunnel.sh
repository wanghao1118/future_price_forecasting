#!/bin/bash
set -eu

ssh_options=(
    -C
    -T
    -o BatchMode=yes
    -o ExitOnForwardFailure=yes
    -o ServerAliveInterval=20
    -o ServerAliveCountMax=3
    -o ConnectTimeout=15
)

/usr/bin/ssh "${ssh_options[@]}" yitianyi \
    'bash /root/autodl-tmp/futures_price_forecasting-forward/start_proxy.sh'

exec /usr/bin/ssh "${ssh_options[@]}" -N \
    -R 127.0.0.1:18765:127.0.0.1:8765 \
    -R 127.0.0.1:18778:127.0.0.1:4178 \
    yitianyi
