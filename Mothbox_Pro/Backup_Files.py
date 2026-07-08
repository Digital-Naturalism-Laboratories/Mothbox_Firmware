#!/usr/bin/python

"""
Backupper Script
This script is for folks collecting lots of data automatically that needs to get backed up at certain intervals
for instance saving a bunch of files to a folder, but then automatically copying them to larger external devices
This script first defines paths for the desktop, photos folder, and backup folder name. Then, it defines functions to:

    Get the storage information (total and available space) of a path.
    Find the sizes of all external devices in terms of total storage capacity (not available because this will change)
    Rank them in order  of their total storage capacity
    Check if the first option has space available to copy the new files
      if not, choose the next option in terms of total storage
    Copy all the files from the internal storage to the external storage
    Move the files from the directory of "fresh" files to the internal "backedup" folder
    if the internal storage gets too small, delete the internal "backedup" folder
Finally, the script checks if the photos folder exists and then finds the largest external storage. It compares the total space and available space on both the desktop and the external storage to determine if the external storage has enough space for the backup. If so, it creates a backup folder on the external storage and copies the photos. Otherwise, it informs the user about insufficient space.

Note:
    This script assumes the user running the script has read and write permissions to the desktop and any external storage devices.
    You might need to adjust the user name in desktop_path depending on your Raspberry Pi setup.
"""



import os
import sys
#######---- Check for Boot lock ------
BOOT_LOCK = "/run/boot_script_running"

if os.path.exists(BOOT_LOCK):
    sys.exit(0)

#-----------------------------##

import os
import subprocess

import shutil
import psutil
from pathlib import Path
from datetime import datetime
import sys
import time


from pathlib import Path

CONTROL_ROOT = Path("/boot/firmware/mothbox_custom/system/controls")

LAST_BACKUP_FILE = CONTROL_ROOT / "last_backup_time.txt"


def get_backup_interval():
    path = CONTROL_ROOT / "backup_interval.txt"
    if not path.exists():
        return 5
    try:
        with open(path) as f:
            for line in f:
                if line.startswith("backup_interval="):
                    return int(line.split("=", 1)[1].strip())
    except (ValueError, IOError):
        pass
    return 5


def is_backup_due():
    interval_mins = get_backup_interval()
    if not os.path.exists(LAST_BACKUP_FILE):
        return True
    try:
        with open(LAST_BACKUP_FILE) as f:
            last = float(f.read().strip())
        elapsed_mins = (time.time() - last) / 60.0
        threshold = interval_mins - 0.55
        print(f"Backup interval check: {elapsed_mins:.2f} min elapsed, threshold {threshold:.1f} min (interval={interval_mins})")
        return elapsed_mins >= threshold
    except (ValueError, IOError):
        return True


def record_backup_done():
    try:
        tmp = str(LAST_BACKUP_FILE) + ".tmp"
        with open(tmp, "w") as f:
            f.write(str(time.time()))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, LAST_BACKUP_FILE)
    except IOError as e:
        print(f"Warning: could not write last_backup_time: {e}")


def read_control(path: Path, key: str, default=None):
    """
    Reads a single key=value control file.
    Safe against missing, empty, or corrupted files.
    """
    if not path.exists():
        return default

    try:
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if not line or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() == key:
                    return v.strip()
    except Exception as e:
        print(f"⚠️ Warning: Failed reading {path}: {e}")

    return default


# ---- Load Controls ----

computerName = read_control(CONTROL_ROOT / "name.txt", "name", "errorname")
LastCalibration= float(read_control(CONTROL_ROOT / "lastcalibration.txt", "lastcalibration", 0))

internal_storage_minimum = int(
    read_control(CONTROL_ROOT / "safetygb.txt", "safetygb", 9)
)


# Define paths
desktop_path = Path(
    "/home/pi/Desktop/Mothbox"
)  # Assuming user is "pi" on your Raspberry Pi
photos_folder = desktop_path / "photos"
logs_folder = desktop_path / "logs"
backedup_photos_folder = desktop_path / "photos_backedup"

