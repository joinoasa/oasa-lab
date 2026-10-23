#!/usr/bin/env bash
# Packages, radio drivers and USB rules for the lab PC (Ubuntu 24.04 LTS). Safe to run again.
. "$(dirname "$0")/lib.sh"
need_root; check_os

say "Packages"
run apt-get update
pkg_install ca-certificates curl gpg git python3 python3-venv python3-pip usbutils polkitd \
  libiio-utils libiio0 rtl-sdr uhubctl nftables wireguard-tools

say "Keep the SDR control tools from running as network services"
# The SDR network tools have no authentication. They stay off; tests start them on localhost when needed.
for unit in iiod.service rtl_tcp.service SoapySDRServer.service; do
  if systemctl list-unit-files "$unit" 2>/dev/null | grep -q "$unit"; then
    run systemctl disable --now "$unit"
    note "disabled $unit"
  fi
done

say "RTL-SDR: stop the TV driver grabbing the dongle"
write_file /etc/modprobe.d/oasa-rtlsdr-blacklist.conf 0644 root:root <<'EOF'
# The kernel's DVB driver claims RTL-SDR dongles; the SDR tools need them free.
blacklist dvb_usb_rtl28xxu
blacklist rtl2832
blacklist rtl2830
EOF
if grep -q '^dvb_usb_rtl28xxu' /proc/modules; then run modprobe -r dvb_usb_rtl28xxu || note "unplug the dongle, or reboot, to release it"; fi

say "USB access rules (groups plugdev and dialout only)"
getent group plugdev >/dev/null || run groupadd --system plugdev
write_file /etc/udev/rules.d/99-oasa-lab.rules 0644 root:root < "$HERE/udev/99-oasa-lab.rules"
run udevadm control --reload-rules
run udevadm trigger --subsystem-match=usb --subsystem-match=tty

say "Done"
note "Next: sudo ./02-network.sh   (plug the Pluto+ in first; unplug and replug USB radios so the new rules apply)"
