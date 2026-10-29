#!/usr/bin/env python3
"""
clone_shrink_sd.py -- on a Mac, turn a Mothbox SD card into a small .img that
grows itself to fill whatever card it is flashed onto. The Mac version of
"Win32 Disk Imager + pishrink.sh".

    sudo python3 imaging/clone_shrink_sd.py                    # pick the card from a list
    sudo python3 imaging/clone_shrink_sd.py --disk disk4 --out ~/Desktop/mothbox_6.0.4.img
    python3 imaging/clone_shrink_sd.py --image old.img         # shrink an image you already have

Needs e2fsprogs from Homebrew:   brew install e2fsprogs

Safety
------
* The SD card is only ever READ. Nothing this script runs is given a /dev
  path to write to; every write goes to files next to the output image.
* Only external, physical disks laid out like a Raspberry Pi card (MBR, a FAT
  boot partition, a Linux root partition) are offered, and you must type the
  disk's name to confirm.
* The image is built as <out>.partial (+ <out>.root while working) and only
  renamed to <out> when every step, including a byte-for-byte check of the
  finished image, has passed. On any failure both temp files are removed.
* When macOS pops up "The disk you inserted was not readable", click IGNORE.
  Never "Initialize" -- that is the one thing that would erase the card.

How it works
------------
1. Copy the partition table and boot partition byte for byte.
2. Copy the root (ext4) partition with `e2image -ra` into its own file. It
   reads only the blocks the filesystem is using and writes a sparse file, so
   a 128 GB card holding 9 GB needs ~9 GB of free space and reads ~9 GB, not
   128. Free space comes out as zeros, so deleted photos and logs never reach
   the image.
3. Do what pishrink.sh does, on that file: check the filesystem, install
   PiShrink's first-boot /etc/rc.local that expands the root filesystem
   (firstboot_guard and the "UNPACKING DISK" screen rely on this), and shrink
   the filesystem to its minimum plus a little room.
   The root filesystem is always worked on as a plain file. Pointing
   resize2fs at it inside the whole image with "img?offset=N" CORRUPTS it
   (Homebrew e2fsprogs 1.47.4: moved blocks land in the wrong place) -- found
   while testing this script, so never "simplify" it back to that.
4. Shrink the partition in the MBR and move the root file into the image from
   the back, cutting the root file shorter after each chunk, so this needs
   only one chunk of extra space. Then re-read and compare every chunk.
"""
import argparse
import hashlib
import math
import os
import plistlib
import re
import shutil
import struct
import subprocess
import sys
import tempfile

SECTOR = 512
E2FS_DIRS = ["/opt/homebrew/opt/e2fsprogs/sbin", "/usr/local/opt/e2fsprogs/sbin"]
FAT_CONTENT = {"DOS_FAT_32", "Windows_FAT_32", "DOS_FAT_16", "Windows_FAT_16", "DOS_FAT_12"}
MAX_CARD_BYTES = 1024 ** 4          # nothing bigger than 1 TB is an SD card we image
# Set to 1 only for tests: lets a disk image attached with hdiutil stand in for a card
ALLOW_VIRTUAL = os.environ.get("MOTHBOX_IMAGING_ALLOW_VIRTUAL") == "1"

