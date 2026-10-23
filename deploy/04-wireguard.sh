#!/usr/bin/env bash
# WireGuard for the owner: maintenance access (SSH) to the lab PC. Not used by Claude or the job service.
. "$(dirname "$0")/lib.sh"
need_root; check_os

say "Server key (stays on this PC, mode 0600)"
KEY=/etc/wireguard/oasa-lab.key
if [ ! -f "$KEY" ]; then
  if [ "$DRY_RUN" = 1 ]; then note "DRY: would make $KEY"; else
    install -d -m 0700 /etc/wireguard
    ( umask 077; wg genkey > "$KEY" )
    note "made $KEY"
  fi
else note "kept existing $KEY"; fi

peer=""
if [ -n "$OWNER_WG_PUBKEY" ]; then
  peer="
[Peer]
# the owner's laptop
PublicKey = $OWNER_WG_PUBKEY
AllowedIPs = $OWNER_WG_IP"
fi

say "wg0 config"
if [ "$DRY_RUN" = 1 ]; then priv="PRIVATE_KEY_NOT_SHOWN"; else priv="$(cat "$KEY")"; fi
write_file /etc/wireguard/wg0.conf 0600 root:root <<EOF
# OASA lab WireGuard. The peer below is the only one allowed in.
[Interface]
Address = $WG_SERVER_IP
ListenPort = $WG_PORT
PrivateKey = $priv
$peer
EOF
if [ "$DRY_RUN" != 1 ]; then
  run systemctl enable wg-quick@wg0
  if [ -n "$OWNER_WG_PUBKEY" ]; then run systemctl restart wg-quick@wg0; fi
fi

say "Done"
if [ "$DRY_RUN" != 1 ]; then
  pub="$(wg pubkey < "$KEY")"
  note "This PC's WireGuard PUBLIC key (give it to your laptop's WireGuard config): $pub"
fi
if [ -z "$OWNER_WG_PUBKEY" ]; then
  owner "Install WireGuard on your laptop, make a key pair there, put the PUBLIC key in deploy/setup.env as OWNER_WG_PUBKEY, run this script again."
fi
owner "Your router must forward UDP $WG_PORT to this PC (or skip WireGuard and do maintenance in person)."
note "Laptop config:"
note "  [Interface]  PrivateKey = <your laptop's private key>   Address = $OWNER_WG_IP"
note "  [Peer]       PublicKey = <the key above>   Endpoint = <your home IP or DDNS name>:$WG_PORT   AllowedIPs = ${WG_SERVER_IP%/*}/32   PersistentKeepalive = 25"
