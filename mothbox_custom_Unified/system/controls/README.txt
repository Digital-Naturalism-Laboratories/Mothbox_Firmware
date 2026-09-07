Only three control files ship with the firmware:
  softwareversion.txt  - bump by hand when releasing (see zzzz_NewImageChecklist.txt)
  safetygb.txt         - minimum free GB on the SD card before backups purge old photos
  onlyflash.txt        - reserved (not currently implemented)
plus defaults/ (restore copies of the settings CSVs).

Every other file in this folder is PER-BOX STATE that the box writes itself:
name, mode, hours/minutes/weekdays/runtime, photo_interval, nextwake, switches,
hardware, timezone/utc, lat/lon/gpstime, calibration (aflensposition, autogain,
exposuretime, lastcalibration), last_photo_time, last_backup_time,
shutdown_enabled. Scheduler.py, GPS.py and TakePhoto.py recreate them at boot.

Do not commit those files to the repo: copying this folder onto a box would
overwrite its live name and schedule state until the next reboot (the
'strongAbanto' incident, Sept 2026).