# --- PiShrink's first-boot expander ---------------------------------------
# Copied verbatim from pishrink.sh (MIT, https://github.com/Drewsif/PiShrink)
# so an image made here behaves exactly like one made with pishrink.sh. The
# marker line is how both tools recognise an image that already has it.
PISHRINK_MARKER = "## PiShrink https://github.com/Drewsif/PiShrink ##"
RC_LOCAL = r'''#!/bin/bash
## PiShrink https://github.com/Drewsif/PiShrink ##
do_expand_rootfs() {
  ROOT_PART=$(mount | sed -n 's|^/dev/\(.*\) on / .*|\1|p')

  PART_NUM=${ROOT_PART#mmcblk0p}
  if [ "$PART_NUM" = "$ROOT_PART" ]; then
    echo "$ROOT_PART is not an SD card. Don't know how to expand"
    return 0
  fi

  # Get the starting offset of the root partition
  PART_START=$(parted /dev/mmcblk0 -ms unit s p | grep "^${PART_NUM}" | cut -f 2 -d: | sed 's/[^0-9]//g')
  [ "$PART_START" ] || return 1
  # Return value will likely be error for fdisk as it fails to reload the
  # partition table because the root fs is mounted
  fdisk /dev/mmcblk0 <<EOF
p
d
$PART_NUM
n
p
$PART_NUM
$PART_START

p
w
EOF

cat <<EOF > /etc/rc.local &&
#!/bin/sh
echo "Expanding /dev/$ROOT_PART"
resize2fs /dev/$ROOT_PART
rm -f /etc/rc.local; cp -fp /etc/rc.local.bak /etc/rc.local && /etc/rc.local

EOF
reboot
exit
}
raspi_config_expand() {
/usr/bin/env raspi-config --expand-rootfs
if [[ $? != 0 ]]; then
  return -1
else
  rm -f /etc/rc.local; cp -fp /etc/rc.local.bak /etc/rc.local && /etc/rc.local
  reboot
  exit
fi
}
raspi_config_expand
echo "WARNING: Using backup expand..."
sleep 5
do_expand_rootfs
echo "ERROR: Expanding failed..."
sleep 5
if [[ -f /etc/rc.local.bak ]]; then
  cp -fp /etc/rc.local.bak /etc/rc.local
  /etc/rc.local
fi
exit 0
'''


class Abort(Exception):
    pass


def say(msg=""):
    print(msg, flush=True)


def step(msg):
    say(f"\n=== {msg} ===")


def run(cmd, check=True, capture=True, ok_codes=(0,)):
    """Run a command; return CompletedProcess. Raises Abort on an unexpected exit code."""
    p = subprocess.run(cmd, text=True, capture_output=capture)
    if check and p.returncode not in ok_codes:
        out = ((p.stdout or "") + (p.stderr or "")).strip()
        raise Abort(f"{' '.join(cmd)} failed (exit {p.returncode}):\n{out}")
    return p


def e2fs_tool(name):
    for d in E2FS_DIRS:
        p = os.path.join(d, name)
        if os.access(p, os.X_OK):
            return p
    raise Abort("e2fsprogs not found. Install it with:  brew install e2fsprogs")


# ---------------------------------------------------------------------------
# MBR
# ---------------------------------------------------------------------------
def read_mbr(f):
    """Partitions from a 512-byte MBR: [(index, type, start_sector, n_sectors)]."""
    f.seek(0)
    mbr = f.read(SECTOR)
    if len(mbr) < SECTOR or mbr[510:512] != b"\x55\xaa":
        raise Abort("no MBR partition table (is this a Raspberry Pi card/image?)")
    parts = []
    for i in range(4):
        e = mbr[446 + 16 * i: 446 + 16 * (i + 1)]
        ptype = e[4]
        start, count = struct.unpack("<II", e[8:16])
        if ptype:
            parts.append((i, ptype, start, count))
    return parts


def root_partition(parts):
    """The Pi layout: last partition is Linux (0x83), no GPT, no extended partitions."""
    if any(t == 0xEE for _, t, _, _ in parts):
        raise Abort("GPT disk -- Raspberry Pi OS cards use MBR; refusing")
    if any(t in (0x05, 0x0F, 0x85) for _, t, _, _ in parts):
        raise Abort("extended/logical partitions (NOOBS?) are not supported")
    if len(parts) < 2:
        raise Abort(f"expected a boot and a root partition, found {len(parts)}")
    last = max(parts, key=lambda p: p[2])
    if last[1] != 0x83:
        raise Abort(f"last partition is type {last[1]:#x}, not Linux (0x83)")
    return last


