#!/usr/bin/python3
import sys
import os

import box_parts

def main():
    in_path, out_path = sys.argv[1:]
    boxes = box_parts.parse_path(in_path)
    with open(out_path, "wb") as out_fh:
        unpack_to(boxes, out_fh)

def unpack_to(boxes, out_fh):
    ftyp = boxes[0]
    if ftyp.type != "ftyp":
        raise ValueError("Missing ftyp", in_path)
    brands = ftyp.parse_brands()
    if "heic" not in brands:
        raise NotImplementedError("HEIF brand", brands)

    meta = get_one_box(boxes, "meta")
    item_id = get_child_box(meta, "pitm").parse_item_id()

    # Get codec-specific properties from iprp ("item properties") box.
    ipma = get_child_box(meta, "ipma").parse_associations()
    ipco = get_child_box(meta, "ipco")
    property_boxes = [ipco.children[i - 1] for i in ipma[item_id]]

    config_box = get_one_box(property_boxes, "hvcC")
    config_box.parse_seek_to_payload()
    structure = config_box.structure
    # Relevant fields begin 174 bits into an HEVCDecoderConfigurationRecord.
    structure.seek(21, os.SEEK_CUR)

    # This length is needed later, when copying data.
    #
    # It's only two bits long. Read a full byte and then mask off
    # bits that are part of other fields.
    length_size_minus_one = structure.read_int(1) & 0b11
    length_size = length_size_minus_one + 1

    # The actual header copy:
    num_of_arrays = structure.read_int(1)
    for i in range(num_of_arrays):
        # Each array starts with 24 bits:
        #
        # bit(1) array_completeness;
        # unsigned int(1) reserved = 0;
        # unsigned int(6) NAL_unit_type;
        # unsigned int(16) numNalus;
        structure.seek(3, os.SEEK_CUR)
        # Then:
        # unsigned int(16) nalUnitLength;
        # and the nalUnit of that length.
        copy_data_annex_b(structure, out_fh, length_size=2)

    # Get list of extents from the iloc ("item location") box.
    iloc = get_child_box(meta, "iloc")
    extents = iloc.parse_extents()
    extents = extents[item_id]

    # Copy out extent data from the mdat ("media data") box.
    mdat = get_one_box(boxes, "mdat")
    structure = mdat.structure
    for extent_offset, extent_length in extents:
        structure.seek(extent_offset, os.SEEK_SET)
        end = structure.tell() + extent_length
        while structure.tell() < end:
            copy_data_annex_b(structure, out_fh, length_size)

def copy_data_annex_b(structure, out_fh, length_size):
    length = structure.read_int(length_size)
    out_fh.write(b"\x00\x00\x00\x01")
    out_fh.write(structure.read(length))

def get_child_box(box, boxtype):
    """
    Recursively find first matching box. Raises if unmatched.
    """
    try:
        return next(box_parts.box_iter(box.children, boxtype=boxtype))
    except StopIteration:
        raise box_parts.BoxFormatError("Missing box", boxtype)

def get_one_box(boxes, boxtype):
    """
    Require exactly one matching box and return it.
    """
    boxes = [
        box
        for box in boxes
        if box.type == boxtype
    ]
    if len(boxes) == 0:
        raise box_parts.BoxFormatError("Missing boxes", boxtype)
    if len(boxes) > 1:
        raise box_parts.BoxFormatError("Too many boxes", boxtype)
    return boxes[0]

if __name__ == "__main__":
    main()