backup_folder_name = "photos_backup_"+computerName

if not is_backup_due():
    print(f"Backup not due yet (interval={get_backup_interval()} min). Skipping.")
    sys.exit(0)

print("----------------- STARTING BACKUP FILES-------------------")
now = datetime.now()
formatted_time = now.strftime("%Y-%m-%d %H:%M:%S")  # Adjust the format as needed

print(f"Current time: {formatted_time}")

def get_storage_info(path):
    """
    Gets the total and available storage space of a path.
    Args:
        path: The path to the storage device.

    Returns:
        A tuple containing the total and available storage in bytes.
    """
    try:
        stat = os.statvfs(path)
        return stat.f_blocks * stat.f_bsize, stat.f_bavail * stat.f_bsize
    except OSError:
        return 0, 0  # Handle non-existent or inaccessible storages

def find_largest_external_storage():
    largest_storage = None
    largest_size = 0

    # CHANGE: Look in /media instead of /media/pi
    for mount_point in os.listdir("/media"):
        path = Path(f"/media/{mount_point}")
        
        # SAFETY: Skip the 'pi' folder itself if it exists, and ignore the SD card
        if mount_point == "pi" or "mmcblk" in mount_point:
            continue

        if is_mounted(path) and path.is_dir():
            total_size, available_size = get_storage_info(path)
            
            # SAFETY: Ignore 0GB ghost drives
            if total_size == 0:
                continue

            if available_size > largest_size:
                largest_storage = path
                largest_size = available_size
                
    return largest_storage
    
    
def is_mounted(path):
  """
  Checks if the given path is currently mounted.
  Args:
      path: The path to check for mount status.

  Returns:
      True if the path is mounted, False otherwise.
  """
  # Use psutil library to check mounted devices
  partitions = psutil.disk_partitions()
  for partition in partitions:
    if partition.mountpoint == str(path):
      return True
  return False

def rsync_photos_to_backup(source_dir, dest_dir):
    if not os.path.exists(dest_dir):
        os.makedirs(dest_dir)
    #if you don't want as many slow print commands, turn off verbose mode
    rsync_cmd = ["rsync", "-avz", str(source_dir) + "/", dest_dir]
    # Call rsync using subprocess
    try:
        process = subprocess.run(rsync_cmd, check=True)
    except subprocess.CalledProcessError as err:
        raise RuntimeError(f"Oh no! Mothbox couldn't backup your files!") from err

def rsync_copy_and_delete_files(source_dir, dest_dir):
    """
    This function uses rsync to copy files from source_dir to dest_dir and then deletes the originals from source_dir if successful.
    Args:
      source_dir: The source directory containing the files to copy.
      dest_dir: The destination directory to copy the files to.

    Raises:
      subprocess.CalledProcessError: If the rsync command fails.
    """
    if not os.path.exists(dest_dir):
        os.makedirs(dest_dir)

    # Build the rsync command with options for recursive copy, delete on source, and verbose output    
    rsync_cmd = ["rsync", "-avz", str(source_dir) + "/", dest_dir]

    # Call rsync using subprocess
    process = subprocess.run(rsync_cmd, check=True)

    # If successful, iterate through copied files and delete them individually
    if process.returncode == 0:
        for root, _, files in os.walk(source_dir):
            for filename in files:
                source_file = os.path.join(root, filename)
                dest_file = os.path.join(dest_dir, filename)
                # Check if the file was successfully copied (exists in destination)
                if os.path.isfile(dest_file):
                    try:
                        os.remove(source_file)
                        #print(f"Deleted: {source_file}")
                    except OSError as e:
                        print(f"Error deleting {source_file}: {e}")

    return process.returncode