def set_partition_sectors(path, index, n_sectors):
    with open(path, "r+b") as f:
        f.seek(446 + 16 * index + 12)
        f.write(struct.pack("<I", n_sectors))


# ---------------------------------------------------------------------------
# Choosing the card
# ---------------------------------------------------------------------------
def plist(cmd):
    return plistlib.loads(subprocess.run(cmd, capture_output=True, check=True).stdout)


def describe_disk(ident):
    """dict about a whole disk, or None if it doesn't look like a Pi card."""
    info = plist(["diskutil", "info", "-plist", ident])
    layout = plist(["diskutil", "list", "-plist", ident])
    parts = layout["AllDisksAndPartitions"][0].get("Partitions", [])
    size = info.get("TotalSize") or info.get("Size") or 0
    why_not = None
    if info.get("Internal"):
        why_not = "internal disk"
    elif info.get("VirtualOrPhysical") != "Physical" and not ALLOW_VIRTUAL:
        why_not = "not a physical disk"
    elif size > MAX_CARD_BYTES:
        why_not = "bigger than 1 TB"
    elif info.get("Content") != "FDisk_partition_scheme":
        why_not = "not an MBR disk"
    elif len(parts) < 2 or parts[0].get("Content") not in FAT_CONTENT \
            or parts[-1].get("Content") != "Linux":
        why_not = "not laid out like a Raspberry Pi card (FAT boot + Linux root)"
    return dict(ident=ident, size=size, name=info.get("MediaName", "?"),
                parts=parts, why_not=why_not)


def choose_disk(arg_disk):
    if arg_disk:
        idents = [arg_disk.replace("/dev/", "").replace("rdisk", "disk")]
    else:
        idents = plist(["diskutil", "list", "-plist", "external", "physical"]).get("WholeDisks", [])
    disks = [describe_disk(i) for i in idents]
    good = [d for d in disks if not d["why_not"]]
    for d in disks:
        if d["why_not"]:
            say(f"  skipping /dev/{d['ident']} ({d['name']}): {d['why_not']}")
    if not good:
        raise Abort("no Raspberry Pi SD card found. Is the card reader plugged in?")
    say("\nRaspberry Pi card(s) found:")
    for d in good:
        say(f"  /dev/{d['ident']}  {d['size'] / 1e9:.1f} GB  {d['name']}")
        for p in d["parts"]:
            say(f"      {p.get('DeviceIdentifier')}  {p.get('Content'):<16} "
                f"{p.get('VolumeName', ''):<10} {p.get('Size', 0) / 1e9:.1f} GB")
    say("\nThis card will only be READ. Type its name (e.g. disk4) to continue, anything else to stop.")
    answer = input("> ").strip().replace("/dev/", "")
    match = [d for d in good if d["ident"] == answer]
    if not match:
        raise Abort("stopped -- nothing was read or written")
    return match[0]


# ---------------------------------------------------------------------------
# Cloning
# ---------------------------------------------------------------------------
CHUNK = 8 * 1024 * 1024


def copy_range(src, src_off, dst, dst_off, n_bytes, label):
    """Forward copy of n_bytes; all-zero chunks are skipped (left sparse)."""
    done = 0
    src.seek(src_off)
    while done < n_bytes:
        buf = src.read(min(CHUNK, n_bytes - done))
        if not buf:
            raise Abort(f"{label}: read stopped early at {done} of {n_bytes} bytes")
        if buf.count(0) != len(buf):
            dst.seek(dst_off + done)
            dst.write(buf)
        done += len(buf)
        print(f"\r  {label}: {done / 1e6:7.0f} / {n_bytes / 1e6:.0f} MB", end="", flush=True)
    print()


