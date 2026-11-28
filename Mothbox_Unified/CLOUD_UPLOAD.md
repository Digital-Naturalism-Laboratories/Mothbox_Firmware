# Cloud upload

A Mothbox can upload its new photos to a server as a second backup, next to the USB drive. It uses [rclone](https://rclone.org), so any service rclone supports works: a Hetzner Storage Box, any SFTP/FTP/WebDAV server, S3 and S3-compatible storage, Google Drive, Dropbox, OneDrive, Nextcloud and more. The box does not need to be reachable from outside; no VPN, Tailscale or SSH keys.

The code is `Cloud_Backup.py`. `Backup_Files.py` starts it in the background.

## What happens to the photos

The cloud follows the same rule as the USB backup:

1. **New photos stay in `photos/`** until every backup that is switched on has them:
   - the USB drive, when one is plugged in;
   - the server, when cloud upload is on.

   An upload counts only once rclone has **checked** that the file arrived.
2. **Then they move to `photos_backedup/`**, which keeps them as long as possible.
3. **When the SD card runs low** (`safetygb`), the oldest nights in `photos_backedup/` are deleted to keep the box going. Everything there is confirmed on the USB drive or the server. `photos/` is never deleted from.
4. **If the SD card is low while a backup lags behind**, one confirmed copy is enough for a photo to move. That covers no internet for days, or a full USB drive. The box keeps running rather than filling up.

A box **without a USB drive** can therefore run on cloud upload alone: the server is its backup, and `photos_backedup/` is cleared as the card fills.

## When it uploads

All of these must hold:
- **`cloud_upload` is `True`.**
- **The box is in HI POWER mode**, the only mode that keeps WiFi on and stays awake:
  - the HI switch on a Pro;
  - or `hi_power_always` set to `True`. That turns HI POWER on whenever the Active switch is on, which is how a Mothbox DIY (no HI switch) gets it. The box never sleeps, so it uses much more battery.
- **`cloud_when` allows it:**
  - `between_windows` (the default) uploads while the box waits, dark, between capture windows. This keeps uploads away from the camera's memory on a 1 GB Pi, and uploads by day when there is solar power.
  - `anytime` also uploads during windows. Use it if the schedule never leaves a gap.
  - With `between_windows`, an upload still running when a window opens is stopped and carries on next time.
- **`cloud_interval` minutes** (default 60) have passed since the last try. An upload also starts as soon as each capture window ends.

The box also needs internet: a WiFi network it knows (`wifissid`/`wifipass`), or a phone hotspot.

## Settings (mothbox_settings.csv, or the settings editor)

| Setting | What it is |
|---|---|
| `hi_power_always` | `True`: Active runs as HI POWER. |
| `cloud_upload` | `True` turns uploads on. |
| `cloud_service` | `sftp`, `ftps`, `ftp`, `webdav`, `s3`, or `rclone_conf`. |
| `cloud_url` | The server:<br>• sftp, ftps, ftp: the host name; a port can go on the end, as in `host:23`.<br>• webdav: the full `https://` address.<br>• s3: the endpoint, blank for AWS. |
| `cloud_bucket` | The folder on the server.<br>• s3: `bucket/folder`.<br>• rclone_conf: optionally `remote:folder`. |
| `cloud_user`, `cloud_password` | Login for sftp, ftps, ftp and webdav. |
| `cloud_api_key`, `cloud_api_secret`, `cloud_region` | S3 only. |
| `cloud_when` | `between_windows` or `anytime`. |
| `cloud_interval` | Minutes between tries, 10–1440. |
| `cloud_bwlimit` | Optional speed limit, e.g. `2M` (MB/s). |

On the server, the layout matches the USB drive:
- `<folder>/photos_backup_<name>/<night>/…`
- `<folder>/logs_<name>/`
- `<folder>/status_<name>.json`: last upload, photos still waiting, free space, software version. It is updated on every upload, so you can see from home that a box is alive.

**Anyone holding the SD card can read these settings, password included.** For storage like a Hetzner Storage Box, give each box (or project) its own sub-account restricted to one folder, so a stolen box can't reach your other data. Prefer SFTP or FTPS: plain FTP sends the password and photos unencrypted.

## Recipe: Hetzner Storage Box

1. **In Hetzner's console,** create a **sub-account** for the box or project:
   - restrict it to one folder;
   - turn on SSH support (for SFTP) or external reachability;
   - note its user name (`uXXXXX-subN`), its password, and the host name Hetzner shows for it.
2. **In the settings editor:**
   - **Upload to the cloud:** Yes.
   - **How to connect:** SFTP.
   - **Server:** that host name.
   - **User name and password:** the sub-account's.
   - **Folder:** optional; it is relative to the sub-account's folder.
3. **Test it** with the box on WiFi (Debug mode is fine):
   ```
   python3 /home/pi/Desktop/Mothbox/Cloud_Backup.py --test
   ```
   It writes `mothbox_test_<name>.txt` to the folder and says `TEST OK`.
4. **Run it.** Turn the HI switch on (Pro), or set "HI POWER always on" (DIY). The upload log is `logs/Cloud_log.txt`.

Hetzner also offers FTPS (`ftps`) and WebDAV (`webdav`, address `https://uXXXXX-subN.your-storagebox.de`). Plain FTP works too, but it is unencrypted.

## Recipe: Google Drive, Dropbox, OneDrive, anything else (rclone.conf)

These services log in through a browser, which the box can't do. Make the login on your computer instead:

1. **Install rclone on your computer** and run `rclone config`. Make a remote for the service, e.g. one named `gdrive`.
2. **Copy the config file to the SD card.** `rclone config file` shows where it is. Copy that `rclone.conf` next to `mothbox_settings.csv`.
3. **Set the connection:** How to connect = `rclone_conf`. Folder = `moths` (uses the first remote in the file) or `gdrive:moths` (names it).
4. **Test** with `--test` as above.

The box keeps its own copy of `rclone.conf` in `/var/lib/mothbox/state/`, because rclone rewrites the file when it refreshes a login. That write must not happen on the boot partition. A newer `rclone.conf` on the card replaces the box's copy.

## Installing rclone

`setup_new_image.sh` warns if rclone is missing. Without rclone the box carries on as before, and the log says cloud upload is skipped. To add it, with internet:

```
sudo apt install -y rclone
```

Put it in the next golden image so new boxes have it.

## What has and hasn't been tested

- **Tested:** `tests/test_cloud_backup.py` covers:
  - the photo rule;
  - reading the settings;
  - building the rclone connection;
  - the ledger of confirmed photos;
  - moving photos and freeing space.
- **Tested:** the Scheduler dry run covers `hi_power_always` on a box with no HI switch: it never powers off, and only the Active switch ends it.
- **Not yet tested:** the end-to-end upload through real rclone. It runs automatically in that test file when rclone is installed, uploading to a local folder.
- **Not yet tested:** a real Hetzner box, and a real Pi.
