# monitor CPU temperature on RP5
# Prints CPU temperature every 5 second

import time

def get_cpu_temp():
	with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
		temp = f.read() # unit milidegree celcius
		return float(temp) / 1000

while True:
	try:
		temp = get_cpu_temp()
		print(f"CPU temperature: {temp:.2f}")
		time.sleep(5)
	except KeyboardInterrupt:
		break
