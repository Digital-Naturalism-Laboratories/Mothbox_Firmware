Only these ship here, and the firmware never writes to this folder:
  softwareversion.txt  - bump by hand when releasing (see zzzz_NewImageChecklist.txt)
  safetygb.txt         - minimum free GB on the SD card before backups purge old photos
  onlyflash.txt        - reserved (not currently implemented)
  defaults/            - restore copies of the settings CSVs

As of firmware 6.0.2 every file the box GENERATES lives on the journaled ext4
root at /var/lib/mothbox/state/ instead of here: name, mode, switches,
schedule, nextwake, hardware, camera, timezone/utc, GPS, calibration and the
last-photo / last-backup timestamps.

Why: this partition is FAT32 with no journal, and it also holds config.txt and
the kernel. A power cut while the firmware rewrote a control file here could
corrupt the whole partition and leave a box that would not boot at all. In Oct
2026 that took out 3 of 6 deployed boxes. The firmware was writing ~725 files a
night here; it now writes none.

Old copies of those files may still be sitting in this folder on an upgraded
box. They are read only as a fallback until each value is written once, and
are then ignored. They are safe to leave alone.
