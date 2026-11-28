#!/usr/bin/python3
"""
Cloud_Backup.py -- upload new photos to a server with rclone.

The cloud is a second backup, next to the USB drive, and it follows the same
rule: fresh photos stay in photos/ until a backup is PROVEN to have them, and
only then move to photos_backedup/. photos_backedup/ keeps them as long as
possible; only when the SD card runs low are its oldest nights deleted to keep
the box going. photos/ itself is never deleted from.

When a photo may leave photos/ (may_leave_photos):
  - every backup that is switched on must have it: the USB drive when one is
    plugged in, the cloud when cloud upload is on;
  - except when the SD card is low: then one confirmed copy is enough, so a
    dead internet connection or a full USB drive cannot stop the box.

When it uploads (all must hold):
  - cloud_upload is True in mothbox_settings.csv;
  - the box is in HI POWER mode (the HI switch, or hi_power_always): the only
    mode that runs wifi and stays awake;
  - cloud_when allows it: between_windows (default) uploads while the box
    waits, dark, between capture windows; anytime also uploads during them;
  - cloud_interval minutes have passed since the last attempt.

It is started by Backup_Files.py (which cron already runs), as its own
process with its own lock, so a slow upload never holds up the USB backup.
The connection comes from the cloud_* settings (FTP, FTPS, SFTP, WebDAV, S3),
or from an rclone.conf copied next to mothbox_settings.csv (any service
rclone supports, e.g. Google Drive or Dropbox).

By hand:
    python3 Cloud_Backup.py --test   check the connection and upload one small
                                     test file (any mode, needs internet)
    python3 Cloud_Backup.py --now    upload now, ignoring the interval
"""
import csv
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

DESKTOP = Path("/home/pi/Desktop/Mothbox")
PHOTOS = DESKTOP / "photos"
BACKEDUP = DESKTOP / "photos_backedup"
LOGS = DESKTOP / "logs"
LOG_FILE = LOGS / "Cloud_log.txt"

try:
    from mothbox_paths import STATE_ROOT, SETTINGS_CSV, BOOT_CUSTOM, LEGACY_ROOT, resolve_read, ensure_state_dir
except Exception:                       # module missing -> old layout, still runs
    BOOT_CUSTOM = Path("/boot/firmware/mothbox_custom")
    LEGACY_ROOT = BOOT_CUSTOM / "system" / "controls"
    STATE_ROOT = LEGACY_ROOT
    SETTINGS_CSV = BOOT_CUSTOM / "mothbox_settings.csv"

    def resolve_read(p):
        return p

    def ensure_state_dir():
        return STATE_ROOT

LEDGER = STATE_ROOT / "cloud_confirmed.txt"       # photos the server is proven to have
LAST_ATTEMPT = STATE_ROOT / "last_cloud_upload.txt"
STATUS_FILE = STATE_ROOT / "cloud_status.json"
RCLONE_CONF_BOOT = BOOT_CUSTOM / "rclone.conf"    # where people put it
RCLONE_CONF = STATE_ROOT / "rclone.conf"          # the copy rclone uses (it may rewrite tokens; never on FAT)
CLOUD_LOCK = "/run/mothbox_cloud.lock"
BACKUP_LOCK = "/run/mothbox_backup.lock"          # Backup_Files.py's; held only while moving photos

MIN_AGE_S = 120               # a photo this new may still be being written
INTERVAL_DEFAULT, INTERVAL_MIN, INTERVAL_MAX = 60, 10, 1440
REMOTE_NAME = "MOTHBOX"       # the remote built from the cloud_* settings
TRUE_WORDS = ("true", "1", "yes", "on")


