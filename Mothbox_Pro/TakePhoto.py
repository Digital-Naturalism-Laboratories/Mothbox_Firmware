#!/usr/bin/python

"""
Takephoto - Andy Quitmeyer - Public Domain

This script goes through the proper setup for using 64MP cameras on a pi4 or pi5

Its order of operations is like this
-Determine if pi4 or pi5 to set max resolution
-Read in camera settings
-Configure camera settings like HDR mode
-Calibrate camera's exposure and focus (if mandated)
-prepare the camera for capturing pixels
-Turning camera flash on
-Capturing the pixels
-Turning the camera flash off as quickly as possible after
-Saving the pixels to disk


TODO:
-Add safety function to detect if disk space left is less than 7GB and refuse to take more photos, and give a debug flash pattern (such as SOS with ring lights)
"""
import os
import sys
#######---- Check for Boot lock ------
BOOT_LOCK = "/run/boot_script_running"

if os.path.exists(BOOT_LOCK):
    sys.exit(0)

#-----------------------------##
import time
from picamera2 import Picamera2, Preview
from libcamera import controls
from libcamera import Transform

import time
import datetime
from datetime import datetime, timedelta
computerName = "mothboxNOTSET"
import cv2

import csv
import sys
import shutil
import io
from PIL import Image
import piexif
import subprocess


import time

import os, platform
from pathlib import Path

       
CONTROL_ROOT = Path("/boot/firmware/mothbox_custom/system/controls")
CAMERA_SETTINGS_PATH = "/boot/firmware/mothbox_custom/camera_settings.csv"
DEFAULT_CAMERA_SETTINGS_PATH = "/boot/firmware/mothbox_custom/system/controls/defaults/default_camera.txt"
AF_LENS_PATH = CONTROL_ROOT / "aflensposition.txt"
AF_GAIN_PATH=CONTROL_ROOT / "autogain.txt"
AF_EXPOSURE_PATH =CONTROL_ROOT / "exposuretime.txt"


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


#IF the mothbox is supposed to be off, don't take a photo!
#mode = "ACTIVE"  # possible modes are OFF or DEBUG or ACTIVE or PARTY, active is dddddddddddddefault
mode = read_control(CONTROL_ROOT / "mode.txt", "mode", "ACTIVE")

#thecontrol_values = get_control_values("/boot/firmware/mothbox_custom/system/controls.txt")
#sActive = int(thecontrol_values.get("Active", 1))

#internal_storage_minimum = int(thecontrol_values.get("safetyGB",9)) # This is Gigabytes, below 6 on a raspberry pi 5 can make weird OS problems
internal_storage_minimum = int(
    read_control(CONTROL_ROOT / "safetygb.txt", "safetygb", 9)
)

internal_storage_minimum=internal_storage_minimum-1 # Important, this must be lower than the backup files minimum, or else the whole thing will just stall potential
extra_photo_storage_minimum=internal_storage_minimum
# Define paths
desktop_path = Path(
    "/home/pi/Desktop/Mothbox"
)  # Assuming user is "pi" on your Raspberry Pi

def restart_script():
    """
    Terminates the current script and restarts it.
    """
    print("Restarting script...")
    time.sleep(1)  # Optional: Add a small delay for clarity
    python_executable = sys.executable
    script_path = sys.argv[0]
    os.execv(python_executable, [python_executable, script_path])



def atomic_write(path: Path, content: str):
    tmp = path.with_suffix(path.suffix + ".tmp")

    with open(tmp, "w") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())

    os.replace(tmp, path)

