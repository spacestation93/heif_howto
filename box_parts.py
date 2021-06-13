"""
Goals:
* Access raw data inside any ISOBMFF nested box, even as-yet unknown types.
  Child boxes of unknown types do get skipped by default, but callers
  can add their own container types.
* Parse without holding the entire container in RAM.
* Do not depend on libraries whose API changes so often one must pin
  to a specific, years-old patch-level release. (Alas, pymp4/construct!)

If you can find another library meeting all goals, please, use it! There
are way too many parses out there already.
"""
from __future__ import annotations
import collections
import dataclasses
import os
import struct

class StructuredFile(object):
    def __init__(self, fh):
        """
        * fh: Must be seekable.
        """
        self.fh = fh

    def __getattr__(self, attr):
        return getattr(self.fh, attr)

    def seek(self, n_bytes, whence):
        # Make the "whence" argument explicit and mandatory to reduce bugs.
        return self.fh.seek(n_bytes, whence)

    def read(self, n_bytes):
        buf = self.fh.read(n_bytes)
        if len(buf) != n_bytes:
            raise EOFError(self)
        return buf

    def read_struct(self, fmt):
        n_bytes = struct.calcsize(fmt)
        buf = self.read(n_bytes)
        return struct.unpack(fmt, buf)[0]

    def read_int(self, n_bytes):
        buf = self.read(n_bytes)
        return int.from_bytes(buf, "big")

    def read_ascii(self, n_bytes):
        buf = self.read(n_bytes)
        buf = buf.decode("ascii")
        return buf

    def peek(self, sz, at_pos=None):
        orig_pos = self.fh.tell()
        if at_pos is not None:
            self.fh.seek(at_pos, os.SEEK_SET)
        buf = self.fh.read(sz)
        self.fh.seek(orig_pos, os.SEEK_SET)
        return buf

    def get_total_length(self):
        pos = self.fh.tell()
        self.fh.seek(0, os.SEEK_END)
        end = self.fh.tell()
        self.fh.seek(pos, os.SEEK_SET)
        return end

class BoxFormatError(ValueError): pass

@dataclasses.dataclass
class Box(object):
    # Box is not an ABC so it can be used for partial parsing of
    # unknown box types.
    type: str
    structure: StructuredFile
    start: int
    end: int
    payload_start: int
    children: list[Box] = dataclasses.field(default_factory=list)

    def __str__(self):
        return "Box(type={}, size={})".format(
            self.type, (self.end - self.start))

    def get_payload(self):
        self.structure.seek(self.payload_start, os.SEEK_SET)
        return self.structure.read(self.end - self.payload_start)

    def append(self, box):
        self.children.append(box)

    def parse_seek_to_payload(self):
        self.structure.seek(self.payload_start, os.SEEK_SET)

    def parse_seek_to_children(self):
        self.parse_seek_to_payload()

    def parse_children(self):
        pass

class ContainerMixin(object):
    def parse_children(self):
        self.parse_seek_to_children()
        structure = self.structure
        while structure.tell() < self.end:
            box = parse_box(structure)
            self.children.append(box)
        if structure.tell() != self.end:
            raise BoxFormatError("Children beyond box end", self)

class FullBox(Box):
    def parse_seek_to_payload(self):
        offset = (8 + 24) // 8  # 8-bit version and 24-bit flags fields
        self.structure.seek(self.payload_start + offset, os.SEEK_SET)

    def parse_version(self):
        self.structure.seek(self.payload_start, os.SEEK_SET)
        return self.structure.read_int(1)

    def parse_flags(self):
        pos = self.payload_start + 1 # After version field.
        self.structure.seek(pos, os.SEEK_SET)
        return self.structure.read_int(3)

BOX_CLASSES: Dict[str, type] = {}

def register_boxtype(boxtype):
    def registrar(implementing_class):
        BOX_CLASSES[boxtype] = implementing_class
        implementing_class.type = dataclasses.field(default=boxtype)
        return implementing_class
    return registrar

@register_boxtype("ftyp")
class FileTypeBox(Box):
    def parse_brands(self):
        self.parse_seek_to_payload()
        structure = self.structure
        brand = structure.read_ascii(4)
        brands = set((brand,))
        structure.seek(4, os.SEEK_CUR) # Skip minor version.
        while structure.tell() < self.end:
            compatible_brand = structure.read_ascii(4)
            brands.add(compatible_brand)
        return brands

@register_boxtype("meta")
class MetaBox(ContainerMixin, FullBox):
    pass

def parse_box_list(structure):
    i = 0
    boxes = []
    while True:
        box = parse_box(structure)
        if box is None:
            break
        boxes.append(box)
    return boxes