def fs_info(target):
    """Block size / count / free blocks from the ext4 superblock."""
    # tune2fs -l only lists the superblock. (Homebrew's dumpe2fs -h can't parse
    # its own options on macOS: "Unable to resolve '-h'".)
    out = run([e2fs_tool("tune2fs"), "-l", target]).stdout
    def field(name):
        m = re.search(rf"^{name}:\s+(\d+)", out, re.M)
        if not m:
            raise Abort(f"tune2fs did not report '{name}' -- not an ext4 filesystem?")
        return int(m.group(1))
    return field("Block size"), field("Block count"), field("Free blocks")


def check_space(where, head_bytes, root_target):
    bs, blocks, free = fs_info(root_target)
    used = (blocks - free) * bs
    need = head_bytes + used * 1.05 + 2 * CHUNK + 500e6
    have = shutil.disk_usage(os.path.dirname(os.path.abspath(where))).free
    say(f"  root filesystem: {blocks * bs / 1e9:.1f} GB, {used / 1e9:.1f} GB used")
    if have < need:
        raise Abort(f"not enough free space: need ~{need / 1e9:.1f} GB, "
                    f"have {have / 1e9:.1f} GB where the image is going")


def clone(disk, partial, root):
    """Card -> partial (MBR + boot) and root (the ext4 filesystem, sparse)."""
    dev = f"/dev/{disk['ident']}"
    raw = f"/dev/r{disk['ident']}"
    if not os.access(raw, os.R_OK):
        raise Abort(f"cannot read {raw} -- run this script with sudo")

    step(f"Unmounting {dev} (the card is only read from here on)")
    run(["diskutil", "unmountDisk", dev])

    with open(raw, "rb") as card:
        parts = read_mbr(card)
    index, _, start, count = root_partition(parts)
    root_off = start * SECTOR
    root_dev = f"/dev/r{disk['ident']}s{index + 1}"
    check_space(partial, root_off, root_dev)

    step("Copying partition table + boot partition")
    with open(raw, "rb") as card, open(partial, "wb") as img:
        copy_range(card, 0, img, 0, root_off, "boot")
        img.truncate(root_off)

    step("Copying the root filesystem (used blocks only)")
    run([e2fs_tool("e2image"), "-ra", "-p", root_dev, root], capture=False)
    return index, root_off, count


def extract(src, partial, root):
    """An existing image -> partial (APFS clone cut to MBR + boot) and root."""
    with open(src, "rb") as f:
        parts = read_mbr(f)
    index, _, start, count = root_partition(parts)
    root_off = start * SECTOR
    check_space(partial, 0, f"{src}?offset={root_off}")    # read-only use of ?offset is fine

    step("Copying the image")
    # cp -c is an APFS clone: instant and takes no space until changed
    if run(["cp", "-c", src, partial], check=False).returncode != 0:
        raise Abort("could not make an APFS clone of the image (is --out on a different "
                    "volume?). Put the output next to the original")
    with open(partial, "r+b") as img:
        img.truncate(root_off)

    step("Copying out the root filesystem (used blocks only)")
    # Same as from a card: only used blocks, so deleted data in free space is
    # left behind. -o is e2image's own source offset (read-only, safe).
    run([e2fs_tool("e2image"), "-ra", "-p", "-o", str(root_off), src, root], capture=False)
    return index, root_off, count


def fsck(target, label):
    """e2fsck -fp; if it needs more, -fy. Returns True if anything was fixed."""
    p = run([e2fs_tool("e2fsck"), "-f", "-p", target], check=False)
    if p.returncode == 0:
        say(f"  {label}: clean")
        return False
    if p.returncode == 1:
        say(f"  {label}: errors were found and FIXED in the image:\n{p.stdout}{p.stderr}")
        return True
    say(f"  {label}: e2fsck -p could not fix everything, retrying with -y")
    p = run([e2fs_tool("e2fsck"), "-f", "-y", target], check=False)
    if p.returncode >= 4:
        raise Abort(f"the root filesystem is damaged beyond automatic repair:\n{p.stdout}{p.stderr}")
    say(f"  {label}: errors FIXED in the image:\n{p.stdout}")
    return True


