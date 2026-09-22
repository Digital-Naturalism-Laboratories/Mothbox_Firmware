import subprocess
import time
import os


def get_voltage():
    """Run vcgencmd and extract EXT5V_V voltage."""
    result = subprocess.run(
        ["vcgencmd", "pmic_read_adc"], capture_output=True, text=True
    )
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("EXT5V_V"):
            # Example: "EXT5V_V volt(24)=5.36000000V"
            try:
                value_str = line.split("=")[1].replace("V", "").strip()
                return float(value_str)
            except (IndexError, ValueError):
                return None
    return None

# NOTE: this script reports the 5 V rail ONCE, which is what Diagnostics.py
# wants. It used to carry a copy of log_voltage() that referenced an undefined
# LOG_FILE and would have crashed the moment anything called it. The working
# continuous logger lives in scripts/5v_power_logger.py -- use that instead of
# re-adding one here.

if __name__ == "__main__":
    v = get_voltage()
    print("5v to pi: "+str(v))

