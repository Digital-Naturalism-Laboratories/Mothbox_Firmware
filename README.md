# Mothbox_Firmware
This is where the firmware (software running on the device) is developed for the Mothbox. It's copied originally over from https://github.com/Digital-Naturalism-Laboratories/Mothbox/tree/main/Firmware so you can check that out if you want to see the full history

# Firmware trees in this repo

| Folder | What it is |
|---|---|
| `Mothbox_Unified/` + `mothbox_custom_Unified/` | **6.x -- one firmware for both the Pro and the DIY.** Detects the board at boot. Start here. |
| `Mothbox_Pro/` + `mothbox_custom_Pro/` | 5.x Pro-only firmware (frozen at 5.3.2) |
| `Mothbox_DIY/` + `mothbox_custom_DIY/` | 4.x DIY-only firmware (frozen at 4.18.3) |

Deploy `Mothbox_Unified/*` to `/home/pi/Desktop/Mothbox/` and `mothbox_custom_Unified/*` to `/boot/firmware/mothbox_custom/`.

`mothbox_custom_Unified/system/controls/` deliberately contains only `softwareversion.txt`, `safetygb.txt`, `onlyflash.txt` and `defaults/`. Every other control file (name, mode, schedule, nextwake, switches, hardware, GPS, calibration, timestamps) is per-box state that the box regenerates at boot, so copying the folder over a live box is safe. Never commit those generated files; a committed `name.txt` once renamed a box to `strongAbanto` until its next reboot.

## How the unified firmware tells a Pro from a DIY

Both builds are a Pi 5 with the Arducam 64MP OwlSight. Everything else on the GPIO differs, so `mothbox_hw.py` (the hardware abstraction layer) decides once per boot:

1. `Scheduler.py` calls `mothbox_hw.detect_hardware()` right after the Pi model check.
2. It raises the Pro's switched 3.3 V sensor rail (GPIO 27, active low; unconnected on a DIY), then probes I2C bus 1 for the three **PCA9555 switch expanders at 0x20 / 0x21 / 0x22**. Those chips exist only on the Pro PCB, so finding any of them means **Pro**; finding none means **DIY**. It also notes the LTR-303 light sensor (0x29) and identifies the voltage sensor at 0x40 (an INA260 answers with the TI manufacturer id 0x5449; otherwise it is the Pro's INA219).
3. The result is cached in `system/controls/hardware.txt`. Every other script reads the cache, so nothing probes I2C per photo.
4. `hardware,auto|pro|diy` in `mothbox_settings.csv` (also in the settings editor) overrides detection if a box is ever misidentified. If detection cannot run at all, the firmware assumes Pro.

Everything that touches a pin lives in `mothbox_hw.py`; the entry-point scripts (`Attract_On.py`, `Flash_Off.py`, `GetConfigSwitches.py`, ...) are one-line wrappers, so the two boards cannot drift apart again. `python3 tests/test_mothbox_hw.py` exercises both boards' code paths on any machine with fake GPIO and I2C.

What differs per board, all handled inside `mothbox_hw.py`:

| | Mothbox Pro | Mothbox DIY |
|---|---|---|
| Attract lights | GPIO 5/6/9/22 MOSFET channels (+ GPIO 7 pinctrl hack), active high, behind the 12 V rail on GPIO 23 | relay HAT ch1/ch3 on GPIO 26/21, active low |
| Flash | GPIO 19 MOSFET, active high, same 12 V rail | relay ch2 on GPIO 20, active low |
| Config switches | 40 DIP switches via 3 PCA9555 expanders (schedule, days, mode) | OFF jumper GPIO 16, DEBUG jumper GPIO 12; schedule always from the CSV |
| Voltage | INA219 @0x40 behind the 3.3 V sensor rail (GPIO 27) | optional INA260 @0x40, else "UNKNOWN" on the display |
| Other sensors | LTR-303 light @0x29, DS18B20 on 1-Wire GPIO 4 | none |
| Camera orientation | `VerticalFlip=auto` -> 0 | `VerticalFlip=auto` -> 1 (mounted upside-down) |
| e-paper display | always | optional, tolerated if absent |

# Changelog

## 6.0.0 -- unified firmware (Pro + DIY)

