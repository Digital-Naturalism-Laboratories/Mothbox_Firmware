#print("detected device: Mothbox DIY")
# Mothbox DIY (4.x) — INA260 sensor via I2C
import board
import adafruit_ina260
try:
	i2c = board.I2C()
	ina260 = adafruit_ina260.INA260(i2c)
	voltage = ina260.voltage
	print("Current: %.2f mA  Voltage: %.2f V  Power: %.2f mW" % (
		ina260.current, ina260.voltage, ina260.power))
except (OSError, ValueError) as e:
	print("INA260 sensor NOT CONNECTED:", e)
	voltage=-1
