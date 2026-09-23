"""A small bootable USB disk image, written with nothing but the standard library.

The image is an MBR disk with one FAT16 partition, which every UEFI firmware
will boot from when it holds \\EFI\\BOOT\\BOOTX64.EFI, and which Windows, macOS
and Linux can all open to read or edit the files on it. Long file names are
written as VFAT entries, so "autoexec.ipxe" keeps its name.

Only what a boot stick needs is here: directories, files, one partition. The
image is built in memory; at the default 16 MiB that is not worth streaming.
"""
import struct
import time

SECTOR = 512
PART_START = 2048                 # 1 MiB alignment, as partitioning tools do
SECTORS_PER_CLUSTER = 4           # 2 KiB clusters
RESERVED = 4
FATS = 2
ROOT_ENTRIES = 512
CLUSTER = SECTOR * SECTORS_PER_CLUSTER


def _dos_datetime(when=None):
    t = time.localtime(when or time.time())
    date = ((max(t.tm_year, 1980) - 1980) << 9) | (t.tm_mon << 5) | t.tm_mday
    clock = (t.tm_hour << 11) | (t.tm_min << 5) | (t.tm_sec // 2)
    return date, clock


_SHORT_OK = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!#$%&'()-@^_`{}~")


def _is_short(name):
    """Whether a name already fits 8.3 exactly as written."""
    if name in (".", ".."):
        return True
    base, _, ext = name.partition(".")
    return (0 < len(base) <= 8 and len(ext) <= 3 and "." not in ext
            and all(c in _SHORT_OK for c in base + ext))


def _short_name(name, taken):
    """The 11-byte 8.3 name for a directory entry, ~1-style when shortened."""
    if name in (".", ".."):
        return name.ljust(11).encode()
    if _is_short(name):
        base, _, ext = name.partition(".")
        return (base.ljust(8) + ext.ljust(3)).encode()
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    clean = lambda s: "".join(c for c in s.upper() if c in _SHORT_OK)  # noqa: E731
    stem, ext = clean(stem) or "FILE", clean(ext)[:3]
    for n in range(1, 100):
        tail = f"~{n}"
        candidate = (stem[:8 - len(tail)] + tail).ljust(8) + ext.ljust(3)
        if candidate not in taken:
            return candidate.encode()
    raise ValueError(f"too many names like {name}")


def _checksum(short):
    total = 0
    for byte in short:
        total = (((total & 1) << 7) | (total >> 1)) + byte & 0xFF
    return total


def _lfn_entries(name, short):
    """VFAT long-name entries, last part first, as they sit before the short entry."""
    units = list(name.encode("utf-16-le"))
    chars = [units[i] | (units[i + 1] << 8) for i in range(0, len(units), 2)]
    chunks = [chars[i:i + 13] for i in range(0, len(chars), 13)]
    check = _checksum(short)
    entries = []
    for index, chunk in enumerate(chunks, 1):
        padded = chunk + ([0x0000] if len(chunk) < 13 else [])
        padded += [0xFFFF] * (13 - len(padded))
        order = index | (0x40 if index == len(chunks) else 0)
        entry = bytearray(32)
        entry[0] = order
        struct.pack_into("<5H", entry, 1, *padded[0:5])
        entry[11], entry[12], entry[13] = 0x0F, 0, check
        struct.pack_into("<6H", entry, 14, *padded[5:11])
        struct.pack_into("<H", entry, 26, 0)
        struct.pack_into("<2H", entry, 28, *padded[11:13])
        entries.append(bytes(entry))
    return list(reversed(entries))


def _dir_entry(short, attr, cluster, size, when):
    date, clock = when
    entry = bytearray(32)
    entry[0:11] = short
    entry[11] = attr
    struct.pack_into("<BHHH", entry, 13, 0, clock, date, date)
    struct.pack_into("<H", entry, 20, 0)
    struct.pack_into("<HHHI", entry, 22, clock, date, cluster, size)
    return bytes(entry)


class _Volume:
    def __init__(self, total_sectors):
        self.total = total_sectors
        root_sectors = ROOT_ENTRIES * 32 // SECTOR
        # The FAT has to describe every data cluster; grow it until it does.
        fat_sectors = 1
        while True:
            data = total_sectors - RESERVED - FATS * fat_sectors - root_sectors
            clusters = data // SECTORS_PER_CLUSTER
            if (clusters + 2) * 2 <= fat_sectors * SECTOR:
                break
            fat_sectors += 1
        if not 4085 <= clusters < 65525:
            raise ValueError("that size does not make a FAT16 volume; use 8 to 1024 MiB")
        self.fat_sectors, self.root_sectors, self.clusters = fat_sectors, root_sectors, clusters
        self.fat = [0xFFF8, 0xFFFF] + [0] * clusters
        self.data = {}
        self.next = 2

    def allocate(self, payload):
        """Store bytes in a chain of clusters; returns the first cluster, or 0 if empty."""
        if not payload:
            return 0
        count = -(-len(payload) // CLUSTER)
        if self.next + count > self.clusters + 2:
            raise ValueError("the files do not fit on the image")
        first = self.next
        for i in range(count):
            cluster = first + i
            self.fat[cluster] = cluster + 1 if i < count - 1 else 0xFFFF
            self.data[cluster] = payload[i * CLUSTER:(i + 1) * CLUSTER].ljust(CLUSTER, b"\0")
        self.next += count
        return first


def _entries_for(children, when):
    """Directory entries for a mapping of name -> (attr, cluster, size)."""
    out, taken = [], set()
    for name, (attr, cluster, size) in children.items():
        short = _short_name(name, taken)
        taken.add(short.decode())
        if name not in (".", "..") and not _is_short(name):
            out.extend(_lfn_entries(name, short))
        out.append(_dir_entry(short, attr, cluster, size, when))
    return out


def build(files, size_mib=16, label="HOMESTEAD", when=None):
    """A disk image holding `files`, a mapping of "EFI/BOOT/BOOTX64.EFI" -> bytes."""
    if not 8 <= size_mib <= 1024:
        raise ValueError("choose a size from 8 to 1024 MiB")
    stamp = _dos_datetime(when)
    disk_sectors = size_mib * 1024 * 1024 // SECTOR
    part_sectors = disk_sectors - PART_START
    vol = _Volume(part_sectors)

    # A tree of directories from the paths given.
    tree = {}
    for path, payload in files.items():
        parts = [p for p in path.replace("\\", "/").split("/") if p]
        node = tree
        for part in parts[:-1]:
            node = node.setdefault(part, {})
            if not isinstance(node, dict):
                raise ValueError(f"{part} is both a file and a folder")
        node[parts[-1]] = bytes(payload)

    def write_dir(node, parent_cluster, is_root):
        """Writes a directory's children, then the directory; returns its cluster."""
        children = {}
        for name, value in node.items():
            if isinstance(value, dict):
                children[name] = ("dir", value)
            else:
                children[name] = (0x20, vol.allocate(value), len(value))
        # Subdirectories need their own cluster first, so "." can point at it.
        entries_by_name = {}
        placeholders = {}
        for name, item in children.items():
            if item[0] == "dir":
                placeholders[name] = vol.allocate(b"\0" * CLUSTER)
        for name, item in children.items():
            if item[0] == "dir":
                cluster = placeholders[name]
                sub = {".": (0x10, cluster, 0), "..": (0x10, 0 if is_root else parent_cluster, 0)}
                body = write_children(item[1], cluster)
                sub_entries = _entries_for(sub, stamp) + body
                blob = b"".join(sub_entries)
                if len(blob) > CLUSTER:
                    raise ValueError(f"too many files in {name}")
                vol.data[cluster] = blob.ljust(CLUSTER, b"\0")
                entries_by_name[name] = (0x10, cluster, 0)
            else:
                entries_by_name[name] = item
        return entries_by_name

    def write_children(node, cluster):
        return _entries_for(write_dir(node, cluster, False), stamp)

    root_children = write_dir(tree, 0, True)
    label_entry = _dir_entry(label.upper()[:11].ljust(11).encode(), 0x08, 0, 0, stamp)
    root = [label_entry] + _entries_for(root_children, stamp)
    if len(root) > ROOT_ENTRIES:
        raise ValueError("too many files at the top of the image")

    image = bytearray(disk_sectors * SECTOR)

    # MBR: one bootable FAT16 (LBA) partition, which UEFI firmware will look in.
    entry = struct.pack("<B3sB3sII", 0x80, b"\xfe\xff\xff", 0x0E, b"\xfe\xff\xff", PART_START, part_sectors)
    image[446:462] = entry
    image[510:512] = b"\x55\xaa"
    struct.pack_into("<I", image, 440, int((when or time.time())) & 0xFFFFFFFF)

    base = PART_START * SECTOR
    boot = bytearray(SECTOR)
    boot[0:3] = b"\xeb\x3c\x90"
    boot[3:11] = b"HOMESTD "
    struct.pack_into("<HBHBHHBHHHII", boot, 11,
                     SECTOR, SECTORS_PER_CLUSTER, RESERVED, FATS, ROOT_ENTRIES,
                     part_sectors if part_sectors < 65536 else 0, 0xF8, vol.fat_sectors,
                     32, 64, PART_START, part_sectors if part_sectors >= 65536 else 0)
    struct.pack_into("<BBBI", boot, 36, 0x80, 0, 0x29, int((when or time.time())) & 0xFFFFFFFF)
    boot[43:54] = label.upper()[:11].ljust(11).encode()
    boot[54:62] = b"FAT16   "
    boot[510:512] = b"\x55\xaa"
    image[base:base + SECTOR] = boot

    fat_bytes = struct.pack(f"<{len(vol.fat)}H", *vol.fat).ljust(vol.fat_sectors * SECTOR, b"\0")
    for copy in range(FATS):
        start = base + (RESERVED + copy * vol.fat_sectors) * SECTOR
        image[start:start + len(fat_bytes)] = fat_bytes

    root_start = base + (RESERVED + FATS * vol.fat_sectors) * SECTOR
    root_blob = b"".join(root)
    image[root_start:root_start + len(root_blob)] = root_blob

    data_start = root_start + vol.root_sectors * SECTOR
    for cluster, blob in vol.data.items():
        offset = data_start + (cluster - 2) * CLUSTER
        image[offset:offset + CLUSTER] = blob
    return bytes(image)


def read(image):
    """Reads a volume this module wrote back into {path: bytes}. For tests and checks."""
    part_start, = struct.unpack_from("<I", image, 454)
    base = part_start * SECTOR
    bps, spc, reserved, fats, root_entries = struct.unpack_from("<HBHBH", image, base + 11)
    fat_sectors, = struct.unpack_from("<H", image, base + 22)
    fat_start = base + reserved * bps
    root_start = fat_start + fats * fat_sectors * bps
    data_start = root_start + root_entries * 32
    cluster_size = bps * spc

    def chain(first):
        out, cluster = [], first
        while 2 <= cluster < 0xFFF8:
            out.append(cluster)
            cluster, = struct.unpack_from("<H", image, fat_start + cluster * 2)
        return out

    def entries(blob):
        name_parts, found = [], []
        for i in range(0, len(blob), 32):
            e = blob[i:i + 32]
            if e[0] == 0:
                break
            if e[0] == 0xE5:
                continue
            if e[11] == 0x0F:
                units = struct.unpack_from("<5H", e, 1) + struct.unpack_from("<6H", e, 14) + struct.unpack_from("<2H", e, 28)
                text = "".join(chr(u) for u in units if u not in (0, 0xFFFF))
                name_parts.insert(0, text)
                continue
            if e[11] & 0x08:
                name_parts = []
                continue
            short = e[0:8].decode().rstrip() + ("." + e[8:11].decode().rstrip() if e[8:11].strip() else "")
            name = "".join(name_parts) or short
            name_parts = []
            cluster, size = struct.unpack_from("<HI", e, 26)
            found.append((name, e[11], cluster, size))
        return found

    out = {}

    def walk(blob, prefix):
        for name, attr, cluster, size in entries(blob):
            if name in (".", ".."):
                continue
            body = b"".join(image[data_start + (c - 2) * cluster_size:data_start + (c - 1) * cluster_size]
                            for c in chain(cluster))
            if attr & 0x10:
                walk(body, prefix + name + "/")
            else:
                out[prefix + name] = body[:size]

    walk(image[root_start:data_start], "")
    return out
