#!/usr/bin/env bash
# A separate lab network for the Pluto+: one NIC, a fixed address, no gateway, no DNS, no forwarding.
# The Pluto+ can talk to this PC and nothing else, and nothing outside can talk to the Pluto+.
. "$(dirname "$0")/lib.sh"
need_root; check_os
[ -n "$LAB_IFACE" ] || die "set LAB_IFACE in deploy/setup.env (see: ip -br link)"
if [ "$DRY_RUN" != 1 ]; then ip link show "$LAB_IFACE" >/dev/null 2>&1 || die "no network card named $LAB_IFACE (see: ip -br link)"; fi

say "Lab NIC $LAB_IFACE = $LAB_HOST_IP (Pluto+ expected at $PLUTO_IP)"
write_file /etc/netplan/60-oasa-lab.yaml 0600 root:root <<EOF
# OASA lab network. No gateway, no DNS: this link goes nowhere except the Pluto+.
network:
  version: 2
  ethernets:
    $LAB_IFACE:
      dhcp4: false
      dhcp6: false
      accept-ra: false
      link-local: []
      optional: true
      addresses: [$LAB_HOST_IP]
EOF

say "No routing between networks"
write_file /etc/sysctl.d/60-oasa-lab.conf 0644 root:root <<'EOF'
net.ipv4.ip_forward = 0
net.ipv6.conf.all.forwarding = 0
EOF
run sysctl --system >/dev/null

run netplan generate
note "Applying the network config. If you are logged in over this NIC, the link may blink."
run netplan apply

say "Done"
note "Check:  ip -br addr show $LAB_IFACE   and   ping -c1 $PLUTO_IP"
note "If the Pluto+ does not answer, its own address may differ: set PLUTO_IP and LAB_HOST_IP to match its config.txt."