def move_folder_contents(source_folder, destination_folder):
  """
  Moves the entire contents of a folder to a new folder, overwriting existing files.
  Args:
      source_folder (str): Path to the source folder.
      destination_folder (str): Path to the destination folder.
  """
  print("moving folder contents")
  for filename in os.listdir(source_folder):
    source_path = os.path.join(source_folder, filename)
    destination_path = os.path.join(destination_folder, filename)

    if os.path.isfile(source_path):
      # Move the file, overwrite if exists
      shutil.move(source_path, destination_path)
    elif os.path.isdir(source_path):
      # Create destination directory if it doesn't exist
      os.makedirs(destination_path, exist_ok=True)
      os.chmod(destination_path, 0o777)
      # Recursively move contents of subfolders
      move_folder_contents(source_path, destination_path)
    else:
      print(f"Skipping unknown item: {filename}")

def move_photos_to_backup(source_folder, target_folder):
  """
  Copies all files and subfolders from the source folder to the target folder recursively,
  handling existing dated folders and copying their contents.
  Args:
      source_folder: The path to the source folder.
      target_folder: The path to the target folder.
  """
  if not os.path.exists(target_folder):
    os.makedirs(target_folder)
  os.chmod(target_folder, 0o777)  # mode=0o777 for read write for all users
  # Move all contents (files and subfolders)
  try:
    shutil.move(source_folder, target_folder)
    print("Contents moved successfully!")
    
    #recreate the empty photos folder
    if not os.path.exists(source_folder):
        os.makedirs(source_folder)
    os.chmod(source_folder, 0o777)
  except OSError as e:
    print("Error moving contents:", e)
  
def copy_photos_to_backup(source_folder, target_folder):
  """
  Copies all files and subfolders from the source folder to the target folder recursively,
  handling existing dated folders and copying their contents.
  Args:
      source_folder: The path to the source folder.
      target_folder: The path to the target folder.
  """
  if not os.path.exists(target_folder):
    os.makedirs(target_folder)
  os.chmod(target_folder, 0o777)  # mode=0o777 for read write for all users

  for item in os.listdir(source_folder):
    source_path = os.path.join(source_folder, item)
    target_path = os.path.join(target_folder, item)

    if os.path.isfile(source_path):
      shutil.copy2(source_path, target_path)  # Copy files
      os.chmod(target_path, 0o777)  # Set permissions for copied files
    else:
      # Handle existing dated folders
      if not os.path.exists(target_path):
        shutil.copytree(source_path, target_path)  # Copy subdirectory if not exists
      else:
        # Copy contents of existing subdirectory
        for inner_item in os.listdir(source_path):
          inner_source_path = os.path.join(source_path, inner_item)
          inner_target_path = os.path.join(target_path, inner_item)
          if os.path.isfile(inner_source_path):
            shutil.copy2(inner_source_path, inner_target_path)
            os.chmod(inner_target_path, 0o777)  # Set permissions for copied files

def copy_folders_with_files(source_folder, target_folder):
    """
    Copies folders from the source folder to the target folder, 
    only if they contain files.

    Args:
        source_folder: The path to the source folder.
        target_folder: The path to the target folder.
    """

    if not os.path.exists(target_folder):
        os.makedirs(target_folder)

    for item in os.listdir(source_folder):
        source_path = os.path.join(source_folder, item)
        target_path = os.path.join(target_folder, item)

        if os.path.isfile(source_path):
            # If the source is a file, copy it directly
            try:
                shutil.copy2(source_path, target_path,dirs_exist_ok = True)
                print(f"Copied file: {source_path} to {target_path}")
            except Exception as e:
                print(f"Error copying {source_path} to {target_path}: {e}")
        elif os.path.isdir(source_path):  # Check if it's a directory
            # If it's a directory, check if it contains files
            if not os.listdir(source_path):
                continue  # Skip empty directories
            else:
                # Copy the directory and its contents
                try:
                    shutil.copytree(source_path, target_path,dirs_exist_ok = True)
                    print(f"Copied folder: {source_path} to {target_path}")
                except Exception as e:
                    print(f"Error copying folder {source_path} to {target_path}: {e}")


