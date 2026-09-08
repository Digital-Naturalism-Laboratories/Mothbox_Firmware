#!/bin/bash
# setup_new_image.sh -- post-flash setup + health check for a Mothbox.
#
# Run on the Pi AFTER rsyncing the firmware and mothbox_custom onto it:
#     bash /home/pi/Desktop/Mothbox/scripts/setup_new_image.sh          # check only
#     bash /home/pi/Desktop/Mothbox/scripts/setup_new_image.sh --apply  # also fix config.txt
#
# --apply installs the reference config.txt (backing up the old one). Everything
# else is read-only: it tells you what is wrong, it does not silently change the box.
set -uo pipefail

REF=/boot/firmware/mothbox_custom/reference_boot_config.txt
CFG=/boot/firmware/config.txt
APPLY=0; [ "${1:-}" = "--apply" ] && APPLY=1
FAIL=0; NEEDS_REBOOT=0
ok(){   printf '  \033[32mOK\033[0m   %s\n' "$1"; }
bad(){  printf '  \033[31mFAIL\033[0m %s\n' "$1"; FAIL=1; }
warn(){ printf '  \033[33mWARN\033[0m %s\n' "$1"; }
hdr(){  printf '\n=== %s ===\n' "$1"; }

hdr "1. config.txt"
if [ ! -f "$REF" ]; then
  bad "No reference at $REF -- rsync mothbox_custom_Unified/ to /boot/firmware/mothbox_custom/ first"
else
  # compare active (non-comment, non-blank) lines only
  active(){ grep -vE '^\s*(#|$)' "$1" | sed 's/[[:space:]]*$//' | sort; }
  if diff <(active "$CFG") <(active "$REF") >/dev/null 2>&1; then
    ok "config.txt already matches the reference"
  elif [ "$APPLY" = "1" ]; then
    BAK="$CFG.bak-$(date +%Y%m%d-%H%M%S)"
    sudo cp "$CFG" "$BAK" && sudo cp "$REF" "$CFG" \
      && { ok "installed reference config.txt (old one saved as $(basename "$BAK"))"; NEEDS_REBOOT=1; } \
      || bad "could not write $CFG"
  else
    warn "config.txt differs from the reference. Lines that would change:"
    diff <(active "$CFG") <(active "$REF") | sed 's/^/       /'
    warn "re-run with --apply to install the reference"
  fi
fi

hdr "2. Bootloader EEPROM (RTC wake depends on this)"
EE_LIVE=$(vcgencmd bootloader_config 2>/dev/null)
EE_IMG=$(sudo rpi-eeprom-config 2>/dev/null)
for kv in "POWER_OFF_ON_HALT=1" "WAKE_ON_GPIO=0"; do
  if grep -qx "$kv" <<<"$EE_LIVE" || grep -qx "$kv" <<<"$EE_IMG"; then ok "$kv"
  else bad "$kv is NOT set -- the box will not wake itself. Fix: sudo rm -f /var/lib/mothbox/eeprom_* && sudo reboot"; fi
done
printf '       bootloader: %s\n' "$(vcgencmd bootloader_version 2>/dev/null | head -1)"

hdr "3. Hardware interfaces"
[ -e /dev/i2c-1 ]      && ok "/dev/i2c-1 (sensors, config switches)"      || bad "/dev/i2c-1 missing -- dtparam=i2c_arm=on"
[ -e /dev/spidev0.0 ]  && ok "/dev/spidev0.0 (e-paper display)"           || bad "/dev/spidev0.0 missing -- dtparam=spi=on"

# The Pro's DS18B20 hangs off the switched 3.3 V sensor rail, so it only shows
# up on the 1-Wire bus once that rail is up. Raise it before looking, or the
# answer just depends on whether something else happened to leave it on.
python3 -c "import sys; sys.path.insert(0,'/home/pi/Desktop/Mothbox'); import mothbox_hw; mothbox_hw.rail_3v3(True)" 2>/dev/null
sleep 2
if ls /sys/bus/w1/devices/28-* >/dev/null 2>&1; then
  ok "DS18B20 board thermometer ($(basename $(ls -d /sys/bus/w1/devices/28-* | head -1)))"
