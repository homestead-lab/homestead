"""The signature check on the validated-versions list, against RFC 8032's
test vectors (section 7.1)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import homestead_ed25519 as ED

# (secret, public, message, signature), hex: RFC 8032 7.1, tests 1 to 3.
VECTORS = [
    ("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
     "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", "",
     "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
    ("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c", "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
    ("c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7",
     "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025", "af82",
     "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a"),
]


class Ed25519Tests(unittest.TestCase):
    def test_the_rfc_s_vectors(self):
        for secret, public, message, signature in VECTORS:
            with self.subTest(public=public[:8]):
                secret, public, message, signature = (bytes.fromhex(x) for x in (secret, public, message, signature))
                self.assertEqual(public, ED.public_key(secret))
                self.assertEqual(signature, ED.sign(secret, message))
                self.assertTrue(ED.verify(public, message, signature))

    def test_anything_changed_does_not_verify(self):
        secret, public, _, _ = (bytes.fromhex(x) for x in VECTORS[1])
        signature = ED.sign(secret, b"the list")
        self.assertTrue(ED.verify(public, b"the list", signature))
        self.assertFalse(ED.verify(public, b"the list!", signature))
        flipped = bytes([signature[0] ^ 1]) + signature[1:]
        self.assertFalse(ED.verify(public, b"the list", flipped))
        other = bytes.fromhex(VECTORS[0][1])
        self.assertFalse(ED.verify(other, b"the list", signature))
        for bad in (b"", signature[:63], b"\xff" * 64):
            self.assertFalse(ED.verify(public, b"the list", bad))
        self.assertFalse(ED.verify(b"\x00" * 31, b"the list", signature))
        # s at or above the group order is refused (malleability).
        high = signature[:32] + int.to_bytes(int.from_bytes(signature[32:], "little") + ED.Q, 32, "little")
        self.assertFalse(ED.verify(public, b"the list", high))


if __name__ == "__main__":
    unittest.main()