def verify_copy_bad(source_folder, destination_folder):
    """
    Compares the contents of a source folder and its subdirectories with the destination folder to verify successful copy.
    If an empty directory is found that hasn't been copied, it's ignored.
    If a directory with files is missing, it reports an error.
    Args:
        source_folder: The path to the source folder.
        destination_folder: The path to the destination folder.
    Returns:
        A list of any differences found between the source and destination folders.
    """
    source_path = Path(source_folder)
    dest_path = Path(destination_folder)
    differences = []
    # Check if source folder exists
    if not source_path.exists():
        differences.append(f"Error: Source folder '{source_folder}' does not exist.")
        return differences

    # Compare files and subdirectories recursively
    for root, dirs, files in os.walk(source_path):
        rel_path = os.path.relpath(root, source_path)
        dest_dir = os.path.join(dest_path, rel_path)

        # Check if corresponding directory exists in destination
        if not os.path.exists(dest_dir):
            # If directory doesn't exist, check if it's empty
            if not os.listdir(dest_dir):
                # If it's empty, ignore it
                continue
            else:
                differences.append(f"Missing directory in destination: {dest_dir}")
                continue

        # Compare files within the directory
        for filename in files:
            source_file = os.path.join(root, filename)
            dest_file = os.path.join(dest_dir, filename)

            # Check if file exists in destination
            if not os.path.isfile(dest_file):
                differences.append(f"Missing file in destination: {dest_file}")

    return differences

def verify_copy(source_folder, destination_folder):
  """
  Compares the contents of a source folder and its subdirectories with the destination folder to verify successful copy.
  Args:
      source_folder: The path to the source folder.
      destination_folder: The path to the destination folder.
  Returns:
      A list of any differences found between the source and destination folders.
  """
  source_path = Path(source_folder)
  dest_path = Path(destination_folder)
  differences = []

  # Check if source folder exists
  if not source_path.exists():
    differences.append(f"Error: Source folder '{source_folder}' does not exist.")
    return differences

  # Compare files and subdirectories recursively
  for root, dirs, files in os.walk(source_path):
    rel_path = os.path.relpath(root, source_path)
    dest_dir = os.path.join(dest_path, rel_path)

    # Check if corresponding directory exists in destination
    if not os.path.exists(dest_dir):
      differences.append(f"Missing directory in destination: {dest_dir}")
      continue

    # Compare files within the directory
    for filename in files:
      source_file = os.path.join(root, filename)
      dest_file = os.path.join(dest_dir, filename)

      # Check if file exists in destination
      if not os.path.isfile(dest_file):
        differences.append(f"Missing file in destination: {dest_file}")
  return differences

def delete_folder_contents(folder_path):
  """
  Deletes all contents (files and subdirectories) from a folder.
  Args:
      folder_path: The path to the folder to be emptied.
  """
  for root, dirs, files in os.walk(folder_path, topdown=False):
    for filename in files:
      file_path = os.path.join(root, filename)
      os.remove(file_path)
    for dir in dirs:
      dir_path = os.path.join(root, dir)
      os.rmdir(dir_path)

def delete_original_photos(source_folder):
    """
    Deletes all files from the source folder.
    Args:
        source_folder: The path to the source folder.
    """
    print("trying to delete fresh")
    for filename in os.listdir(source_folder):
        file_path = os.path.join(source_folder, filename)
        try:
            if os.path.isfile(file_path):
                os.remove(file_path)
        except OSError as e:
            print(f"Error deleting file {file_path}: {e}")
            
def get_dir_size(dir_path):
  """
  Calculates the total size of a directory and its subdirectories.
  Args:
      dir_path: The path to the directory.
  Returns:
      The total size of the directory in bytes.
  """
  total_size = 0
  for dirpath, dirnames, filenames in os.walk(dir_path):
    for filename in filenames:
      file_path = os.path.join(dirpath, filename)
      if not os.path.islink(file_path):  # Skip symbolic links (optional)
        total_size += os.path.getsize(file_path)
  return total_size