def debugfs(target, *commands, write=False):
    with tempfile.NamedTemporaryFile("w", suffix=".debugfs", delete=False) as f:
        f.write("\n".join(commands) + "\n")
        script = f.name
    try:
        cmd = [e2fs_tool("debugfs")] + (["-w"] if write else []) + ["-f", script, target]
        p = run(cmd)
        return p.stdout + p.stderr
    finally:
        os.remove(script)


def set_autoexpand(target):
    """Install PiShrink's first-boot rc.local (keeping the original as rc.local.bak)."""
    current = run([e2fs_tool("debugfs"), "-R", "cat /etc/rc.local", target], check=False)
    has_rc = "File not found" not in current.stderr and current.returncode == 0
    if has_rc and PISHRINK_MARKER in current.stdout:
        say("  first-boot expansion already installed")
        return
    with tempfile.NamedTemporaryFile("w", suffix=".rc", delete=False) as f:
        f.write(RC_LOCAL)
        payload = f.name
    try:
        cmds = []
        if has_rc:
            have_bak = run([e2fs_tool("debugfs"), "-R", "stat /etc/rc.local.bak", target],
                           check=False)
            if "File not found" not in have_bak.stderr:
                cmds.append("rm /etc/rc.local.bak")
            # debugfs rename: link the inode under the new name, drop the old name
            cmds += ["ln /etc/rc.local /etc/rc.local.bak", "unlink /etc/rc.local"]
        else:
            say("  WARNING: the image has no /etc/rc.local -- autoexpand may fail (same as pishrink)")
        cmds += [f"write {payload} /etc/rc.local",
                 "sif /etc/rc.local mode 0100755",
                 "sif /etc/rc.local uid 0",
                 "sif /etc/rc.local gid 0"]
        debugfs(target, *cmds, write=True)
    finally:
        os.remove(payload)
    check = run([e2fs_tool("debugfs"), "-R", "cat /etc/rc.local", target]).stdout
    if PISHRINK_MARKER not in check:
        raise Abort("writing the first-boot /etc/rc.local did not take")
    say("  first-boot expansion installed (/etc/rc.local, original kept as rc.local.bak)")


def shrink_root(root, autoexpand=True):
    """pishrink.sh's filesystem steps, on the root filesystem as a plain file.
    Returns the new filesystem size in bytes."""
    step("Checking the root filesystem")
    fsck(root, "e2fsck")

    if autoexpand:
        step("Making it expand to fill the card on first boot")
        set_autoexpand(root)

    step("Shrinking the filesystem")
    bs, current, _ = fs_info(root)
    out = run([e2fs_tool("resize2fs"), "-P", root]).stdout
    m = re.search(r"minimum size of the filesystem:\s*(\d+)", out)
    if not m:
        raise Abort(f"resize2fs -P gave no minimum size:\n{out}")
    minsize = int(m.group(1))
    extra = current - minsize
    for room in (5000, 1000, 100):                     # pishrink's breathing room
        if extra > room:
            minsize += room
            break
    if minsize < current:
        run([e2fs_tool("resize2fs"), "-p", root, str(minsize)], capture=False)
    else:
        say("  already at its minimum size")
        minsize = current

    step("Checking the shrunk filesystem (read-only)")
    p = run([e2fs_tool("e2fsck"), "-f", "-n", root], check=False)
    if p.returncode != 0:
        raise Abort(f"the shrunk filesystem does not check clean:\n{p.stdout}{p.stderr}")
    say("  clean")
    return minsize * bs


