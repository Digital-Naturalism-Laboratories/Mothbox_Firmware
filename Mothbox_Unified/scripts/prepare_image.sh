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
echo "  - wipe per-box state:   $STATE/*.txt (name, GPS, calibration, schedule state)"
echo "  - remove the ssh host keys (each flashed box makes its own on first boot)"
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
rm -f "$STATE"/*.txt "$STATE"/*.tmp
ok "$STATE is empty -- it regenerates on first boot"

hdr "Test leftovers and caches (5b)"
rm -rf "$MB/__pycache__" "$MB/tests/__pycache__" "$MB/scripts/__pycache__" "$MB/tools/__pycache__" "$MB/tools/mock_out"
rm -f /home/pi/thermtest.log /home/pi/fat_*.txt /home/pi/settings_backup.csv
ok "python caches, display mock output and test files removed"

hdr "Per-box identity (5c)"
if systemctl cat regenerate_ssh_host_keys.service >/dev/null 2>&1; then
  rm -f /etc/ssh/ssh_host_*
  systemctl enable regenerate_ssh_host_keys.service >/dev/null 2>&1
  ok "ssh host keys removed; each flashed box makes its own on first boot"
else
  warn "regenerate_ssh_host_keys.service not found -- KEEPING the ssh host keys"
  warn "(without that service a flashed box would have no keys and nobody could ssh in)"
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

hdr "config.txt and the rest (9)"
bash "$MB/scripts/setup_new_image.sh"

echo
echo "Done. Now:  sudo shutdown -h now   -- then clone the card."