def snapshot_files(folder):
    """Return the set of all file paths currently in folder (recursive)."""
    result = set()
    for root, dirs, files in os.walk(str(folder)):
        for f in files:
            result.add(os.path.join(root, f))
    return result


def move_snapshot_files(file_set, source_root, dest_root):
    """
    Move only the files in file_set from source_root to dest_root.
    Files that appeared in source_root after the snapshot are left untouched,
    keeping them in photos/ so the next backup cycle picks them up.
    """
    if not os.path.exists(dest_root):
        os.makedirs(dest_root)
        os.chmod(dest_root, 0o777)
    for source_file in sorted(file_set):
        if not os.path.exists(source_file):
            continue
        rel = os.path.relpath(source_file, str(source_root))
        dest_file = os.path.join(str(dest_root), rel)
        os.makedirs(os.path.dirname(dest_file), exist_ok=True)
        shutil.move(source_file, dest_file)


def remove_empty_dirs(folder):
    """Remove empty subdirectories bottom-up; leaves the folder itself intact."""
    for root, dirs, files in os.walk(str(folder), topdown=False):
        for d in dirs:
            dirpath = os.path.join(root, d)
            try:
                os.rmdir(dirpath)  # only succeeds if the directory is empty
            except OSError:
                pass


def purge_oldest_backedup_until_threshold(folder, target_free_bytes):
    """
    Delete oldest dated subdirs from photos_backedup/ one at a time until
    SD card free space reaches target_free_bytes.
    Only call this when external storage is confirmed connected.
    Never touches photos/ (fresh, unbackedup data).
    """
    if not os.path.exists(folder):
        return
    subdirs = sorted([d for d in Path(folder).iterdir() if d.is_dir()])
    for subdir in subdirs:
        _, current_free = get_storage_info(desktop_path)
        if current_free >= target_free_bytes:
            break
        dir_size = get_dir_size(subdir)
        shutil.rmtree(subdir)
        print(f"Purged oldest backed-up folder: {subdir} ({dir_size/1e9:.2f} GB freed)")


def backup_and_delete(source_folder, destination_folder):
  """
  Back up files and delete them
  Args:
      source_folder: path to the original location
      destination_folder: path to backup location
  """
  # Ensure source and destination folders exist
  if not os.path.exists(source_folder):
      print(f"Source folder '{source_folder}' does not exist.")
      return
  if not os.path.exists(destination_folder):
      os.makedirs(destination_folder)
      print(f"Created destination folder '{destination_folder}'.")
  try:
      # Copy the contents of the source folder to the destination folder
      for item in os.listdir(source_folder):
          src_path = os.path.join(source_folder, item)
          dest_path = os.path.join(destination_folder, item)
          if os.path.isdir(src_path):
              shutil.copytree(src_path, dest_path)
          else:
              shutil.copy2(src_path, dest_path)
      print(f"All contents of '{source_folder}' successfully copied to '{destination_folder}'.")
      # Verify the copy
      src_items = set(os.listdir(source_folder))
      dest_items = set(os.listdir(destination_folder))
      if not src_items.issubset(dest_items):
          print("Error: Not all items were copied successfully.")
          return
      # Delete the contents of the source folder
      for item in os.listdir(source_folder):
          src_path = os.path.join(source_folder, item)
          if os.path.isdir(src_path):
              shutil.rmtree(src_path)
          else:
              os.remove(src_path)
      print(f"All contents of '{source_folder}' have been deleted.")
  except Exception as e:
      print(f"An error occurred: {e}")



