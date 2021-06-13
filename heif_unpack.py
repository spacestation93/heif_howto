#!/usr/bin/python3
"""
MVP demonstrating HEIF structure.

For real files with grids and other features use libheif!
"""
import abc
import os
import sys

import box_parts

def main():
    in_path, out_path = sys.argv[1:]

    boxes = box_parts.parse_path(in_path)
    ftyp = boxes[0]
    if ftyp.type != "ftyp":
        raise ValueError("Missing ftyp", in_path)
    brands = ftyp.parse_brands()
    if "heic" in brands:
        heif = HEIC(boxes)
    elif "avif" in brands:
        heif = AVIF(boxes)
    elif "avic" in brands:
        heif = AVIC(boxes)
    else:
        raise NotImplementedError("HEIF brand", brands)

    heif.unpack_to(out_path)

class HEIF(abc.ABC):
    def __init__(self, boxes):
        self.boxes = boxes
        self.mdat = self.get_one_box("mdat", self.boxes)

    def unpack_to(self, out_path):
        # Get the ID from the pitm ("primary item") box.
        item_id = self.get_child_box("pitm").parse_item_id()

        # Get list of extents from the iloc ("item location") box.
        iloc = self.get_child_box("iloc")
        extents = iloc.parse_extents()
        extents = extents[item_id]

        # Get codec-specific properties from iprp ("item properties") box.
        ipma = self.get_child_box("ipma").parse_associations()
        ipco = self.get_child_box("ipco")
        property_boxes = [ipco.children[i - 1] for i in ipma[item_id]]

        # Cross-checking the format with the infe ("item info entry")
        # box would be good, but not necessary.
        self.unpack_image(
            out_path=out_path,
            extents=extents,
            property_boxes=property_boxes,
        )

    @abc.abstractmethod
    def unpack_image(self, out_path, extents, property_boxes):
        pass

    def get_child_box(self, boxtype):
        """
        Recursively find first matching box. Raises if unmatched.
        """
        try:
            return next(box_parts.box_iter(self.boxes, boxtype=boxtype))
        except StopIteration:
            raise box_parts.BoxFormatError("Missing box", boxtype)

    def get_one_box(self, boxtype, boxes):
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

class HEIC(HEIF):
    config_boxtype = "hvcC"

    def unpack_image(self, out_path, extents, property_boxes):
        config_box = self.get_one_box(self.config_boxtype, property_boxes)
        with open(out_path, "wb") as out_fh:
            # Parse and copy out codec config properties associated
            # with the item. They're stored in codec-dependent boxes
            # under the iprp ("item properties") box.
            length_size_minus_one = self.copy_config_annex_b(config_box, out_fh)

            # Copy out extent data from the mdat ("media data") box.
            structure = self.mdat.structure
            for extent_offset, extent_length in extents:
                structure.seek(extent_offset, os.SEEK_SET)
                self.copy_video_annex_b(
                    structure=structure,
                    length_size=(length_size_minus_one + 1),
                    extent_length=extent_length,
                    out_fh=out_fh)

    def copy_config_annex_b(self, box, out_fh):
        box.parse_seek_to_payload()
        structure = box.structure
        # Relevant fields begin 174 bits into an HEVCDecoderConfigurationRecord.
        structure.seek(21, os.SEEK_CUR)
        length_size_minus_one = structure.read_int(1) & 0b11
        num_of_arrays = structure.read_int(1)
        for i in range(num_of_arrays):
            if structure.tell() > box.end:
                raise box_parts.BoxFormatError(
                    "Tried reading past end of config box")
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
            self.copy_data_annex_b(structure, out_fh, length_size=2)
        return length_size_minus_one

    def copy_video_annex_b(self, structure, length_size, extent_length, out_fh):
        end = structure.tell() + extent_length
        while structure.tell() < end:
            self.copy_data_annex_b(structure, out_fh, length_size)

    def copy_data_annex_b(self, structure, out_fh, length_size):
        length = structure.read_int(length_size)
        out_fh.write(b"\x00\x00\x00\x01")
        out_fh.write(structure.read(length))

class AVIC(HEIC):
    config_boxtype = "avcC"

    def copy_config_annex_b(self, box, out_fh):
        box.parse_seek_to_payload()
        structure = box.structure

        configuration_version = structure.read_int(1)
        if configuration_version != 1:
            raise ValueError("Invalid AVCDecoderConfigurationRecord version")

        avc_profile_indication = structure.read_int(1)
        structure.seek(2, os.SEEK_CUR)
        length_size_minus_one = structure.read_int(1) & 0b11

        def copy_array(num_of_nal_units):
            for i in range(num_of_nal_units):
                if structure.tell() > box.end:
                    raise box_parts.BoxFormatError(
                        "Tried reading past end of config box")
                self.copy_data_annex_b(structure, out_fh, length_size=2)

        num_of_sps = structure.read_int(1) & 0b00011111
        copy_array(num_of_sps)
        num_of_pps = structure.read_int(1)
        copy_array(num_of_pps)
        if avc_profile_indication in (100, 110, 122, 144):
            # Profile has Range Extensions.
            structure.seek((24 // 8), os.SEEK_CUR)
            num_of_sps_ext = structure.read_int(1)
            copy_array(num_of_sps_ext)

        return length_size_minus_one

class AVIF(HEIF):
    def unpack_image(self, out_path, extents, property_boxes):
        ispe = self.get_one_box("ispe", property_boxes)
        (width, height) = ispe.parse_resolution()

        with open(out_path, "wb") as fh:
            # IVF header:
            fh.write(b"DKIF")
            fh.write((0).to_bytes(2, "little")) # version
            fh.write((32).to_bytes(2, "little")) # header length
            fh.write(b"AV01")                 # FourCC
            fh.write((width).to_bytes(2, "little")) # width
            fh.write((height).to_bytes(2, "little")) # height
            # Arbitrarily 25 FPS to match FFMPEG.
            fh.write((25).to_bytes(4, "little")) # FPS numerator (framerate)
            fh.write((1).to_bytes(4, "little")) # FPS denominator (timescale)
            # The next field is "number of frames" in some IVF specs
            # and "duration" in others. FFMPEG seems to write all ones?
            fh.write(b"\xFF" * 4)
            fh.write(b"\xFF" * 4) # reserved

            # Frame header at byte 32:
            frame_len = sum(
                extent_length
                for extent_offset, extent_length in extents
            )
            frame_len += 2
            fh.write(frame_len.to_bytes(4, "little"))
            fh.write((0).to_bytes(8, "little")) # presentation timestamp

            # obu_header starts at byte 44:
            # obu_type = OBU_TEMPORAL_DELIMITER = 2
            # obu_has_size_field = 1
            fh.write(b"\x12\x00")

            # Frame extents at byte 46:
            structure = self.mdat.structure
            for extent_offset, extent_length in extents:
                structure.seek(extent_offset, os.SEEK_SET)
                buf = structure.read(extent_length)
                assert len(buf) == extent_length
                fh.write(buf)

if __name__ == "__main__":
    main()
