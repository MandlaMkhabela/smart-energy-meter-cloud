"""
26G55 serial_to_mqtt.py
-----------------------
This bridge does NOT decide whether a bypass exists.

The Arduino/MCU sends:
    upstream_A,main_A,difference_A,upstream_W,main_W,power_difference_W,voltage_V,power_factor,possible_bypass_energy_Wh,status

Example:
    1.198,1.187,0.011,274.1,271.6,2.5,229.9,0.995,0.000,NORMAL
    1.207,0.623,0.584,276.4,142.7,133.7,230.1,0.995,0.334,POSSIBLE_BYPASS

The Python program only:
    1. reads the MCU packet,
    2. packages it as JSON,
    3. publishes it to MQTT.

Install:
    py -m pip install pyserial paho-mqtt

Run:
    py serial_to_mqtt.py
"""

import json
import time

import serial
from serial.tools import list_ports
import paho.mqtt.client as mqtt


BROKER = "broker.hivemq.com"
MQTT_PORT = 1883
TOPIC = "wits/group55/mandla-main001/data"

BAUD_RATE = 115200


def find_arduino_port():
    ports = list(list_ports.comports())

    if not ports:
        raise RuntimeError("No serial ports found. Check the Arduino USB cable.")

    print("Serial ports found:")
    for p in ports:
        print(f"  {p.device}: {p.description}")

    keywords = ("arduino", "ch340", "wch", "usb serial", "cp210", "usb-serial")

    for p in ports:
        desc = (p.description or "").lower()
        if any(k in desc for k in keywords):
            print(f"Using detected Arduino port: {p.device}")
            return p.device

    if len(ports) == 1:
        print(f"Using the only serial port found: {ports[0].device}")
        return ports[0].device

    raise RuntimeError(
        "Could not confidently identify the Arduino. "
        "Set SERIAL_PORT manually."
    )


# Leave as None for automatic detection.
# Example manual setting:
# SERIAL_PORT = "COM4"
SERIAL_PORT = None


def main():
    port = SERIAL_PORT or find_arduino_port()

    print(f"\nOpening {port} at {BAUD_RATE} baud...")
    ser = serial.Serial(port, BAUD_RATE, timeout=2)

    # Opening an Uno serial port often resets the board.
    time.sleep(2)

    mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    mqtt_client.connect(BROKER, MQTT_PORT, 60)
    mqtt_client.loop_start()

    print(f"MQTT connected to {BROKER}")
    print(f"Publishing to: {TOPIC}")
    print("Bypass decision source: Arduino MCU")
    print("Current and power source: Arduino MCU demo measurements")
    print("Waiting for MCU readings...\n")
    print("Press Ctrl+C to stop.\n")

    try:
        while True:
            raw = ser.readline().decode("utf-8", errors="ignore").strip()

            if not raw:
                continue

            parts = [p.strip() for p in raw.split(",")]

            if len(parts) != 10:
                print("Ignored serial line:", raw)
                continue

            try:
                upstream_current = float(parts[0])
                main_current = float(parts[1])
                current_difference = float(parts[2])
                upstream_power = float(parts[3])
                main_power = float(parts[4])
                power_difference = float(parts[5])
                voltage = float(parts[6])
                power_factor = float(parts[7])
                possible_bypass_energy = float(parts[8])
                status = parts[9]

                # Validate the MCU status string, but do not calculate it here.
                if status not in ("NORMAL", "POSSIBLE_BYPASS"):
                    print("Ignored invalid MCU status:", raw)
                    continue

                payload = {
                    "meter_id": "main001",
                    "upstream_current_A": round(upstream_current, 3),
                    "main_current_A": round(main_current, 3),
                    "difference_A": round(current_difference, 3),
                    "upstream_power_W": round(upstream_power, 1),
                    "main_power_W": round(main_power, 1),
                    "power_difference_W": round(power_difference, 1),
                    "voltage_V": round(voltage, 1),
                    "power_factor": round(power_factor, 3),
                    "possible_bypass_energy_Wh": round(possible_bypass_energy, 3),
                    "status": status,
                    "source": "arduino_mcu_via_serial",
                }

                message = json.dumps(payload)

                mqtt_client.publish(
                    TOPIC,
                    message,
                    qos=0,
                    retain=True
                )

                print(message)

            except ValueError:
                print("Ignored malformed numeric data:", raw)

    except KeyboardInterrupt:
        print("\nStopped.")

    finally:
        ser.close()
        mqtt_client.loop_stop()
        mqtt_client.disconnect()


if __name__ == "__main__":
    main()