if __name__ == "__main__":
    # Check if "photos" folder exists
    if not os.path.exists(photos_folder):
        print("Photos folder not found, exiting.")
        exit(1)

    # Get total and available space on desktop
    desktop_total, desktop_available = get_storage_info(desktop_path)
    print("Desktop Total    Storage: \t" + str(desktop_total/1000000000))
    print("Desktop Available Storage: \t" + str(desktop_available/1000000000))

    # Find all external drives and rank by available space (descending)
    disks = {}
    for mount_point in os.listdir("/media"):
        path = Path(f"/media/{mount_point}")
        if mount_point == "pi" or "mmcblk" in mount_point:
            continue
        if path.is_dir() and is_mounted(path):
            total_size, available_size = get_storage_info(path)
            if total_size > 0:
                disks[path] = total_size, available_size

    print("~~~sorting disks~~~~~~")
    if disks:
        sorted_disks = sorted(disks.items(), key=lambda item: item[1][0], reverse=True)
        print("External Drives (Ranked by Total Size - Descending):")
        for disk_name, capacity in sorted_disks:
            print(
                f"{disk_name}: total size {capacity[0]/1000000000} GB - available size {capacity[1]/1000000000} GB"
            )
    else:
        print("No external drives found.")
        print("Skipping backup. Not purging photos_backedup because no external storage is available to confirm data is safe.")
        exit(1)
    print("~~~sorted~~~~~~")

    # Check if internal storage is running low.
    # Only purge backed-up photos if external storage is confirmed present (already checked above).
    # Never purge photos/ (unbackedup fresh data).
    x = internal_storage_minimum
    print("Internal storage threshold: " + str(x * 1024**3/1000000000))
    if desktop_available < x * 1024**3:
        print("Low internal storage — purging oldest backed-up photos (external storage confirmed present).")
        purge_oldest_backedup_until_threshold(backedup_photos_folder, x * 1024**3)
        # Refresh available space after purge
        _, desktop_available = get_storage_info(desktop_path)
        print(f"Internal storage after purge: {desktop_available/1000000000:.2f} GB available")
    else:
        print("More than " + str(x) + "GB remain so original backed up files are kept")

    thingsworkedok = False
    # Iterate through disks starting with the largest; back up to the first one with enough room
    for disk_name, capacity in sorted_disks:
        print("chosen Disk: " + str(disk_name))
        total_available, external_available = capacity
        print("total available \t" + str(total_available/1000000000))
        total_size_bytes = get_dir_size(photos_folder)
        print("total needed \t\t" + str(total_size_bytes/1000000000))
        if external_available > total_size_bytes:
            external_backup_folder = disk_name / backup_folder_name
            # Snapshot which files exist NOW before the copy starts.
            # Any photo TakePhoto writes during the copy won't be in this set
            # and will stay in photos/ safely for the next backup cycle.
            files_to_move = snapshot_files(photos_folder)
            print(f"doing the backup to external: {external_backup_folder}")
            copy_folders_with_files(photos_folder, external_backup_folder)  # skips blank folders
            print(f"Photos successfully copied to external backup folder: {external_backup_folder}")

            logfolder = "logs_" + computerName
            external_logs_folder = disk_name / logfolder
            copy_photos_to_backup(logs_folder, external_logs_folder)
            print(f"Logs successfully copied to external backup folder: {external_logs_folder}")

            differences = ""  # skipping verify
            if differences:
                print("Differences found:")
                for difference in differences:
                    print(difference)
            else:
                move_snapshot_files(files_to_move, photos_folder, backedup_photos_folder)
                remove_empty_dirs(photos_folder)  # clean up empty dated subfolders left by move
                print(f"Photos moved to internal backup folder: {backedup_photos_folder}")
            thingsworkedok = True
            if thingsworkedok:
                break
        else:
            print("This External storage doesn't have enough space for backup.\n Trying next available storage if there is one ")

    if thingsworkedok == False:
        print("stuff never worked out with this backup, your files are not properly backedup")
    else:
        record_backup_done()
        print("stuff worked out BACKUP COMPLETE")
    print("end")
quit()