def atomic_update_kv(path: Path, key: str, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    lines = []
    if path.exists():
        with open(path, "r") as f:
            for line in f:
                if "=" in line:
                    lines.append(line)

    found = False
    for i, line in enumerate(lines):
        if line.startswith(key + "="):
            lines[i] = f"{key}={value}\n"
            found = True

    if not found:
        lines.append(f"{key}={value}\n")

    atomic_write(path, "".join(lines))

def run_cmd(cmd):
    """Run a shell command safely"""
    subprocess.run(cmd, shell=True, check=False)

def flashOff():
    run_cmd("python /home/pi/Desktop/Mothbox/Flash_Off.py")
    print("Flash Off\n")
    run_cmd("python /home/pi/Desktop/Mothbox/Attract_On.py") # keep regulator on

def flashOn():
    run_cmd("python /home/pi/Desktop/Mothbox/Attract_On.py")
    run_cmd("python /home/pi/Desktop/Mothbox/Flash_On.py")

    print("Flash On\n")

def is_csv_valid(filepath):
    if not os.path.exists(filepath):
        return False
    if os.path.getsize(filepath) < 10:
        return False

    try:
        with open(filepath, newline="") as f:
            reader = csv.DictReader(f)
            required = {"SETTING", "VALUE", "DETAILS"}
            return required.issubset(reader.fieldnames or [])
    except Exception:
        return False


def restore_default_camera_csv():
    print("⚠️ Camera settings corrupted — restoring defaults")
    os.makedirs(os.path.dirname(CAMERA_SETTINGS_PATH), exist_ok=True)
    shutil.copy2(DEFAULT_CAMERA_SETTINGS_PATH, CAMERA_SETTINGS_PATH)


def auto_cast_value(setting, value):
    """
    Convert known camera settings to proper types.
    Everything else: best-effort numeric cast, else string.
    """

    # Explicit rules
    if setting in ("AeEnable", "AwbEnable"):
        return value.lower() in ("1", "true", "yes", "on")

    if setting in ("LensPosition", "AnalogueGain", "ExposureValue"):
        return float(value)

    if setting in (
        "ExposureTime", "AwbMode", "AfTrigger",
        "AfRange", "AfSpeed", "AfMode",
        "HDR", "HDR_width",
        "AutoCalibration", "AutoCalibrationPeriod",
        "ImageFileType", "VerticalFlip",
        "onlyflash"
    ):
        return int(float(value))

    # Best-effort fallback casting:
    try:
        if "." in value:
            return float(value)
        return int(value)
    except ValueError:
        return value


def load_camera_settings():
    global middleexposure, calib_lens_position, calib_exposure

    if not is_csv_valid(CAMERA_SETTINGS_PATH):
        restore_default_camera_csv()

    try:
        with open(CAMERA_SETTINGS_PATH, newline="") as csv_file:
            reader = csv.DictReader(csv_file)
            the_camera_settings = {}

            for row in reader:
                setting = row.get("SETTING", "").strip()
                value   = row.get("VALUE", "").strip()

                if not setting:
                    continue

                value = auto_cast_value(setting, value)

                if setting == "ExposureTime":
                    middleexposure = value
                    print("middleexposurevalue", middleexposure)

                the_camera_settings[setting] = value

            return the_camera_settings

    except Exception as e:
        print(f" Camera settings load failure: {e}")
        print("️ Reverting to default camera settings")

        restore_default_camera_csv()
        return load_camera_settings()


def atomic_write_csv(path, rows, fieldnames):
    """
    Atomically writes CSV file contents.
    Power-loss safe.
    """
    tmp_path = path + ".tmp"

    with open(tmp_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
        f.flush()
        os.fsync(f.fileno())

    os.replace(tmp_path, path)


def update_camera_settings(filename, new_settings):
    """
    Safely updates the values in a CSV file using atomic replacement.
    Power-loss safe.

    Args:
        filename (str): Path to CSV file.
        new_settings (dict): Dictionary of key → new value
    """

    # Ensure file exists — if missing, restore defaults first
    if not is_csv_valid(filename):
        restore_default_camera_csv()

    try:
        with open(filename, newline="") as csvfile:
            reader = csv.DictReader(csvfile)
            fieldnames = reader.fieldnames
            rows = []

            for row in reader:
                setting = row.get("SETTING", "").strip()
                if setting in new_settings:
                    row["VALUE"] = str(new_settings[setting])
                rows.append(row)

        atomic_write_csv(filename, rows, fieldnames)

    except Exception as e:
        print(f" Failed updating camera CSV safely: {e}")
        
        
def get_serial_number():
  """
  This function retrieves the Raspberry Pi's serial number from the CPU info file.
  """
  try:
    with open('/proc/cpuinfo', 'r') as cpuinfo:
      for line in cpuinfo:
        if line.startswith('Serial'):
          return line.split(':')[1].strip()
  except (IOError, IndexError):
    return None

def stop_cron():
    """Runs the command 'service cron stop' to stop the cron service."""
    try:
        subprocess.run(["sudo", "service", "cron", "stop"], check=True)
        print("Cron service stopped successfully.")
    except subprocess.CalledProcessError as error:
        print("Error stopping cron service:", error)

def start_cron():
    """Runs the command 'service cron stop' to stop the cron service."""
    try:
        subprocess.run(["sudo", "service", "cron", "start"], check=True)
        print("Cron service started successfully.")
    except subprocess.CalledProcessError as error:
        print("Error starting cron service:", error)
        
def print_af_state(request):
    md = request.get_metadata()
    #print(("Idle", "Scanning", "Success", "Fail")[md['AfState']], md.get('LensPosition'))
def run_calibration():
    global calib_lens_position, calib_exposure, camera_settings, width, height, picam2
    #preview_config = picam2.create_preview_configuration(main={'format': 'RGB888', 'size': (4624, 3472)})
    preview_config = picam2.create_preview_configuration(main={'size': (1920*2, 1080*2)})
    #still_config = picam2.create_still_configuration(main={"size": (width, height), "format": "RGB888"}, buffer_count=1)
    picam2.configure(preview_config)

    
    #picam2.set_controls({"AfMode":0,"AfSpeed":0,"AfRange":0, "LensPosition":7.0})

    
    #time.sleep(1)
    picam2.pre_callback = print_af_state
    
    
    time.sleep(2)
    picam2.set_controls({"LensPosition":7.0})
    #picam2.set_controls({"AfSpeed":controls.AfSpeedEnum.Fast})

    
    exposurevalue=camera_settings["ExposureValue"]
    picam2.set_controls({"ExposureValue":exposurevalue})# Floating point number between -8.0 and 8.0
    picam2.set_controls({"ExposureTime":500}) #we want a fast photo so we don't get blurry insects. We lock the exposure time and adjust gain. The max speed seems to be 469, but we will leave some overhead


    time.sleep(1)

    print("!!! Autofocusing !!!")
    afstart = time.time()
    flashOn()
    picam2.start(show_preview=False)
    #picam2.start()
    
    for i in range(5):
        if i == 15:
            pass
            #picam2.set_controls({'AnalogueGain': 4.0})
            #picam2.set_controls({"ExposureValue":-4.0})# Floating point number between -8.0 and 8.0

        elif i == 50:
            pass
            #picam2.set_controls({'AnalogueGain': 1.2})
            #picam2.set_controls({"ExposureValue":8.0})# Floating point number between -8.0 and 8.0

        md = picam2.capture_metadata()
        print(i, "Calibrating for BRIGHTNESS--  exposure: ", md['ExposureTime'],"  gain: ", md['AnalogueGain'], "  Lensposition:", md['LensPosition'])
    
    md = picam2.capture_metadata()
    calib_exposure = md['ExposureTime']
    autogain= md['AnalogueGain']

    print("Exposure: "+str(calib_exposure))
    print("Autogain: "+str(autogain))
    
    time.sleep(.1) #give a tiny bit of time to let the flash start up

    #picam2.set_controls({"AfMode": 2})
    #time.sleep(7)
    print("Running autofocus...")
    #picam2.start(show_preview=True, ) #preview has to be on for some reason to work
    success = picam2.autofocus_cycle()

    #picam2.pre_callback = None
    flashOff()
    print("Autofocus completed! "+str(time.time()-afstart))
    md = picam2.capture_metadata()
    
    

    calib_lens_position = md['LensPosition']
    atomic_update_kv(AF_LENS_PATH, "aflensposition", calib_lens_position)
    focusstate = md['AfState']

    print("LensPosition: "+str(calib_lens_position))
    print(focusstate)


    #camera_settings["LensPosition"]=calib_lens_position
    
    #camera_settings["ExposureTime"]=calib_exposure
    atomic_update_kv(AF_EXPOSURE_PATH, "exposuretime", calib_exposure)

    #camera_settings["AnalogueGain"]=autogain
    atomic_update_kv(AF_GAIN_PATH, "autogain", autogain)

    picam2.stop()
    picam2.stop_preview()
    
    #save last time
    #set_last_calibration(control_values_fpath)
    atomic_update_kv(os.path.join(CONTROL_ROOT, "lastcalibration.txt"), "lastcalibration", str(time.time()))

    #save the calibrated settings back to the CSV
    #new_settings = {"LensPosition": calib_lens_position, "ExposureTime": calib_exposure, "AnalogueGain": autogain} 
    #update_camera_settings(chosen_settings_path, new_settings)
    
    #restart the whole script now because for some reason if we just run the phot taking it is always slightly brighter
    time.sleep(1)
    restart_script()
    

def list_exposuretimes(middle_exposuretime, num_photos, exposure_width):
  """
  This function calculates exposure times for HDR photos.

  Args:
      middle_exposuretime: The middle exposure time in microseconds.
      num_photos: The number of photos to take.
      exposure_width: The exposure width in steps (added/subtracted to middle time).

  Returns:
      A list of exposure times in microseconds for each HDR photo.
  """
  
  exposure_times = []
  half_num_photos =  int((num_photos -1) / 2)  # Ensure at least one photo on each side
  #print(half_num_photos)
  # Start with middle exposure for the first photo
  current_exposure = middle_exposuretime
  exposure_times.append(current_exposure)

  # Loop for positive adjustments (excluding middle)
  for i in range(1, half_num_photos+1):
    direction = 1
    current_exposure = middle_exposuretime+ direction * exposure_width * i
    exposure_times.append(current_exposure)

  # Loop for negative adjustments (excluding middle, if applicable)
  for i in range(half_num_photos):
    direction = -1
    current_exposure = middle_exposuretime+direction * exposure_width * (i + 1)  # Adjust index for missing middle photo
    exposure_times.append(current_exposure)
  return exposure_times

def create_dated_folder(base_path):
  """
  Creates a folder with the current date in the format YYYY-MM-DD if it doesn't exist.

  Args:
      base_path: The base path where the folder will be created.

  Returns:
      The full path to the created folder.
  """
  now = datetime.now()
  # Adjust for time between 12:00 pm and 11:59 am next day
  if 12 <= now.hour < 24:
    date_str = now.strftime("%Y-%m-%d")
  else:
    # Add a day if time is between 12:00 pm and next day's 11:59 am
    date_str = (now - timedelta(days=1)).strftime("%Y-%m-%d")
  folder_path = os.path.join(base_path, computerName+"_"+date_str)
  if not os.path.exists(folder_path):
    os.makedirs(folder_path)
  os.chmod(folder_path, 0o777)  # mode=0o777 for read write for all users
  return folder_path+"/"

def takePhoto_Manual():
    global middleexposure, calib_lens_position, calib_exposure
    # LensPosition: Manual focus, Set the lens position.
    now = datetime.now()
    timestamp = now.strftime("%Y_%m_%d__%H_%M_%S")  # Adjust the format as needed
    #TODO MAKE ALL TIME ISO FORMAT
    #timestamp = now.strftime("%y%m%d%H%M%S")
    #serial_number = get_serial_number()
    #lastfivedigits=serial_number[-5:]


    ''''''
    if camera_settings:
        picam2.set_controls(camera_settings)
    else:
        print("can't set controls")
    ''''''
    min_exp, max_exp, default_exp = picam2.camera_controls["ExposureTime"]
    #print(min_exp,"   ", max_exp,"   ", default_exp)


    #important note, to actually 100% lock down an AWB you need to set ColourGains! (0,0) works well for plain white LEDS
    cgains = 2.25943877696990967, 1.500129925489425659
    picam2.set_controls({"ColourGains": cgains})
   
    middleexposure = camera_settings["ExposureTime"]
    #middleexposure = calib_exposure  # this is more correct i think, but it's messing it up if it is here!
    exposure_times = list_exposuretimes(middleexposure, num_photos,exposuretime_width)
    print(exposure_times)
    
    time.sleep(1)
    picam2.start()
        
    time.sleep(3)

    start = time.time()

    if(num_photos>2):
        print("About to take HDR photo:  ",timestamp)
    else:
        print("About to take single photo:  ",timestamp)



    exposureset_delay=.3 #values less than 5 don't seem to work! (unless you restart the cam!)
    requests = []  # Create an empty list to store requests
    PILs = []
    metadatas = []
    #HDR loop
    for i in range(num_photos):
        #middleexposure = camera_settings["ExposureTime"]
        
        picam2.set_controls({"ExposureTime":exposure_times[i] })
        print("exp  ",exposure_times[i],"  ",i)
        #picam2.set_controls({"NoiseReductionMode":controls.draft.NoiseReductionModeEnum.HighQuality})
        picam2.start() #need to restart camera or wait a couple frames for settings to change

        time.sleep(exposureset_delay)#need some time for the settings to sink into the camera)
        
        flashOn()
        request = picam2.capture_request(flush=True)

        flashOff()
        #if not onlyflash:
            #flashOff()
        flashtime=time.time()-start

        pilImage = request.make_image("main")
        PILs.append(pilImage)
        #image_buffer = request.make_array("main")
        #requests.append(image_buffer)
        
        #print(request.get_metadata()) # this is the metadata for this image
        metadatas.append(request.get_metadata())
        request.release()

        picam2.stop()
        print("picture take time: "+str(flashtime))
        
    # Saving loop (can be done later)
    i=0
    for img in PILs:  
          exif_data=metadatas[i]
          pil_image = img
          # Save the image using PIL to get the image data on disk
          folderPath= "/home/pi/Desktop/Mothbox/photos/" #can't use relative directories with cron
          if not os.path.exists(folderPath):
            os.makedirs(folderPath)
          os.chmod(folderPath, 0o777)  # mode=0o777 for read write for all users

          folderPath = create_dated_folder(folderPath)
          
          
          print(ImageFileType)
          if ImageFileType==1: #png
              filepath = folderPath+computerName+"_"+timestamp+"_HDR"+str(i)+".png"
          elif ImageFileType==0: #jpeg
              filepath = folderPath+computerName+"_"+timestamp+"_HDR"+str(i)+".jpg"
          elif ImageFileType==2: #bmp
              filepath = folderPath+computerName+"_"+timestamp+"_HDR"+str(i)+".bmp"

        
          #print(exif_data) #This is a LOT of data
          print(camera_settings.get("LensPosition"))
          #https://github.com/hMatoba/Piexif/blob/3422fbe7a12c3ebcc90532d8e1f4e3be32ece80c/piexif/_exif.py#L406
          #https://piexif.readthedocs.io/en/latest/functions.html#dump
          zeroth_ifd = {piexif.ImageIFD.Make: u"MothboxV5",
              }
          exif_ifd = {#piexif.ExifIFD.DateTimeOriginal: u"2099:09:29 10:10:10",
            #piexif.ExifIFD.LensMake: u"LensMake",
            piexif.ExifIFD.ExposureTime: (1,int(1/(abs(exposure_times[i])/1000000))),
            piexif.ExifIFD.FocalLength: (int(calib_lens_position * 100), 10),
            piexif.ExifIFD.ISOSpeed: int(calib_gain * 100),
            piexif.ExifIFD.ISOSpeedRatings: int(calib_gain * 100),

            }
          gps_ifd = {
           #piexif.GPSIFD.GPSVersionID: (2, 0, 0, 0),
           #piexif.GPSIFD.GPSAltitudeRef: 1,
           #piexif.GPSIFD.GPSDateStamp: u"1999:99:99 99:99:99",
           }
          first_ifd = {piexif.ImageIFD.Make: u"Arducam64mp",
             #piexif.ImageIFD.XResolution: (40, 1),
             #piexif.ImageIFD.YResolution: (40, 1),
             piexif.ImageIFD.Software: u"piexif"
             }
          
          exif_dict = {"0th":zeroth_ifd, "Exif":exif_ifd, "GPS":gps_ifd, "1st":first_ifd}
          exif_bytes = piexif.dump(exif_dict)
          img.save(filepath,exif=exif_bytes, quality=96)
          print("Image saved to "+filepath)
          i=i+1


def determinePiModel():

  # Check Raspberry Pi model using CPU info
  cpuinfo = open("/proc/cpuinfo", "r")
  model = None  # Initialize model variable outside the loop
  themodel=None

  for line in cpuinfo:
    #print(line)
    if line.startswith("Model"):
      model = line.split(":")[1].strip()
      break
  cpuinfo.close()

  # Execute function based on model
  print(model)
  if model:  # Check if model was found
    if "Pi 4" in model:  # Model identifier for Raspberry Pi 4
      themodel=4
    elif "Pi 5" in model:  # Model identifier for Raspberry Pi 5
      themodel=5
    else:
      print("Unknown Raspberry Pi model detected. Going to treat as model 5")
      themodel=5
  else:
    print("Error: Could not read Raspberry Pi model information.")
    themodel=5
  return themodel

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

#---------------MAIN CODE--------------------- #

print("----------------- STARTING TAKEPHOTO-------------------")
now = datetime.now()
formatted_time = now.strftime("%Y-%m-%d %H:%M:%S")  # Adjust the format as needed

print(f"Current time: {formatted_time}")


#First check and see if we have enough storage left to keep taking photos, or else do nothing
# Get total and available space on desktop and external storage
desktop_total, desktop_available = get_storage_info(desktop_path)
print("Desktop Total    Storage: \t" + str(desktop_total))

print("Desktop Available Storage: \t" + str(desktop_available))
x=extra_photo_storage_minimum

print("Minimum storage needed: \t" +str(x * 1024**3))

if desktop_available < x * 1024**3:  # x GB in bytes
    print("not enough space to take more photos")
    quit()




#First figure out if this is a Pi4 or a Pi5
rpiModel=None
rpiModel=determinePiModel()

#default resolution
width=9000
height=6000

#the Pi4 can't really handle the FULL resolution, but pi5 can!
if(rpiModel==5):
    width=9248
    height=6944


#I don't really know why we need this below code, but it's here. it may have been an earlier attempt to find the pi model
if platform.system() == "Windows":
	print(platform.uname().node)
else:
	#computerName = os.uname()[1]
	print(os.uname()[1])   # doesnt work on windows


#HDR Controls
num_photos = 1
exposuretime_width = 18000
middleexposure=500 # 500 #minimum exposure time for Hawkeye camera 64mp arducam


#global onlyflash
#onlyflash=False



#control_values_fpath = "/boot/firmware/mothbox_custom/system/controls.txt"
#control_values = get_control_values(control_values_fpath)

#onlyflash = control_values.get("OnlyFlash", "True").lower() == "true"
#onlyflash = read_control(CONTROL_ROOT / "onlyflash.txt", "onlyflash", "0")

#LastCalibration = float(control_values.get("LastCalibration", 0))
LastCalibration= float(read_control(CONTROL_ROOT / "lastcalibration.txt", "lastcalibration", 0))


#computerName = control_values.get("name", "wrong")
computerName = read_control(CONTROL_ROOT / "name.txt", "name", "errorname")

'''
if(onlyflash):
    print("operating in always on flash mode")
'''


#------- Setting up camera settings -------------

'''
#This is for getting min and max details for certain settings, (See the picam pdf manual)
print(picam2.camera_controls["AnalogueGain"])
min_gain, max_gain, default_gain = picam2.camera_controls["AnalogueGain"]
'''
#This will be the path to the CSV holding the settings whether it is the one on the disk or the external CSV
global chosen_settings_path
default_path = "/boot/firmware/mothbox_custom/camera_settings.csv"
chosen_settings_path=default_path

#camera_settings = load_camera_settings("camera_settings.csv")#CRONTAB CAN'T TAKE RELATIVE LINKS! 
camera_settings = load_camera_settings()

    
#before calibration, set these values to the default we read in

calib_lens_position=6

calib_lens_position = float(read_control(AF_LENS_PATH, "aflensposition", None))
human_lens_position = camera_settings.get("LensPosition", None)

calib_exposure = float(read_control(AF_EXPOSURE_PATH, "exposuretime", None))
human_exposure = camera_settings["ExposureTime"]

calib_gain = float(read_control(AF_GAIN_PATH, "autogain", None))

AutoCalibration = camera_settings.pop("AutoCalibration",1) #defaults to what is set above if not in the files being read
AutoCalibrationPeriod = int(camera_settings.pop("AutoCalibrationPeriod",1000))


#Start up cameras
picam2 = Picamera2()


#----Autocalibration ---------

current_time = int(time.time())
timesincelastcalibration= current_time - LastCalibration
print("Last calibration was   ",timesincelastcalibration,"  seconds ago \n Autocalibration period is   ", AutoCalibrationPeriod)
recalibrated= False
if AutoCalibration and (timesincelastcalibration > AutoCalibrationPeriod):
    print("Do Autocalibrate")
    recalibrated=True
    print(current_time)
    #picam2.configure(preview_config)
    #picam2.configure(capture_config_fastAuto)
    run_calibration()
else:
    print("Don't Autocalibration")

# ------ Prepare to take actual photo -----------
#reload camera settings after possible calibration
camera_settings = load_camera_settings()
AutoCalibration = camera_settings.pop("AutoCalibration",1) #defaults to what is set above if not in the files being read
AutoCalibrationPeriod = int(camera_settings.pop("AutoCalibrationPeriod",1000))

if AutoCalibration:
    None
else:
    calib_lens_position = human_lens_position
    calib_exposure = human_exposure


#remove settings that aren't actually in picamera2
oldsettingsnames = camera_settings.pop("Name",computerName) #defaults to what is set above if not in the files being read
ImageFileType = int(camera_settings.pop("ImageFileType",0))
VerticalFlip = int(camera_settings.pop("VerticalFlip",0))
onlyflash =int(camera_settings.pop("onlyflash",0))

#HDR settings
num_photos = int(camera_settings.pop("HDR",num_photos)) #defaults to what is set above if not in the files being read

exposuretime_width = int(camera_settings.pop("HDR_width",exposuretime_width))
if(num_photos<1 or num_photos==2):
    num_photos=1

capture_main = {"size": (width, height), "format": "RGB888", }
capture_config = picam2.create_still_configuration(main=capture_main,raw=None, lores=None)
capture_config_flipped =  picam2.create_still_configuration(main=capture_main, transform=Transform(vflip=True, hflip=True), raw=None, lores=None)
picam2.configure(capture_config)


if camera_settings:
    print(camera_settings)
    print(calib_lens_position, calib_exposure, calib_gain)
    camera_settings["LensPosition"] = float(calib_lens_position)
    camera_settings["ExposureTime"] = int(calib_exposure)
    camera_settings["AnalogueGain"] = float(calib_gain)
    picam2.set_controls(camera_settings)

picam2.start()
time.sleep(1)

print("cam started");

picam2.stop()

if(VerticalFlip):
    picam2.configure(capture_config_flipped)
else:
    picam2.configure(capture_config)

time.sleep(.5)
takePhoto_Manual()


picam2.stop()

quit()