- One set of scripts and one image for both boards; the board is detected at boot (see above).
- `mothbox_hw.py` hardware abstraction layer; all light / rail / switch / voltage pin logic moved there. `Attract_On/Off`, `Flash_On/Off`, `Party`, `DebugMode`, `blink_standby`, `12vOn/Off`, `3v3SensorsOn/Off`, `GetConfigSwitches`, `Diagnostics`, `UpdateDisplay`, `TakePhoto` all dispatch through it.
- New `hardware` setting (auto / pro / diy) in `mothbox_settings.csv` and the editor. New `system/controls/hardware.txt` cache with the detection details.
- `VerticalFlip` in `camera_settings.csv` accepts `auto`.
- Diagnostics log gains a `Hardware:` line; voltage is logged in one format on both boards (`scripts/read_power.py`), so photo EXIF carries it on both.
- The e-paper footer shows `MOTHBOX PRO` / `MOTHBOX DIY`; photo EXIF `Model` says which body took it.
- Old DIY-only experiments moved to `scripts/legacy_DIY/`; two old Pro pin helpers to `scripts/legacy_Pro/`.
- Includes everything from 5.3.2 (final backup at session end, night-ordered hours on the display, correct EXIF, config.txt reference, 456 MHz camera link).

# Older changelog (Pro 5.x and DIY 4.x versions)

## 5.2.1 + 4.18.1 minor bugfix  WITH LARGE NAMING CHANGES

It turns out the way i was kind of hashing the raspberry pi serial numbers to create unique names for the mothboxes had a flaw that made them not as random as we thought (out of 100 mothboxes we had 4 sets with the same name!)
The names were still pretty random, so it's not that big of a problem, but at some point we need to fix this.

It was a minor correction to the Scheduler.py script, BUT IMPORTANTLY, this means if you upgrade to this firmware, your auto-generated name for your mothbox will change! You can always give it a custom name, but the autoname will very likely change.
So just heads up!


## 5.2.0 + 4.18.0 added minor bugfix 

added a rule via

sudo nano /etc/udev/rules.d/99-usb-mount.rules
ACTION=="add", SUBSYSTEMS=="usb", SUBSYSTEM=="block", ENV{ID_FS_USAGE}=="filesystem", RUN{program}+="/usr/bin/systemd-mount --no-block --collect $devnode /media/%k"
sudo udevadm control --reload-rules
sudo udevadm trigger

to make sure it reads the USB during non GUI modes

and then updated BACKUP and DISPLAY scripts




## 5.2.0

-power and memory saving features like
- Wifi only turns on in DEBUG or PARTY modes, otherwise off
- GUI only loads for DEBUG mode
 ---  This means a RPI 5 2GB can work with this! (image was tested on 2GB)
-Can program the pro with SWITCHES, flip U1 and switches will override internal schedule

-Photo interval - you can set how often photos get taken (default every 1 min). Min 1 minute, in 1 min intervals.


## 4.18.0

-power and memory saving features like
- Wifi only turns on in DEBUG or PARTY modes, otherwise off
- GUI only loads for DEBUG mode
 ---  This means a RPI 5 2GB can work with this! (image was tested on 2GB)

-Photo interval - you can set how often photos get taken (default every 1 min). Min 1 minute, in 1 min intervals.
- Safer writes
- safer segregated files for user input vs system control


## 4.16.4

This image is the latest and greatest for DIY v4 boxes.

Just released a new version of the 4.16 firmware. It has a lot of features I have been waiting to add. Namely:

    Automatically naming the backup folder on the USB after the mothbox’s name
    fixing the battery percent indicator to be a bit more accurate
    blocking mothbox cron functions at boot until the main scheduler.py has fully run (so we can do other sensing more accurately)
    moved “mode” to the controls.txt so other things can read the current mode

Big change: STANDBY mode- doesn’t just turn on when you turn on the mothbox during the day

The big improvement was probably in the UI though. We chunked the information on the epaper a lot better and are working on better fonts that are hinted for low resolution displays (thanks to some person on bluesky who was roasting me over my fonts)



## 5.0.3

This image is the latest and greatest for the new v5 boards.

- UPDATED sudo raspi-update and all that
- Takephoto stores photos in folders with device name now!
- Diagnostics.py can record the sensors
- Diagnostics also allows labels so startup diagnostics are separate than shutdown diagnostics and such
- does v5 board stuff like
-- log light (actually made improvements on arduino script that had lux calculation wrong!)
-- log power
-- log ALL SWITCH STATES at startup!
-There's a dedicated party button now
-Debug switch works
-Active Switch works
-flip camera correctly
- backup remainder changed to 8gb
- update display to refined landscape mode
- scripts for turning attractors and flashes on and off refined


TODO - Board temperature reading has problems because 1-wire service is weird for pi5 apparently. might be a thing in pi kernel to update
