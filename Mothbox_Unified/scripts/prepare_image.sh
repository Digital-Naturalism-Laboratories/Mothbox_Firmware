#!/bin/bash
# prepare_image.sh -- clean a master Mothbox before its SD card is cloned into
# an image (zzzz_NewImageChecklist.txt steps 4 and 5a-5d, plus checks 8 and 9).
#
#     sudo bash /home/pi/Desktop/Mothbox/scripts/prepare_image.sh
#
# Then: sudo shutdown -h now, and clone the card.
#
# It deletes ONLY the exact folders and file patterns listed below, asks before
# doing anything, and reports -- never deletes -- anything unexpected on the
# boot partition. User accounts and passwords (pi / luna) are not touched.

MB=/home/pi/Desktop/Mothbox
FAT=/boot/firmware/mothbox_custom
STATE=/var/lib/mothbox/state

ok()   { echo "  OK   $*"; }
warn() { echo "  WARN $*"; }
hdr()  { echo; echo "=== $* ==="; }

if [ "$(id -u)" -ne 0 ]; then
  echo "Run with sudo:  sudo bash $0"
  exit 1
fi

count() { [ -d "$1" ] && find "$1" -type f | wc -l || echo 0; }
echo "This prepares THIS box's card to become the image for every other box. It will:"
echo "  - delete all photos:    $(count $MB/photos) in photos/, $(count $MB/photos_backedup) in photos_backedup/"
echo "  - delete all logs:      $(count $MB/logs) files in logs/"
echo "  - wipe per-box state:   $STATE/*.txt + clock_battery_log.csv (name, GPS, calibration, schedule, coin cell)"
echo "  - keep the ssh host keys, so every box from this image has the same one"
echo "  - tidy the boot partition and the apt / journal caches"
read -r -p "Type yes to continue: " answer
[ "$answer" = "yes" ] || { echo "Stopped -- nothing was changed."; exit 1; }

hdr "Photos and logs (step 5)"
for d in photos photos_backedup logs; do
  if [ -d "$MB/$d" ]; then
    rm -rf "${MB:?}/$d"
  fi
  mkdir -p "$MB/$d"
  chown pi:pi "$MB/$d"
  ok "$d/ emptied"
done

