#!/usr/bin/env python3

from joycon.USBSwitchController import USBSwitchController

print("Connecting...")
right = USBSwitchController(interface=0)
left  = USBSwitchController(interface=1)
print("Connected.")

# Get the device's metadata/status.
print("Left joycon: {}".format(left.read_device_info()))
print("Right joycon: {}".format(right.read_device_info()))

# Ask the charging grip to connect to the JoyCon over its UART.
print("Pairing over downstream UART...")
right.pair_via_uart()

# Claim the JoyCon, so it does not revert to Bluetooth.
right.restrict_to_hid()

print(right.send_bluetooth_command(0x01, 0x02, response_length=12).tostring())
