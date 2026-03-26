# Mothbox_Firmware
This is where the firmware (software running on the device) is developed for the Mothbox. It's copied originally over from https://github.com/Digital-Naturalism-Laboratories/Mothbox/tree/main/Firmware so you can check that out if you want to see the full history

# Changelog (Pro 5.x and DIY 4.x versions)

5.2.0 + 4.18.0 added minor bugfix 

added a rule via

sudo nano /etc/udev/rules.d/99-usb-mount.rules
ACTION=="add", SUBSYSTEMS=="usb", SUBSYSTEM=="block", ENV{ID_FS_USAGE}=="filesystem", RUN{program}+="/usr/bin/systemd-mount --no-block --collect $devnode /media/%k"
sudo udevadm control --reload-rules
sudo udevadm trigger

to make sure it reads the USB during non GUI modes

and then updated BACKUP and DISPLAY scripts





5.2.0

-power and memory saving features like
- Wifi only turns on in DEBUG or PARTY modes, otherwise off
- GUI only loads for DEBUG mode
 ---  This means a RPI 5 2GB can work with this! (image was tested on 2GB)
-Can program the pro with SWITCHES, flip U1 and switches will override internal schedule

-Photo interval - you can set how often photos get taken (default every 1 min). Min 1 minute, in 1 min intervals.


4.18.0

-power and memory saving features like
- Wifi only turns on in DEBUG or PARTY modes, otherwise off
- GUI only loads for DEBUG mode
 ---  This means a RPI 5 2GB can work with this! (image was tested on 2GB)

-Photo interval - you can set how often photos get taken (default every 1 min). Min 1 minute, in 1 min intervals.
- Safer writes
- safer segregated files for user input vs system control


4.16.4

This image is the latest and greatest for DIY v4 boxes.

Just released a new version of the 4.16 firmware. It has a lot of features I have been waiting to add. Namely:

    Automatically naming the backup folder on the USB after the mothbox’s name
    fixing the battery percent indicator to be a bit more accurate
    blocking mothbox cron functions at boot until the main scheduler.py has fully run (so we can do other sensing more accurately)
    moved “mode” to the controls.txt so other things can read the current mode

Big change: STANDBY mode- doesn’t just turn on when you turn on the mothbox during the day

The big improvement was probably in the UI though. We chunked the information on the epaper a lot better and are working on better fonts that are hinted for low resolution displays (thanks to some person on bluesky who was roasting me over my fonts)



5.0.3

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
