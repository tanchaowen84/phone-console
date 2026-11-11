"""Read the phones actually attached to this Mac's USB bus."""

import plistlib
import re
import subprocess


def device_key(serial):
    return serial.replace("-", "").lower()


def parse_usb_devices(nodes):
    found = {}
    for node in nodes:
        name = node.get("USB Product Name", node.get("kUSBProductString", ""))
        serial = node.get("USB Serial Number", "")
        if (node.get("idVendor") == 0x05AC and isinstance(name, str) and name.startswith(("iPhone", "iPad"))
                and isinstance(serial, str) and re.fullmatch(r"[0-9A-Fa-f-]{8,64}", serial)):
            found[device_key(serial)] = name
        found.update(parse_usb_devices(node.get("IORegistryEntryChildren", [])))
    return found


def usb_devices():
    try:
        raw = subprocess.check_output(
            ["/usr/sbin/ioreg", "-a", "-r", "-c", "IOUSBHostDevice", "-p", "IOUSB"],
            stderr=subprocess.DEVNULL, timeout=2,
        )
        return parse_usb_devices(plistlib.loads(raw))
    except (OSError, subprocess.SubprocessError, plistlib.InvalidFileException):
        return {}
