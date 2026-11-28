#!/usr/bin/python3
"""
Cloud upload (Cloud_Backup.py): reading the settings, building the rclone
connection, and above all the rule for when a photo may leave photos/:

    every backup that is on must have it (the USB drive when one is plugged
    in, the cloud when cloud upload is on); when the SD card is low, one
    confirmed copy is enough, so the box keeps going.

Runs anywhere; no rclone needed. If rclone is installed, the last part also
uploads to a local folder through rclone for real.

    python3 tests/test_cloud_backup.py
"""
import os, shutil, sys, tempfile, time
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import Cloud_Backup as cb

failures = 0
def check(label, got, want):
    global failures
    ok = got == want
    print(f"{'OK  ' if ok else 'FAIL'} {label}")
    if not ok:
        print(f"     got:  {got!r}\n     want: {want!r}")
        failures += 1

tmp = Path(tempfile.mkdtemp(prefix="mothbox_cloud_test_"))

def write(path, data=b"x", age_s=600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    t = time.time() - age_s
    os.utime(path, (t, t))
    return path

# --- the rule ---------------------------------------------------------------
leave = cb.may_leave_photos
check("cloud on, no USB: leaves once the server has it", leave(True, True, False, False, False), True)
check("cloud on, no USB: stays until then", leave(True, False, False, False, False), False)
check("cloud and USB: waits for both", (leave(True, True, True, False, False), leave(True, False, True, True, False),
                                        leave(True, True, True, True, False)), (False, False, True))
check("SD card low: one confirmed copy is enough", (leave(True, False, True, True, True), leave(True, True, True, False, True)),
      (True, True))
check("SD card low: but never with no copy at all", leave(True, False, True, False, True), False)
check("USB only (cloud off): as before", (leave(False, False, True, True, False), leave(False, False, True, False, False)),
      (True, False))
check("no backup on at all: stays", leave(False, False, False, False, True), False)

# --- settings ---------------------------------------------------------------
csv_path = tmp / "mothbox_settings.csv"
csv_path.write_text("﻿SETTING,VALUE,DETAILS\r\ncloud_upload, True ,x\r\ncloud_service,SFTP,x\r\n"
                    "cloud_interval,5,x\r\ncloud_when,ANYTIME,x\r\n", encoding="utf-8")
cfg = cb.cloud_config(cb.read_settings(csv_path))
check("settings: Excel BOM and CRLF, values trimmed, interval kept to 10+",
      (cfg["enabled"], cfg["service"], cfg["interval_min"], cfg["when"]), (True, "sftp", 10, "anytime"))
d = cb.cloud_config({})
check("defaults: off, every 60 min, between windows", (d["enabled"], d["interval_min"], d["when"], d["service"]),
      (False, 60, "between_windows", ""))
check("old free-text services still read",
      [cb.normalize_service(v) for v in ("Amazon S3 (or S3-compatible)", "WebDAV", "rclone", "Nextcloud", "Antenna")],
      ["s3", "webdav", "rclone_conf", "webdav", "antenna"])
check("cloud needed only in HI POWER", (cb.cloud_needed({"cloud_upload": "True"}, "HI_POW"),
                                        cb.cloud_needed({"cloud_upload": "True"}, "ACTIVE"),
                                        cb.cloud_needed({"cloud_upload": "False"}, "HI_POW")), (True, False, False))

# --- the connection -----------------------------------------------------------
ob = lambda pw: "OBSCURED(" + pw + ")"
base = cb.cloud_config({"cloud_upload": "True"})
def spec(**kw):
    c = dict(base, **kw)
    return cb.remote_spec(c, ob, kw.pop("conf", None) if "conf" in kw else None)

root, env, err = spec(service="sftp", url="sftp://u123-sub1.your-storagebox.de:23", user="u123-sub1",
                      password="pw", folder="mothboxes")
P = "RCLONE_CONFIG_MOTHBOX_"
check("sftp (Hetzner): host, port, user, obscured password",
      (root, env[P + "TYPE"], env[P + "HOST"], env[P + "PORT"], env[P + "USER"], env[P + "PASS"], err),
      ("MOTHBOX:mothboxes", "sftp", "u123-sub1.your-storagebox.de", "23", "u123-sub1", "OBSCURED(pw)", None))
root, env, err = spec(service="ftps", url="u123.your-storagebox.de", user="u", password="p")
check("ftps: FTP with explicit TLS, no port given", (env[P + "TYPE"], env.get(P + "EXPLICIT_TLS"), P + "PORT" in env, root),
      ("ftp", "true", False, "MOTHBOX:"))
check("ftp: missing password named", spec(service="ftp", url="h", user="u")[2], "ftp needs a password (cloud_password)")
root, env, err = spec(service="s3", key="AK", secret="SK", folder="bucket/box")
check("s3 without an endpoint is AWS", (env[P + "PROVIDER"], env[P + "ACCESS_KEY_ID"], root), ("AWS", "AK", "MOTHBOX:bucket/box"))
root, env, err = spec(service="s3", key="AK", secret="SK", folder="b", url="https://s3.example.org", region="eu")
check("s3 with an endpoint", (env[P + "PROVIDER"], env[P + "ENDPOINT"], env[P + "REGION"]), ("Other", "https://s3.example.org", "eu"))
check("webdav needs https://", spec(service="webdav", url="cloud.example.org")[2] is not None, True)
check("unsupported service explained", "rclone_conf" in spec(service="antenna")[2], True)
check("empty service explained", "cloud_service is empty" in spec(service="")[2], True)
conf = "# made by rclone config\n[gdrive]\ntype = drive\n\n[other]\ntype = local\n"
check("rclone.conf: the first remote", cb.remote_spec(dict(base, service="rclone_conf", folder="moths"), ob, conf)[0],
      "gdrive:moths")
check("rclone.conf: remote:folder as written", cb.remote_spec(dict(base, service="rclone_conf", folder="other:x"), ob, conf)[0],
      "other:x")
check("rclone.conf missing", cb.remote_spec(dict(base, service="rclone_conf"), ob, None)[2] is not None, True)
check("remote paths join", (cb.rjoin("MOTHBOX:", "a"), cb.rjoin("MOTHBOX:x", "a", "b.json"), cb.rjoin("r:x/", "a")),
      ("MOTHBOX:a", "MOTHBOX:x/a/b.json", "r:x/a"))

# --- the ledger and moving --------------------------------------------------------
photos, backedup, usb = tmp / "photos", tmp / "photos_backedup", tmp / "usb" / "photos_backup_box"
a = write(photos / "night1" / "a.jpg", b"aaaa")
b = write(photos / "night1" / "b.jpg", b"bbbbbb")
new = write(photos / "night1" / "c.jpg", b"c", age_s=10)
ready = cb.ready_photos(photos)
check("a photo under 2 minutes old is not uploaded yet", sorted(ready), ["night1/a.jpg", "night1/b.jpg"])
ledger_path = tmp / "cloud_confirmed.txt"
cb.write_ledger(ready, ledger_path)
ledger = cb.read_ledger(ledger_path)
check("ledger round trip", ledger, ready)
check("confirmed while unchanged", cb.in_cloud(ledger, photos, a), True)
write(photos / "night1" / "b.jpg", b"bbbbbb-changed")
check("a photo changed since its upload is not confirmed", cb.in_cloud(ledger, photos, photos / "night1" / "b.jpg"), False)

write(usb / "night1" / "a.jpg", b"aaaa")
moved, waiting = cb.move_confirmed(ledger, photos, backedup, [usb], storage_low=False)
check("USB plugged in and has it: moves", (moved, (backedup / "night1" / "a.jpg").exists(), a.exists()), (1, True, False))
check("changed photo dropped from the ledger, to upload again", sorted(ledger), [])
check("unconfirmed and new photos stay in photos/", sorted(p.name for p in (photos / "night1").iterdir()), ["b.jpg", "c.jpg"])

d2 = write(photos / "night2" / "d.jpg", b"dd")
ledger = {"night2/d.jpg": cb.file_key(d2)}
moved, waiting = cb.move_confirmed(ledger, photos, backedup, [usb], storage_low=False)
check("on the server but not yet on the plugged-in USB drive: waits", (moved, waiting, d2.exists()), (0, 1, True))
moved, waiting = cb.move_confirmed(ledger, photos, backedup, [usb], storage_low=True)
check("...unless the SD card is low", (moved, d2.exists()), (1, False))
e = write(photos / "night2" / "e.jpg", b"e")
ledger = {"night2/e.jpg": cb.file_key(e)}
check("no USB drive: the server alone is enough", cb.move_confirmed(ledger, photos, backedup, [], storage_low=False), (1, 0))

# --- freeing space -------------------------------------------------------------------
for n in ("2026-11-01", "2026-11-02", "2026-11-03"):
    write(backedup / n / "p.jpg")
free = [0]
def fake_free():
    free[0] += 1
    return free[0]
cb.purge_backedup_until(backedup, target_free=3, free_fn=fake_free)
check("SD card low: the oldest backed-up nights go first, only until there is room",
      sorted(p.name for p in backedup.iterdir()), ["2026-11-03", "night1", "night2"])
check("photos/ is never purged", (photos / "night1" / "b.jpg").exists(), True)

# --- the interval -----------------------------------------------------------------------
cb.LAST_ATTEMPT = tmp / "last_cloud_upload.txt"
check("first upload is due", cb.attempt_due(60), True)
cb.LAST_ATTEMPT.write_text(str(time.time() - 30 * 60))
check("30 min after: not due at 60", (cb.attempt_due(60), cb.attempt_due(30)), (False, True))

# --- the whole upload, through real rclone to a local folder ------------------------------
if shutil.which("rclone"):
    import session
    box = tmp / "box"
    cb.DESKTOP, cb.PHOTOS, cb.BACKEDUP, cb.LOGS = box, box / "photos", box / "photos_backedup", box / "logs"
    cb.STATE_ROOT = box / "state"; cb.STATE_ROOT.mkdir(parents=True)
    cb.LEDGER, cb.STATUS_FILE = cb.STATE_ROOT / "cloud_confirmed.txt", cb.STATE_ROOT / "cloud_status.json"
    cb.LAST_ATTEMPT = cb.STATE_ROOT / "last.txt"
    cb.CLOUD_LOCK, cb.BACKUP_LOCK = str(tmp / "cloud.lock"), str(tmp / "backup.lock")
    cb.RCLONE_CONF_BOOT, cb.RCLONE_CONF = tmp / "boot_rclone.conf", cb.STATE_ROOT / "rclone.conf"
    cb.RCLONE_CONF_BOOT.write_text("[server]\ntype = local\n")
    server = tmp / "server"
    cb.box_mode = lambda: "HI_POW"
    cb.usb_backup_roots = lambda name: []
    cb.safety_bytes = lambda: 0
    session.read_state = lambda path=None: session.WAITING
    write(cb.PHOTOS / "2026-11-05" / "one.jpg", b"1" * 1000)
    write(cb.PHOTOS / "2026-11-05" / "two.jpg", b"2" * 2000)
    write(cb.PHOTOS / "2026-11-05" / "fresh.jpg", b"3", age_s=5)
    write(cb.LOGS / "Backup_log.txt", b"log")
    cfg = cb.cloud_config({"cloud_upload": "True", "cloud_service": "rclone_conf", "cloud_bucket": f"server:{server}"})
    code = cb.upload(cfg, "box", now_flag=True)
    up = server / "photos_backup_box" / "2026-11-05"
    check("rclone: uploaded, proven, then moved to photos_backedup",
          (code, sorted(p.name for p in up.iterdir()), sorted(p.name for p in (cb.BACKEDUP / "2026-11-05").iterdir())),
          (0, ["one.jpg", "two.jpg"], ["one.jpg", "two.jpg"]))
    check("rclone: the photo still being written stays in photos/",
          sorted(p.name for p in (cb.PHOTOS / "2026-11-05").iterdir()), ["fresh.jpg"])
    check("rclone: logs and a status file uploaded",
          ((server / "logs_box" / "Backup_log.txt").exists(), (server / "status_box.json").exists()), (True, True))
else:
    print("SKIP rclone not installed here (the real upload to a local folder)")

shutil.rmtree(tmp)
if failures:
    print(f"\n{failures} check(s) FAILED")
    sys.exit(1)
print("\nALL CHECKS PASSED")