def parse_box(structure):
    start = structure.tell()
    end = None
    try:
        sz = structure.read_int(4)
    except EOFError:
        return None

    try:
        boxtype = structure.read_ascii(4)
    except EOFError:
        # Ignore trailing NULLs at EOF.
        if sz == 0:
            return None
        else:
            raise

    if sz == 0:
        # Box extends to EOF.
        end = structure.get_total_length()
        sz = end - start
    elif sz == 1:
        # Large box.
        sz = structure.read_int(8)
    elif sz < 4:
        raise BoxFormatError("box too short")
    payload_start = structure.tell()
    sz -= (payload_start - start)
    if end is None:
        end = structure.tell() + sz

    if boxtype == "uuid":
        raise NotImplementedError("boxtype uuid")

    box_class = BOX_CLASSES.get(boxtype, Box)
    box = box_class(
        type=boxtype,
        structure=structure,
        payload_start=payload_start,
        start=start,
        end=end)
    box.parse_children()
    structure.seek(box.end, os.SEEK_SET)
    return box

def parse_file(fh):
    structure = StructuredFile(fh)
    return parse_box_list(structure)

def parse_path(path):
    fh = open(path, "rb")
    return parse_file(fh)

def box_iter(boxes, boxtype=None):
    to_visit = collections.deque(boxes)
    while to_visit:
        box = to_visit.popleft()
        to_visit.extend(box.children)
        if boxtype and box.type != boxtype:
            continue
        yield box

@register_boxtype("iloc")
class ItemLocationBox(FullBox):
    def parse_extents(self):
        """
        Returns {item_id: (extent_offset, extent_length)}.

        extent_offset is relative to the start of the box list, not mdat
        or its payload.
        """
        version = self.parse_version()
        if version != 0:
            raise NotImplementedError("iloc version", version)
        self.parse_seek_to_payload() # Spec says flags must be 0; skip.
        structure = self.structure
        buf = structure.read_int(1)
        offset_size = buf >> 4
        length_size = buf & 0x0F
        buf = structure.read_int(1)
        base_offset_size = buf >> 4
        item_count = structure.read_int(2)
        items = {}
        for i in range(item_count):
            item_id = structure.read_int(2)
            data_reference_index = structure.read_int(2)
            if data_reference_index != 0:
                raise NotImplementedError("data_reference_index in other file")
            base_offset = structure.read_int(base_offset_size)
            extent_count = structure.read_int(2)
            extents = []
            for i in range(extent_count):
                extent_offset = structure.read_int(offset_size)
                extent_length = structure.read_int(length_size)
                extents.append((base_offset + extent_offset, extent_length))
            items[item_id] = extents
        if structure.tell() != self.end:
            BoxFormatError("Extra content after iloc items")
        return items

@register_boxtype("pitm")
class PrimaryItemBox(FullBox):
    def parse_item_id(self):
        """
        Returns the primary item ID.
        """
        version = self.parse_version()
        self.parse_seek_to_payload() # Spec says flags must be 0; skip.
        structure = self.structure
        if version == 0:
            return structure.read_int(2)
        else:
            return structure.read_int(4)

@register_boxtype("iinf")
class ItemInfoBox(ContainerMixin, FullBox):
    def parse_seek_to_payload(self):
        version = self.parse_version()
        super().parse_seek_to_payload()
        if version == 0:
            self.structure.seek(2, os.SEEK_CUR)
        else:
            self.structure.seek(4, os.SEEK_CUR)

@register_boxtype("iref")
class ItemReferenceBox(ContainerMixin, FullBox):
    pass

@register_boxtype("iprp")
class ItemPropertiesBox(ContainerMixin, Box):
    pass

@register_boxtype("ipco")
class ItemPropertyContainerBox(ContainerMixin, Box):
    pass

@register_boxtype("ipma")
class ItemPropertyAssociationBox(FullBox):
    def parse_associations(self):
        """
        Returns {"item_id": [index...]}.
        """
        version = self.parse_version()
        flags = self.parse_flags()
        structure = self.structure
        entry_count = structure.read_int(4)
        items = {}
        for i in range(entry_count):
            if version == 0:
                item_id = structure.read_int(2)
            else:
                item_id = structure.read_int(4)
            associations = []
            association_count = structure.read_int(1)
            for i in range(association_count):
                if flags & 0x01:
                    buf = structure.read_int(2)
                else:
                    buf = structure.read_int(1)
                # Ignore (by masking away) "essential" bit.
                property_index = buf & 0b01111111
                associations.append(property_index)
            items[item_id] = associations
        if structure.tell() != self.end:
            BoxFormatError("Extra content after ipma items")
        return items

@register_boxtype("ispe")
class ImageSpacialExtentsPropertyBox(FullBox):
    def parse_resolution(self):
        """
        Returns (width, height) in pixels.
        """
        self.parse_seek_to_payload()
        return (
            self.structure.read_int(4),
            self.structure.read_int(4),
        )
