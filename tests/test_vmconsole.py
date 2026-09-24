import io
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import homestead_console as WS
import homestead_vmconsole as V
import server


def frame(payload, opcode, final=True, masked=True):
    data = bytearray(WS.encode_frame(payload, opcode=opcode, masked=masked))
    if not final:
        data[0] &= 0x7F
    return bytes(data)


class VmConsoleTests(unittest.TestCase):
    def test_targets_are_checked(self):
        V.validate("default", "win11", "vnc", {"kube-system"})
        for args in (("default", "../x", "vnc"), ("kube-system", "vm", "vnc"), ("default", "vm", "rdp"), ("", "vm", "vnc")):
            with self.subTest(args=args), self.assertRaises((ValueError, PermissionError)):
                V.validate(*args, {"kube-system"})
        self.assertEqual("/apis/subresources.kubevirt.io/v1/namespaces/default/virtualmachineinstances/win11/vnc",
                         V.subresource_path("default", "win11", "vnc"))
        self.assertTrue(V.subresource_path("default", "win11", "serial").endswith("/console"))

    def test_a_fragmented_message_arrives_whole(self):
        # A framebuffer update split across frames, with a ping in between.
        stream = io.BytesIO(frame(b"abc", 2, final=False) + frame(b"", 9) + frame(b"def", 0, final=False) +
                            frame(b"g", 0) + frame(b"next", 2))
        self.assertEqual((2, b"abcdefg"), V.read_message(stream, require_mask=True))
        self.assertEqual((2, b"next"), V.read_message(stream, require_mask=True))

    def test_frames_bigger_than_a_terminal_would_need_are_allowed(self):
        big = b"x" * (2 * 1024 * 1024)
        stream = io.BytesIO(frame(big, 2, masked=False))
        self.assertEqual(big, V.read_message(stream, require_mask=False)[1])

    def test_serial_input_is_the_console_message_shape(self):
        self.assertEqual(b"ls\n", V.serial_input(json.dumps({"type": "input", "data": "ls\n"}).encode()))
        self.assertIsNone(V.serial_input(json.dumps({"type": "resize", "cols": 80}).encode()))
        with self.assertRaises(ValueError):
            V.serial_input(json.dumps({"type": "input", "data": "x" * 70000}).encode())

    def test_only_operators_open_a_vm_console(self):
        self.assertEqual("operator", server.needed_role("/api/vm/console", "GET"))

    def test_the_vnc_client_is_vendored_and_servable(self):
        root = ROOT / "web" / "vendor" / "novnc"
        for relative in ("core/rfb.js", "core/websock.js", "core/display.js", "vendor/pako/lib/zlib/inflate.js", "LICENSE.txt"):
            with self.subTest(relative=relative):
                self.assertTrue((root / relative).is_file())
        self.assertTrue(server.is_vendor_path("/vendor/novnc/core/rfb.js"))


if __name__ == "__main__":
    unittest.main()