def finish(partial, root, index, root_off, fs_bytes):
    """Shrink the partition entry, then move root into partial back to front."""
    step("Building the image")
    sectors = math.ceil(fs_bytes / SECTOR)
    size = sectors * SECTOR
    set_partition_sectors(partial, index, sectors)
    with open(root, "r+b") as r:
        r.truncate(size)                               # nothing past the shrunk fs
    hashes = {}
    with open(root, "r+b") as r, open(partial, "r+b") as img:
        img.truncate(root_off + size)                  # sparse until written
        pos = (size - 1) // CHUNK * CHUNK
        while pos >= 0:
            r.seek(pos)
            buf = r.read(min(CHUNK, size - pos))
            hashes[pos] = hashlib.sha256(buf).digest()
            if buf.count(0) != len(buf):
                img.seek(root_off + pos)
                img.write(buf)
            img.flush()
            os.fsync(img.fileno())                     # on disk before the source shrinks
            r.truncate(pos)
            print(f"\r  {(size - pos) / 1e6:7.0f} / {size / 1e6:.0f} MB", end="", flush=True)
            pos -= CHUNK
    print()
    os.remove(root)

    step("Verifying the image byte for byte")
    with open(partial, "rb") as img:
        _, _, _, written = root_partition(read_mbr(img))
        if written != sectors:
            raise Abort("partition table update did not stick")
        for pos, h in sorted(hashes.items()):
            img.seek(root_off + pos)
            if hashlib.sha256(img.read(min(CHUNK, size - pos))).digest() != h:
                raise Abort(f"image differs from the checked filesystem at byte {root_off + pos}")
    say("  identical to the checked filesystem")


def give_back(path):
    """Under sudo, hand the file to the user who ran sudo."""
    uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    if os.geteuid() == 0 and uid and gid:
        os.chown(path, int(uid), int(gid))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--disk", help="the card, e.g. disk4 (default: choose from a list)")
    ap.add_argument("--out", help="image to write (default: ~/Desktop/mothbox.img, or "
                                  "<name>_shrunk.img next to --image)")
    ap.add_argument("--image", help="shrink this existing .img instead of reading a card "
                                    "(the original is left untouched)")
    ap.add_argument("--no-autoexpand", action="store_true",
                    help="don't install the first-boot expansion (pishrink -s)")
    a = ap.parse_args()

    for t in ("e2fsck", "resize2fs", "e2image", "debugfs", "tune2fs"):
        e2fs_tool(t)

    if a.image:
        src = os.path.abspath(os.path.expanduser(a.image))
        default = re.sub(r"(\.img)?$", "_shrunk.img", src, count=1)
        disk = None
    else:
        disk = choose_disk(a.disk)
        home = os.path.expanduser(f"~{os.environ.get('SUDO_USER', '')}")
        default = os.path.join(home, "Desktop", "mothbox.img")
    out = os.path.abspath(os.path.expanduser(a.out or default))
    if out.startswith("/dev/"):
        raise Abort("--out must be a file, not a device")
    partial, root = out + ".partial", out + ".root"
    for p in (out, partial, root):
        if os.path.exists(p):
            raise Abort(f"{p} already exists -- move it or pick another --out")

    try:
        if disk:
            index, root_off, _ = clone(disk, partial, root)
        else:
            index, root_off, _ = extract(src, partial, root)
        fs_bytes = shrink_root(root, autoexpand=not a.no_autoexpand)
        finish(partial, root, index, root_off, fs_bytes)
        os.rename(partial, out)
    except BaseException:
        for p in (partial, root):                      # only ever our own temp files
            if os.path.exists(p):
                os.remove(p)
        raise
    give_back(out)
    say(f"\nDone: {out}  ({os.path.getsize(out) / 1e9:.2f} GB)")
    if disk:
        say("Flash it with Raspberry Pi Imager (Choose OS -> Use custom) or balenaEtcher.")
        say(f"The card in /dev/{disk['ident']} was not modified; eject it from Finder or with:"
            f"  diskutil eject /dev/{disk['ident']}")


if __name__ == "__main__":
    try:
        main()
    except Abort as e:
        say(f"\nSTOPPED: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        say("\nInterrupted -- the card was not modified.")
        sys.exit(130)
