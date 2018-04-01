#
# Definition for a USB-connected Switch Controller (e.g. pro controller or joycons-on-charging-grip)
#
# Author: Kate Temkin <k@ktemkin.com>
# Author: shuffle2
#

import sys
import array
import time
import struct

import usb.core
import usb.util

class USBSwitchController:
    """
    Class representing a USB connection to a Switch controller; i.e. a connection 
    via the STM32 in a charging grip or pro controller.
    """

    # USB-HID commands handled by the charging grip / pro controller STM32.
    # 0x80 = vendor commands for managing the JoyCon or JoyCon-section
    # these commands result in downstream UART commands to the joycons
    COMMAND_DEVICE_INFO         = [0x80, 0x01]
    COMMAND_UART_PAIR           = [0x80, 0x02]
    COMMAND_RESTRICT_TO_HID     = [0x80, 0x04]
    COMMAND_SEND_UART_COMMAND   = [0x80, 0x92]

    # 0x82 = vendor commands for managing the grip or pro controller
    # these commands shouldn't result in any uart-to-joycon communications
    COMMAND_ENTER_DFU           = [0x82, 0x01]  # Note: erases grip STM32
    COMMAND_RESET               = [0x82, 0x02]


    # UART commands: are there more of these than this?
    UART_COMMAND_SEND_BT_CMD     = 0x00


    # Device IDs for the various USB HID Joycon devices.
    DEVICE_ID_CHARGING_GRIP = (0x057e, 0x200e)
    DEVICE_ID_BOOTLOADER    = (0x057e, 0x200f)



    USB_BUF_LEN = 64

    def __init__(self, interface=0, wait_for_device=True):
        """
        Sets up a new Charging Grip/Pro Controller connection.

        interface -- The interface number to use for our connection. For the charging grip,
            two interfaces are present that represent the two joycons.
        wait_for_device -- Iff this is set, will block until a device is found.
        """

        # By default, don't ever issue any DFU commands.
        self.lock_dfu_command = True

        # TODO: genericize
        self.device_id = self.DEVICE_ID_CHARGING_GRIP

        # Get a connection to the controller.
        self.connect_to_device(*self.device_id, wait_for_device=wait_for_device)

        # Store which endpoint we're working with.
        self._determine_endpoints(interface)
        if (self.endpoint_in is None) or (self.endpoint_out is None):
            raise IOError("Could not connect to the JoyCon with the given interface number.")

        # Ensure we control the JoyCon, rather than the linux hid subsystem.
        self.detach_kernel_driver(interface)


    def _determine_endpoints(self, interface_number):
        """
        Determines the endpoint that should be used to communicate with the given
        interface.

        interface_number -- The number of the interface whose endpoint should be queried.
        """
        self.endpoint_in = None
        self.endpoint_out = None

        # Grab the configuration object for the active device...
        conf = self.dev.get_active_configuration()

        # Search for an intrface that matches our interface number.
        for interface in conf.interfaces():

            # If this doesn't describe the interface we're looking for, skip it.
            if interface.bInterfaceNumber != interface_number:
                continue

            # Populate our information from each endpoint.
            for endpoint in interface.endpoints():

                # Set the address of our internal endpoints according to the endpoint's address.
                if endpoint.bEndpointAddress & usb.util.ENDPOINT_IN:
                    self.endpoint_in = endpoint.bEndpointAddress
                else:
                    self.endpoint_out = endpoint.bEndpointAddress


    def detach_kernel_driver(self, interface):
        """ Detaches the USBHID module from the given interface. """

        # ... unbind the HID driver so we can use the JoyCon.
        try:
            self.dev.detach_kernel_driver(interface)
        except usb.core.USBError:
            pass


    def connect_to_device(s, vid, pid, wait_for_device=True, inter_connection_delay=1):
        """
        Attempt to make a connction to the given controller.

        vid, pid -- The USB vid and PID to look for.
        wait_for_device -- True iff we should block until a device is found.
        inter_connection_delay -- The delay, in seconds, betwen connection attempts.
        """
        s.dev = None

        while s.dev is None:
            s.dev = usb.core.find(idVendor=vid, idProduct=pid)

            # If we're not waiting for the device, fail out immediately.
            if not wait_for_device:
                raise IOError("Controller not found!")

            time.sleep(inter_connection_delay)


    def usb_write(self, packet):
        """
        Sends a raw HID output report to the JoyCon.

        packet -- The raw bytes of the command to be executed.
        """
        return self.dev.write(self.endpoint_out, packet)


    def usb_read(self, length=None, timeout=200):
        """
        Reads a raw report from the JoyCon.

        length -- The number of bytes to be read.
        timeout -- Command timeout, in milliseconds.
        """

        # If no length was specified, assume the maximum.
        if length is None:
            length = self.USB_BUF_LEN

        # FIXME: do we want to return empty packets on failure?
        try:
            return self.dev.read(self.endpoint_in, length, timeout)
        except usb.core.USBError:
            return array.array('B', [0] * length)


    class UsbResponse:
        def __init__(s, pkt, data_len):
            s.cmd_type = pkt[0]
            s.cmd = pkt[1]
            s.status = pkt[2]
            s.data = pkt[3 : 3 + data_len]


    def send_command(self, command, response_length=USB_BUF_LEN):
        """
        Issue a command to the STM32 on the charging grip or pro controller.
        Some commands (e.g. COMMAND_SEND_JOYCON_COMMAND) issue further commands to the JoyCon.

        command -- The command to be issued.
        response_length -- The length of the response to be read.
        """

        resp = None

        # Issute the raw command over USB.
        self.usb_write(command)

        # If we're not looking for a response, don't try to read one.
        if response_length == 0: 
            return None

        while True:

            # FIXME: don't stall here forever on a comm error?

            # Read and parse a response from the device.
            raw_response = self.usb_read()
            resp = self.UsbResponse(raw_response, response_length)

            # Wait for us to recieve a response for the given command.
            if resp.cmd_type == command[0] | 1 and resp.cmd == command[1]:
                break

            # app mainloop resets usb and sends empty device_id_response in case of error...
            # need to check for that specficially and give up.
            if (resp.cmd_type, resp.cmd) == (0x81, 0x01) and resp.status != 0:
                return None

            # fw may respond with old data (e.g. if it's going through reset), so
            # we simply resend cmd until a decently-related looking response comes back.
            # fw could also be throwing us a lot of uart spew, which we want to skip.
            self.usb_write(command)


        if resp.status != 0:
            print('resp %02x:%02x error %x' % (resp.cmd_type, resp.cmd, resp.status))

        return resp.data


    def read_device_info(self, raw=False): 
        """
        Reads the Charging Grip's current status.
        """

        # Read the device's info...
        info = self.send_command(self.COMMAND_DEVICE_INFO, 8)

        # If we've had a request for raw data, return it.
        if raw:
            return info

        # Parse the data in the device info report... 
        joycon_type = int.from_bytes(bytes(info[0:1]), byteorder='little')
        mac         = int.from_bytes(bytes(info[1:7]), byteorder='little')

        # And compress things into a human-readable format.
        result = {
            'type': joycon_type,
            'mac':  mac
        }
        return result


    def pair_via_uart(self): 
        """ Pairs the JoyCon to the charging grip via UART.  """

        response = self.send_command(self.COMMAND_UART_PAIR)
        return response is not None


    def restrict_to_hid(self): 
        """ Instructs the JoyCon to communicate over HID instead of bluetooth. """
        self.send_command(self.COMMAND_RESTRICT_TO_HID, 0)


    def reconnect(s, dev_id=None, delay=1):
        """
        Re-forges a USB connection to the given device.

        dev_id -- The device ID to connect to. Optional; specifies a differnt device ID e.g. if this device
            has rebooted into DFU.
        """

        # FIXME: Don't use reconnect to connect to this; create a separate DFU object.
        if dev_id is None:
            dev_id = self.device_id

        time.sleep(delay)
        s.connect_to_device(dev_id)


    def unlock_dfu():
        # Theoretically, this could just set self.lock_dfu_command to false.
        # For safety, we're leaving this out.
        raise NotImplementError("Dangerous functionality skipped for now")


    def switch_to_dfu(self):
        """
        Switches the device into DFU mode. Can only be issud after unlock_dfu().
        WARNING: This function _will_ erase your Charging Grip or Pro Controller's firmware.
        """

        # Only allow issuing of the 'dfu switch' command if the user has unlocked.
        if self.lock_dfu_command:
            raise IOError("Entering DFU will erase the firmware from your controller; for safety, you'll need to unlock first.")

        s.send_command(self.COMMAND_ENTER_DFU, 0)
        s.reacquire_device(self.DEVICE_ID_DFU)


    def reset(self):
        """ Resets the Charging Grip or pro Controller STM32 section. """

        self.send_command(self.COMMAND_RESET, 0)
        self.reconnect()


    def send_uart_command(self, command, argument=b''):
        """ 
        Sends the JoyCon a command over UART. 
        This can contain an encapsulation of a JoyCon bluetooth command.

        command -- The command number to be issued.
        subcommand -- The subcommand number to be issued.
        argument -- The packet issued to the subcommand handler.

        returns The JoyCon's response.
        """

        # Build the UART command header, which describes the raw JoyCon command to be issued.
        command_header = struct.pack("<BHBBB", command, len(argument), 0, 0, 0)

        # And issue the wrapped command to the device.
        # FIXME: this isn't right; this should be 80 command
        packet       = bytes(self.COMMAND_SEND_UART_COMMAND) + command_header + argument
        raw_response = self.send_command(packet)

        # The response is led by 7 bytes of UART header, which we probably should check.
        # TODO: validate things here

        # The payload starts at byte 7, the eighth byte.
        response = raw_response[7:]
        return response


    def send_bluetooth_command(self, command, subcommand, argument=b'', response_length=35):
        """
        Sends the JoyCon a bluetooth command / subcommad over its wired interfface.
        See: https://github.com/dekuNukem/Nintendo_Switch_Reverse_Engineering/blob/master/bluetooth_hid_notes.md

        command    -- The bluetooth command to be issued.
        subcommand -- The subcommand number to be issued.
        argument   -- The packet issued to the subcommand handler.

        returns The response from the device.
        """

        # FIXME:  possibly move up to a UART abstraction module?
        command    = command.to_bytes(1, byteorder='little')
        rumble     = bytes([0x00, 0x00, 0x01, 0x40, 0x40, 0x00, 0x01, 0x40, 0x40])
        subcommand = subcommand.to_bytes(1, byteorder='little')

        # Wrap the bluetooth command in a UART command header. The grip will further
        # encapsulate it for transmission over real UART.
        packet = command + rumble + subcommand + argument
        raw_response = self.send_uart_command(self.UART_COMMAND_SEND_BT_CMD, packet) 

        # The response is led by 15-bytes of input report. The payload starts at byte 10, the eleventh byte.
        response = raw_response[15:15 + response_length]
        return response