else
  warn "no 1-Wire sensor found (fine if this board has none fitted)"
fi

# NOTE: capture first, then grep. Piping into `grep -q` under `set -o pipefail`
# reports a false failure: grep exits on the first match, rpicam-hello takes
# SIGPIPE (141), and pipefail surfaces that as the pipeline's status.
CAM_OUT=$(rpicam-hello --list-cameras 2>&1)
if grep -q ov64a40 <<<"$CAM_OUT"; then
  ok "camera: $(grep -m1 ov64a40 <<<"$CAM_OUT" | cut -c1-60)"
else
  bad "camera NOT detected -- check the ribbon, and dtoverlay=ov64a40 in config.txt"
  printf '%s\n' "$CAM_OUT" | sed 's/^/       /' | head -5
fi

hdr "4. Software the firmware depends on"
python3 -c "from PIL import _imagingft" 2>/dev/null \
  && ok "Pillow FreeType (display fonts)" \
  || bad "Pillow FreeType broken -- sudo apt install --reinstall python3-pil libfribidi0 libraqm0"
python3 -c "import piexif, numpy, psutil, smbus2, crontab, schedule" 2>/dev/null \
  && ok "python modules (piexif, numpy, psutil, smbus2, crontab, schedule)" \
  || bad "a required python module is missing -- see the traceback: python3 -c 'import piexif, numpy, psutil, smbus2, crontab, schedule'"
for f in mothbox_hw.py mothbox_exif.py Scheduler.py TakePhoto.py UpdateDisplay.py eeprom_safe.py firstboot_guard.py DebugMode.py Party.py; do
  [ -f "/home/pi/Desktop/Mothbox/$f" ] || bad "missing /home/pi/Desktop/Mothbox/$f -- rsync did not complete"
done

# rsync -a copies permissions FROM THE SOURCE, so syncing from a machine where
# these are not marked executable strips the +x bit here. The firmware now
# always names its interpreter, so this is no longer fatal, but a stripped bit
# means the source tree is wrong and should be fixed there too.
NOEXEC=$(find /home/pi/Desktop/Mothbox -maxdepth 1 -name '*.py' -not -perm -u+x 2>/dev/null | wc -l)
if [ "$NOEXEC" -eq 0 ]; then
  ok "scripts are executable"
else
  warn "$NOEXEC script(s) lost the executable bit. Harmless now, but fix the source tree:"
  find /home/pi/Desktop/Mothbox -maxdepth 1 -name '*.py' -not -perm -u+x | sed 's/^/       /'
  warn "on the Mac: chmod +x <repo>/Mothbox_Unified/*.py && re-rsync"
fi

hdr "5. Which Mothbox does the firmware think this is?"
python3 /home/pi/Desktop/Mothbox/mothbox_hw.py 2>/dev/null | sed 's/^/       /' \
  || warn "hardware detection failed -- run: python3 /home/pi/Desktop/Mothbox/mothbox_hw.py"

hdr "Result"
[ "$NEEDS_REBOOT" = "1" ] && printf '  config.txt changed -- REBOOT before trusting the checks above.\n'
if [ "$FAIL" = "0" ]; then
  printf '  \033[32mBox looks good.\033[0m Next: the 2-minute wake test --\n'
  printf '    echo 0 | sudo tee /sys/class/rtc/rtc0/wakealarm >/dev/null\n'
  printf '    echo +120 | sudo tee /sys/class/rtc/rtc0/wakealarm >/dev/null\n'
  printf '    cat /sys/class/rtc/rtc0/wakealarm && sudo shutdown -h now\n'
else
  printf '  \033[31mSomething above needs fixing.\033[0m\n'
fi
exit $FAIL