def log(msg):
    print(f"[Cloud {datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
def read_settings(path=None):
    """SETTING -> VALUE from mothbox_settings.csv. Missing or unreadable file -> {}."""
    path = Path(SETTINGS_CSV if path is None else path)
    if not path.exists():
        return {}
    for enc in ("utf-8-sig", "latin-1"):
        try:
            with open(path, newline="", encoding=enc) as f:
                reader = csv.DictReader(f)
                if not reader.fieldnames:
                    return {}
                reader.fieldnames = [h.strip().lstrip("﻿").upper() for h in reader.fieldnames]
                if "SETTING" not in reader.fieldnames or "VALUE" not in reader.fieldnames:
                    return {}
                out = {}
                for row in reader:
                    key = (row.get("SETTING") or "").strip().lstrip("﻿")
                    if key:
                        out[key] = (row.get("VALUE") or "").strip()
                return out
        except UnicodeDecodeError:
            continue
        except OSError as e:
            log(f"could not read the settings ({e})")
            return {}
    return {}


def is_true(value):
    return str(value or "").strip().lower() in TRUE_WORDS


def normalize_service(value):
    """The cloud_service setting, from the editor or typed by hand, as one of
    ftp / ftps / sftp / webdav / s3 / rclone_conf. Anything else is returned
    as typed (and reported as unsupported)."""
    v = str(value or "").strip().lower()
    if v in ("ftp", "ftps", "sftp", "webdav", "s3", "rclone_conf"):
        return v
    if v in ("rclone", "rclone.conf", "rclone conf"):
        return "rclone_conf"
    if "s3" in v:
        return "s3"
    if v in ("nextcloud", "owncloud"):
        return "webdav"
    return v


def cloud_config(settings):
    try:
        interval = int(str(settings.get("cloud_interval", INTERVAL_DEFAULT)).strip() or INTERVAL_DEFAULT)
    except ValueError:
        interval = INTERVAL_DEFAULT
    interval = min(max(interval, INTERVAL_MIN), INTERVAL_MAX)
    when = str(settings.get("cloud_when", "")).strip().lower()
    return {
        "enabled": is_true(settings.get("cloud_upload")),
        "service": normalize_service(settings.get("cloud_service")),
        "url": settings.get("cloud_url", "").strip(),
        "folder": settings.get("cloud_bucket", "").strip().strip("/"),
        "region": settings.get("cloud_region", "").strip(),
        "user": settings.get("cloud_user", "").strip(),
        "password": settings.get("cloud_password", ""),
        "key": settings.get("cloud_api_key", "").strip(),
        "secret": settings.get("cloud_api_secret", "").strip(),
        "interval_min": interval,
        "when": "anytime" if when == "anytime" else "between_windows",
        "bwlimit": settings.get("cloud_bwlimit", "").strip(),
    }


def read_kv(path, key, default=None):
    """One key=value control file (name.txt, mode.txt, ...)."""
    path = resolve_read(Path(path))
    try:
        with open(path) as f:
            for line in f:
                if "=" in line:
                    k, v = line.split("=", 1)
                    if k.strip() == key:
                        return v.strip()
    except OSError:
        pass
    return default


def box_name():
    return read_kv(STATE_ROOT / "name.txt", "name", "errorname")


def box_mode():
    return read_kv(STATE_ROOT / "mode.txt", "mode", "")


def safety_bytes():
    try:
        gb = int(read_kv(LEGACY_ROOT / "safetygb.txt", "safetygb", 9))
    except (TypeError, ValueError):
        gb = 9
    return gb * 1024 ** 3


def cloud_needed(settings=None, mode=None):
    """Is the cloud one of this box's backups right now? Then photos wait in
    photos/ for it as well as for the USB drive. Only in HI POWER: in any other
    mode nothing would ever upload them."""
    settings = read_settings() if settings is None else settings
    mode = box_mode() if mode is None else mode
    return is_true(settings.get("cloud_upload")) and mode == "HI_POW"


# ---------------------------------------------------------------------------
# The rule: when a photo may leave photos/
# ---------------------------------------------------------------------------
def may_leave_photos(cloud_needed, in_cloud, usb_needed, on_usb, storage_low):
    """A photo moves from photos/ to photos_backedup/ (where it can later be
    deleted to free space) once every backup that is switched on has it. When
    the SD card is low, one confirmed copy is enough, so the box keeps going."""
    have = []
    if cloud_needed:
        have.append(bool(in_cloud))
    if usb_needed:
        have.append(bool(on_usb))
    if not have:
        return False            # no backup at all: it stays where it is
    return all(have) or (storage_low and any(have))


# ---------------------------------------------------------------------------
# The ledger: photos the server is proven to have
#   one line per photo: <path relative to photos/> TAB <size> TAB <mtime>
# A photo only counts as confirmed while its size and time still match.
# ---------------------------------------------------------------------------
def file_key(path):
    st = os.stat(path)
    return st.st_size, int(st.st_mtime)


def read_ledger(path=None):
    path = LEDGER if path is None else path
    out = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                parts = line.rstrip("\n").split("\t")
                if len(parts) == 3:
                    try:
                        out[parts[0]] = (int(parts[1]), int(parts[2]))
                    except ValueError:
                        continue
    except OSError:
        pass
    return out


def write_ledger(entries, path=None):
    path = LEDGER if path is None else path
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for rel in sorted(entries):
            size, mtime = entries[rel]
            f.write(f"{rel}\t{size}\t{mtime}\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def in_cloud(ledger, photos_root, file_path):
    """Is this photo (an absolute path under photos_root) confirmed on the server?"""
    rel = os.path.relpath(str(file_path), str(photos_root))
    want = ledger.get(rel)
    if want is None:
        return False
    try:
        return file_key(file_path) == want
    except OSError:
        return False


def ready_photos(photos_root, now=None, min_age_s=MIN_AGE_S):
    """{relative path: (size, mtime)} for photos old enough to be finished."""
    now = time.time() if now is None else now
    out = {}
    for root, _dirs, files in os.walk(str(photos_root)):
        for name in files:
            p = os.path.join(root, name)
            try:
                size, mtime = file_key(p)
            except OSError:
                continue
            if now - mtime >= min_age_s:
                out[os.path.relpath(p, str(photos_root))] = (size, mtime)
    return out


# ---------------------------------------------------------------------------
# Where it uploads to
# ---------------------------------------------------------------------------
def split_host(url):
    """'sftp://u123.your-storagebox.de:23/x' -> ('u123.your-storagebox.de', '23')."""
    u = url.strip()
    if "://" in u:
        u = u.split("://", 1)[1]
    u = u.split("/", 1)[0]
    host, port = u, ""
    if ":" in u:
        h, p = u.rsplit(":", 1)
        if p.isdigit():
            host, port = h, p
    return host, port


def first_remote_in_conf(text):
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]") and len(line) > 2:
            return line[1:-1].strip()
    return None


def rjoin(root, *parts):
    """Join rclone remote paths: 'MOTHBOX:' + 'a' -> 'MOTHBOX:a'; 'MOTHBOX:x' + 'a' -> 'MOTHBOX:x/a'."""
    out = root
    for p in parts:
        out = out + p if out.endswith(":") or out.endswith("/") else out + "/" + p
    return out


def remote_spec(cfg, obscure, conf_text=None):
    """
    (remote root, extra environment, problem). The environment defines the
    remote for rclone (RCLONE_CONFIG_<NAME>_<OPTION>), so no config file is
    written for the simple services. `obscure` turns a password into rclone's
    obscured form. `conf_text` is the rclone.conf to use for rclone_conf.
    """
    s = cfg["service"]
    env = {}
    prefix = f"RCLONE_CONFIG_{REMOTE_NAME}_"
    folder = cfg["folder"]

    if s == "rclone_conf":
        if conf_text is None:
            return None, {}, f"cloud_service is rclone_conf, but there is no rclone.conf next to mothbox_settings.csv"
        if ":" in folder:
            return folder, {}, None
        name = first_remote_in_conf(conf_text)
        if not name:
            return None, {}, "rclone.conf has no remote in it (run `rclone config` to make one)"
        return f"{name}:{folder}", {}, None

    if s in ("ftp", "ftps", "sftp"):
        host, port = split_host(cfg["url"])
        missing = [n for n, v in (("server (cloud_url)", host), ("user (cloud_user)", cfg["user"]),
                                   ("password (cloud_password)", cfg["password"])) if not v]
        if missing:
            return None, {}, f"{s} needs a " + ", ".join(missing)
        env[prefix + "TYPE"] = "sftp" if s == "sftp" else "ftp"
        env[prefix + "HOST"] = host
        env[prefix + "USER"] = cfg["user"]
        env[prefix + "PASS"] = obscure(cfg["password"])
        if port:
            env[prefix + "PORT"] = port
        if s == "ftps":
            env[prefix + "EXPLICIT_TLS"] = "true"
    elif s == "webdav":
        if not cfg["url"].lower().startswith(("http://", "https://")):
            return None, {}, "webdav needs the full address in cloud_url, starting with https://"
        env[prefix + "TYPE"] = "webdav"
        env[prefix + "URL"] = cfg["url"]
        env[prefix + "VENDOR"] = "other"
        if cfg["user"]:
            env[prefix + "USER"] = cfg["user"]
        if cfg["password"]:
            env[prefix + "PASS"] = obscure(cfg["password"])
    elif s == "s3":
        missing = [n for n, v in (("access key (cloud_api_key)", cfg["key"]), ("secret (cloud_api_secret)", cfg["secret"]),
                                   ("bucket (cloud_bucket)", folder)) if not v]
        if missing:
            return None, {}, "s3 needs a " + ", ".join(missing)
        env[prefix + "TYPE"] = "s3"
        env[prefix + "PROVIDER"] = "Other" if cfg["url"] else "AWS"
        env[prefix + "ACCESS_KEY_ID"] = cfg["key"]
        env[prefix + "SECRET_ACCESS_KEY"] = cfg["secret"]
        if cfg["region"]:
            env[prefix + "REGION"] = cfg["region"]
        if cfg["url"]:
            env[prefix + "ENDPOINT"] = cfg["url"]
    elif s == "":
        return None, {}, "cloud_service is empty: choose ftp, ftps, sftp, webdav, s3 or rclone_conf"
    else:
        return None, {}, (f"cloud_service '{s}' is not supported directly. Use ftp, ftps, sftp, webdav or s3, "
                          f"or make an rclone.conf for it and set cloud_service to rclone_conf")
    return f"{REMOTE_NAME}:{folder}", env, None


def sync_rclone_conf():
    """Copy rclone.conf from the boot partition to the state folder when it
    changed, and return its text (None if there is none). rclone rewrites the
    file when it refreshes a login token, and that must never happen on FAT."""
    try:
        boot = RCLONE_CONF_BOOT.read_bytes()
    except OSError:
        return None
    try:
        current = RCLONE_CONF.read_bytes()
    except OSError:
        current = None
    if current is None or (boot != current and RCLONE_CONF_BOOT.stat().st_mtime > RCLONE_CONF.stat().st_mtime):
        ensure_state_dir()
        tmp = str(RCLONE_CONF) + ".tmp"
        with open(tmp, "wb") as f:
            f.write(boot)
        os.chmod(tmp, 0o600)
        os.replace(tmp, RCLONE_CONF)
        current = boot
    return current.decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# Running rclone
# ---------------------------------------------------------------------------
def rclone_obscure(password):
    r = subprocess.run(["rclone", "obscure", "-"], input=password, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f"rclone obscure failed: {r.stderr.strip()}")
    return r.stdout.strip()


def common_flags(cfg):
    flags = ["--contimeout", "20s", "--timeout", "2m", "--retries", "3", "--low-level-retries", "10",
             "--stats", "5m", "--stats-one-line", "--stats-log-level", "NOTICE"]
    if cfg.get("bwlimit"):
        flags += ["--bwlimit", cfg["bwlimit"]]
    return flags


def low_priority(cmd):
    """Run behind the camera: lowest CPU and disk priority, where available."""
    pre = []
    if shutil.which("ionice"):
        pre += ["ionice", "-c3"]
    if shutil.which("nice"):
        pre += ["nice", "-n", "19"]
    return pre + cmd


def run_rclone(args, env, should_stop=None, timeout_s=6 * 3600):
    """Run one rclone command, its output going to our log. Stops it early when
    should_stop() says so (a capture window opening). Returns the exit code,
    or None if it was stopped."""
    full_env = dict(os.environ, **env)
    p = subprocess.Popen(low_priority(["rclone"] + args), env=full_env, stdin=subprocess.DEVNULL)
    deadline = time.time() + timeout_s
    while True:
        try:
            return p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        if (should_stop and should_stop()) or time.time() > deadline:
            p.terminate()
            try:
                p.wait(timeout=30)
            except subprocess.TimeoutExpired:
                p.kill()
            return None


# ---------------------------------------------------------------------------
# Moving and freeing space
# ---------------------------------------------------------------------------
def free_bytes(path=None):
    try:
        return shutil.disk_usage(str(DESKTOP if path is None else path)).free
    except OSError:
        return 0


def usb_backup_roots(name):
    """The photos_backup_<name> folders on every USB drive that is plugged in."""
    roots = []
    try:
        mounts = os.listdir("/media")
    except OSError:
        return roots
    for m in mounts:
        if m == "pi" or "mmcblk" in m:
            continue
        p = Path("/media") / m
        if p.is_dir() and os.path.ismount(p):
            roots.append(p / f"photos_backup_{name}")
    return roots


def on_usb(usb_roots, rel, size):
    for root in usb_roots:
        try:
            if os.path.getsize(root / rel) == size:
                return True
        except OSError:
            continue
    return False


def move_confirmed(ledger, photos_root, backedup_root, usb_roots, storage_low):
    """Move the photos the server has from photos/ to photos_backedup/, if the
    USB drive (when one is plugged in) has them too. Returns (moved, waiting
    for USB) and drops moved or vanished photos from the ledger."""
    moved = waiting = 0
    for rel in sorted(ledger):
        src = os.path.join(str(photos_root), rel)
        if not os.path.exists(src):
            del ledger[rel]                 # Backup_Files moved it, or it is gone
            continue
        if not in_cloud(ledger, photos_root, src):
            del ledger[rel]                 # changed since it was uploaded: upload again
            continue
        size = ledger[rel][0]
        if not may_leave_photos(True, True, bool(usb_roots), on_usb(usb_roots, rel, size), storage_low):
            waiting += 1
            continue
        dest = os.path.join(str(backedup_root), rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.move(src, dest)
        del ledger[rel]
        moved += 1
    return moved, waiting


def remove_empty_dirs(folder):
    for root, dirs, _files in os.walk(str(folder), topdown=False):
        for d in dirs:
            try:
                os.rmdir(os.path.join(root, d))
            except OSError:
                pass


def dir_size(path):
    total = 0
    for root, _dirs, files in os.walk(str(path)):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def purge_backedup_until(folder, target_free, free_fn=None):
    """Delete the oldest nights in photos_backedup/ until the SD card has
    target_free bytes free. Everything in photos_backedup/ is confirmed on the
    USB drive or the server. Never touches photos/."""
    free_fn = free_bytes if free_fn is None else free_fn
    folder = Path(folder)
    if not folder.exists():
        return 0
    purged = 0
    for sub in sorted(d for d in folder.iterdir() if d.is_dir()):
        if free_fn() >= target_free:
            break
        size = dir_size(sub)
        shutil.rmtree(sub)
        purged += 1
        log(f"SD card low: deleted the oldest backed-up night {sub.name} ({size / 1e9:.2f} GB)")
    return purged


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------
def write_status(**fields):
    status = {"time": datetime.now().isoformat(timespec="seconds")}
    status.update(fields)
    try:
        ensure_state_dir()
        tmp = str(STATUS_FILE) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(status, f, indent=1)
        os.replace(tmp, STATUS_FILE)
    except OSError as e:
        log(f"could not write the status file ({e})")
    return status


def software_version():
    return read_kv(LEGACY_ROOT / "softwareversion.txt", "softwareversion", "")


# ---------------------------------------------------------------------------
# Starting it (from Backup_Files.py)
# ---------------------------------------------------------------------------
def attempt_due(interval_min, now=None):
    now = time.time() if now is None else now
    try:
        last = float(Path(LAST_ATTEMPT).read_text().strip())
    except (OSError, ValueError):
        return True
    return (now - last) / 60.0 >= interval_min - 0.55


def record_attempt():
    try:
        ensure_state_dir()
        tmp = str(LAST_ATTEMPT) + ".tmp"
        with open(tmp, "w") as f:
            f.write(str(time.time()))
        os.replace(tmp, LAST_ATTEMPT)
    except OSError as e:
        log(f"could not record the upload time ({e})")


def launch_if_enabled(now=False):
    """Called by Backup_Files.py on every run. Starts this script in the
    background when an upload could happen; costs nothing when it is off."""
    settings = read_settings()
    if not cloud_needed(settings):
        return False
    cfg = cloud_config(settings)
    if not now and not attempt_due(cfg["interval_min"]):
        return False
    try:
        import session
        if not when_allows(cfg["when"], session.read_state()):
            return False
    except Exception:
        pass                                # let the upload itself decide
    script = os.path.abspath(__file__)
    LOGS.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a") as out:
        subprocess.Popen(["python3", script] + (["--now"] if now else []),
                         stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         start_new_session=True)
    print("[Backup] cloud upload started in the background (see logs/Cloud_log.txt)")
    return True


# ---------------------------------------------------------------------------
# The upload
# ---------------------------------------------------------------------------
def when_allows(when, state):
    import session
    if when == "anytime":
        return state in (session.ACTIVE, session.WAITING)
    return state == session.WAITING


def take_lock(path, wait_s=0):
    import fcntl
    try:
        fh = open(path, "w")
    except OSError as e:
        log(f"could not open the lock {path} ({e}); continuing anyway")
        return True
    deadline = time.time() + wait_s
    while True:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fh
        except OSError:
            if time.time() >= deadline:
                fh.close()
                return None
            time.sleep(2)


def prepare_remote(cfg):
    if not shutil.which("rclone"):
        return None, None, "rclone is not installed (sudo apt install rclone)"
    conf_text = sync_rclone_conf() if cfg["service"] == "rclone_conf" else None
    try:
        root, env, problem = remote_spec(cfg, rclone_obscure, conf_text)
    except Exception as e:
        return None, None, str(e)
    if problem:
        return None, None, problem
    if cfg["service"] == "rclone_conf":
        env = dict(env, RCLONE_CONFIG=str(RCLONE_CONF))
    return root, env, None


def run_test(cfg, name):
    """--test: can the box reach the server and write to it?"""
    root, env, problem = prepare_remote(cfg)
    if problem:
        log(f"TEST FAILED: {problem}")
        return 1
    dest = rjoin(root, f"mothbox_test_{name}.txt")
    log(f"testing {cfg['service']} -> {root}")
    p = subprocess.run(["rclone", "rcat", dest] + common_flags(cfg), env=dict(os.environ, **env),
                       input=f"Mothbox {name} can upload here ({datetime.now():%Y-%m-%d %H:%M:%S}).\n", text=True)
    if p.returncode != 0:
        log(f"TEST FAILED: rclone exit {p.returncode} (see the lines above)")
        return 1
    log(f"TEST OK: wrote {dest}. You can delete that file.")
    return 0


def upload(cfg, name, now_flag=False):
    import session

    if not cfg["enabled"]:
        log("cloud_upload is off.")
        return 0
    mode = box_mode()
    if mode != "HI_POW":
        log(f"not uploading: the box is in {mode or 'an unknown'} mode; uploads need HI POWER "
            f"(the HI switch, or hi_power_always).")
        return 0
    state = session.read_state()
    if not when_allows(cfg["when"], state):
        log(f"not uploading now: session is '{state}', cloud_when is {cfg['when']}.")
        return 0
    if not now_flag and not attempt_due(cfg["interval_min"]):
        return 0
    lock = take_lock(CLOUD_LOCK)
    if lock is None:
        log("another upload is still running -- skipping this one.")
        return 0
    record_attempt()

    root, env, problem = prepare_remote(cfg)
    if problem:
        log(f"cannot upload: {problem}")
        write_status(ok=False, error=problem)
        return 1

    def window_opened():
        return cfg["when"] == "between_windows" and session.read_state() != session.WAITING

    flags = common_flags(cfg)
    photos_dest = rjoin(root, f"photos_backup_{name}")
    logs_dest = rjoin(root, f"logs_{name}")
    log(f"--- upload start: {cfg['service']} -> {photos_dest}")

    code = run_rclone(["mkdir", photos_dest] + flags, env, timeout_s=300)
    if code != 0:
        problem = "could not reach the server (no internet, or wrong settings)" if code is not None else "stopped"
        log(f"cannot upload: {problem}")
        write_status(ok=False, error=problem)
        return 1

    ledger = read_ledger()
    ready = ready_photos(PHOTOS)
    todo = {rel: key for rel, key in ready.items() if ledger.get(rel) != key}
    uploaded = 0
    stopped = False
    if todo:
        ensure_state_dir()
        list_path = STATE_ROOT / "cloud_upload_list.txt"
        match_path = STATE_ROOT / "cloud_upload_matched.txt"
        list_path.write_text("".join(rel + "\n" for rel in sorted(todo)), encoding="utf-8")
        log(f"{len(todo)} photo(s) to upload ({sum(k[0] for k in todo.values()) / 1e9:.2f} GB)")
        code = run_rclone(["copy", str(PHOTOS), photos_dest, "--files-from-raw", str(list_path),
                           "--no-traverse", "--transfers", "2", "--checkers", "4"] + flags,
                          env, should_stop=window_opened)
        if code is None:
            stopped = True
            log("a capture window opened (or the upload ran too long) -- stopped; it carries on next time.")
        elif code != 0:
            log(f"rclone copy exit {code}: some photos did not upload; they are tried again next time.")
        # Whatever happened, prove what arrived. Only proven photos may move.
        try:
            match_path.unlink()
        except OSError:
            pass
        run_rclone(["check", str(PHOTOS), photos_dest, "--one-way", "--files-from-raw", str(list_path),
                    "--match", str(match_path)] + flags, env, timeout_s=3600)
        try:
            matched = [l.strip("\n") for l in match_path.read_text(encoding="utf-8").splitlines() if l.strip()]
        except OSError:
            matched = []
        for rel in matched:
            if rel in todo:
                ledger[rel] = todo[rel]
                uploaded += 1
        write_ledger(ledger)
        log(f"{uploaded} of {len(todo)} photo(s) confirmed on the server.")
    else:
        log("no new photos to upload.")

    if not stopped:
        run_rclone(["copy", str(LOGS), logs_dest] + flags, env, timeout_s=1800)

    # Move what is proven, under Backup_Files.py's lock so the two never move the same photo.
    storage_low = free_bytes() < safety_bytes()
    backup_lock = take_lock(BACKUP_LOCK, wait_s=300)
    moved = waiting_usb = 0
    if backup_lock is None:
        log("the USB backup is busy -- moving confirmed photos next time.")
    else:
        moved, waiting_usb = move_confirmed(ledger, PHOTOS, BACKEDUP, usb_backup_roots(name), storage_low)
        write_ledger(ledger)
        remove_empty_dirs(PHOTOS)
        if moved:
            log(f"moved {moved} confirmed photo(s) to photos_backedup.")
        if waiting_usb:
            log(f"{waiting_usb} photo(s) are on the server but waiting for the USB backup too.")
        if free_bytes() < safety_bytes():
            purge_backedup_until(BACKEDUP, safety_bytes())

    remaining = len(ready_photos(PHOTOS, min_age_s=0))
    status = write_status(ok=not stopped, name=name, version=software_version(), uploaded=uploaded,
                          moved=moved, photos_waiting=remaining, free_gb=round(free_bytes() / 1e9, 2),
                          service=cfg["service"])
    p = subprocess.run(["rclone", "rcat", rjoin(root, f"status_{name}.json")] + flags,
                       env=dict(os.environ, **env), input=json.dumps(status, indent=1), text=True)
    if p.returncode != 0:
        log("could not upload the status file.")
    log("--- upload done")
    return 0


def main(argv):
    settings = read_settings()
    cfg = cloud_config(settings)
    name = box_name()
    if "--test" in argv:
        return run_test(cfg, name)
    return upload(cfg, name, now_flag="--now" in argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