hdr "Per-box state (5a)"
rm -f "$STATE"/*.txt "$STATE"/*.tmp "$STATE"/clock_battery_log.csv
ok "$STATE is empty -- it regenerates on first boot (incl. this box's coin cell history)"

hdr "Test leftovers and caches (5b)"
rm -rf "$MB/__pycache__" "$MB/tests/__pycache__" "$MB/scripts/__pycache__" "$MB/tools/__pycache__" "$MB/tools/mock_out"
rm -f /home/pi/thermtest.log /home/pi/fat_*.txt /home/pi/settings_backup.csv
[ -d /media/usbtest ] && ! mountpoint -q /media/usbtest && rmdir /media/usbtest 2>/dev/null
ok "python caches, display mock output and test files removed"

hdr "Per-box identity (5c)"
# The ssh host keys are KEPT on purpose: every box from this image shares one,
# so a laptop that has connected to one mothbox.local connects to any other
# without the "REMOTE HOST IDENTIFICATION HAS CHANGED" wall. Per-box keys only
# guard against an impostor box on the same network, which isn't a worry here.
# Make sure nothing regenerates them on a flashed box's first boot.
if systemctl is-enabled regenerate_ssh_host_keys.service >/dev/null 2>&1; then
  systemctl disable regenerate_ssh_host_keys.service >/dev/null 2>&1
  ok "regenerate_ssh_host_keys.service disabled (it would give each box new keys)"
fi
if ls /etc/ssh/ssh_host_*_key >/dev/null 2>&1; then
  ok "ssh host keys kept -- every box from this image shares them"
else
  warn "no ssh host keys found -- run: sudo ssh-keygen -A   (or nobody can ssh in)"
fi
rm -f /home/pi/.bash_history /root/.bash_history
ok "shell history cleared"
if sshd -T 2>/dev/null | grep -qi "^passwordauthentication yes"; then
  ok "ssh password login is on (pi / luna works)"
else
  warn "ssh password login looks OFF -- check PasswordAuthentication in /etc/ssh/sshd_config"
fi
echo "  INFO saved wifi networks are kept on purpose (shared field networks):"
nmcli -t -f NAME,TYPE connection show 2>/dev/null | grep -i wireless | sed 's/^/         /'

hdr "Boot partition (5d)"
EXPECTED="camera_settings.csv
camera_settings_editor.html
mothbox_settings.csv
mothbox_settings_editor.html
reference_boot_config.txt
system/controls/README.txt
system/controls/defaults/default_camera.txt
system/controls/safetygb.txt
system/controls/softwareversion.txt
system/version_and_legal.txt
timezone_list.txt"
# Known junk: macOS droppings, editor temp files, files older firmware wrote
# here before 6.0.2 moved them to $STATE, and three files retired in 6.0.2.
find "$FAT" -type f \( -name '.DS_Store' -o -name '._*' -o -name '*.tmp' \) -print -delete | sed 's/^/  removed /'
for f in system/controls/onlyflash.txt system/controls/defaults/default_backup_controls.txt \
         system/controls/defaults/default_settings.csv; do
  [ -f "$FAT/$f" ] && rm -f "$FAT/$f" && echo "  removed $FAT/$f (retired file)"
done
for f in "$FAT"/system/controls/*.txt; do
  case "$(basename "$f")" in
    README.txt|safetygb.txt|softwareversion.txt) ;;
    *) [ -f "$f" ] && rm -f "$f" && echo "  removed $f (generated control file, lives in $STATE now)" ;;
  esac
done
ACTUAL=$(cd "$FAT" && find . -type f | sed 's|^\./||' | sort)
EXTRA=$(comm -13 <(echo "$EXPECTED" | sort) <(echo "$ACTUAL"))
MISSING=$(comm -23 <(echo "$EXPECTED" | sort) <(echo "$ACTUAL"))
[ -n "$EXTRA" ]   && warn "unexpected files (left alone -- check them):" && echo "$EXTRA" | sed "s|^|         $FAT/|"
[ -n "$MISSING" ] && warn "missing files (re-rsync mothbox_custom_Unified):" && echo "$MISSING" | sed 's/^/         /'
[ -z "$EXTRA$MISSING" ] && ok "exactly the $(echo "$EXPECTED" | wc -l) shipped files"

hdr "Caches (4)"
journalctl --vacuum-size=50M >/dev/null 2>&1 && ok "journal trimmed to 50 MB"
apt-get clean && ok "apt cache cleared"

hdr "Default schedule (8)"
grep -E "^(weekday|hour|minute|runtime|photo_interval)," "$FAT/mothbox_settings.csv" | cut -d, -f1,2 | sed 's/^/  /'

hdr "Clock coin cell"
CELL=$(grep -E "^rtc_battery," "$FAT/mothbox_settings.csv" | cut -d, -f2 | tr -d ' \r')
echo "  rtc_battery = ${CELL:-<missing>}  (every box flashed from this image starts with this)"
if grep -qE "^[[:space:]]*dtparam=rtc_bbat_vchg" /boot/firmware/config.txt; then
  warn "config.txt still charges the coin cell at every boot (dtparam=rtc_bbat_vchg)."
  warn "Remove it -- the firmware turns charging on only for rtc_battery=rechargeable:"
  warn "  sudo sed -i 's/^dtparam=rtc_bbat_vchg=/#dtparam=rtc_bbat_vchg=/' /boot/firmware/config.txt"
else
  ok "config.txt does not charge the coin cell by itself"
fi

hdr "config.txt and the rest (9)"
bash "$MB/scripts/setup_new_image.sh"

echo
echo "Done. Now:  sudo shutdown -h now   -- then clone the card."
