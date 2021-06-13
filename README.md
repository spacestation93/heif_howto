A recent project required me to understand parsing HEIF files and feeding image bytes to a decompressor. I couldn't find much out there about nuts-and-bolts at this low level. Here's the how-to I wish I'd had.

Why new image formats?
======================

If you found this page you probably know HEIC and AVIF are "new" image formats (standardized in 2015 and 2019, respectively). Both have lossy and lossless modes, so either can be used instead of JPEG or PNG. Both promise better compression -- 50% to 25% smaller files with quality as-good or better -- and other features like HDR, deblocking, or embedded thumbnails.

https://developer.apple.com/videos/play/wwdc2017/513/ is a good presentation from Apple about why they changed iOS's camera default from JPEG to HEIC.

What's HEIF?
============

HEIC and AVIF are examples of a generic format named HEIF (High Efficiency Image File Format; yes, the acronym has fewer F's than its expansion). Someone noticed compressing one frame of video is a lot like compressing a photo if you squint at it and HEIF was the result.

The most significant difference is that HEIC uses the HEVC codec (aka H.265 aka MPEG-H Part 2) and AVIF uses AV1. (Aka... okay, it's just called "AV1". Simplicity was a goal of that standard!)

HEIF, in turn, is based on a container format named ISO BMFF (ISO/IEC Base Media File Format). So to parse HEIF I must first parse BMFF.

ISO BMFF
========

You might know ISO BMFF better as ".MP4" (or MPEG-4 Part 12, or perhaps even ISO/IEC 14496-12). BMFF stores binary blobs and metadata about them inside a single file.

Most files are like data pizzas, with most metadata on top and then a data blob. Sometimes these are followed with additional metadata and data blobs. The metadata is basically a series of key-value maps (or arrays of them), and the data can be anything.

For example, a video file might start with playback parameters like the resolution, then contain alternating pieces of video and audio data (so the player can play both without seeking), and end with metadata listing offsets for various seek times. Or the metadata describing which data bytes are video and which are audio might go after the data so the entire file must be downloaded before a single frame can get played. BMFF doesn't care!

BMFF calls metadata sections "boxes". The official standard document specifies a basic box like this:

```
aligned(8) class Box (unsigned int(32) boxtype,
         optional unsigned int(8)[16] extended_type) {
   unsigned int(32) size;
   unsigned int(32) type = boxtype;
   if (size==1) {
      unsigned int(64) largesize;
   } else if (size==0) {
      // box extends to end of file
   }
   if (boxtype==‘uuid’) {
      unsigned int(8)[16] usertype = extended_type;
   }
}
```

That syntax is MPEG-4 Syntactic Description Language, standardized in ISO/IEC 14496-1. It goes by Flavor (http://flavor.sourceforge.net/) when it's at home.

Anyway, every box starts with a 32-bit size field and then a 32-bit box type name. The size gives the box's total length in bytes -- including the size field itself, so the smallest possible Box is eight bytes long. BMFF takes advantage of this to use `size == 1` (which would otherwise be invalid) as a flag meaning the real size is in a 64-bit field following the type.

Box types are printable strings. Each type extends the base Box by adding additional fields after in the base. Most types are ASCII letters and numbers, although a few other characters are used, especially in older BMFF-based formats.

Most HEIF files start with an "ftyp" box (technically it must be "as early as possible"):

```
aligned(8) class FileTypeBox
   extends Box(‘ftyp’) {
   unsigned int(32) major_brand;
   unsigned int(32) minor_version;
   unsigned int(32) compatible_brands[]; // to end of the box
}
```

Each "brand" is a standard this file complies with. For instance, many files are both HEIC and MIF1 files, but not all HEIC files are MIF1 files or vice-versa -- neither's a sub-type of the other. Thus, many files carry both brands.

While teaching myself how to parse HIEF I translated the spec into simple, inefficient Python code:

```python
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
```

BMFF supports a few complex field types:
* Fields can be arrays.
* Field lengths can depend on other fields' values or mathematical expressions.

Here's a sample array with variable-length values from the spec for storing HEIC decoding parameters:

```
unsigned int(16) numNalus;
for (i=0; i< numNalus; i++) {
   unsigned int(16) nalUnitLength;
   bit(8*nalUnitLength) nalUnit;
}
```

Finally, boxes can contain other boxes. HEIF's "Item Property Container" box contains an "Image Spatial Extents Property" box, and that box lists the image resolution.

Value names aren't stored in files. Parsing anything except box types requires specifications like the above. And that's a problem because while the ISO gives away free downloads for a few of their standards, most cost hundreds of dollars a pop. Parsing HEIF needs five or ten different standards -- or puzzling out code written by someone who already has them.

HEIC images
===========

Minimal HEIF files consist of three boxes:
* "ftyp", with a brand "heic". Should come first.
* "meta", containing many other boxes of (you guessed it) metadata.
* "mdat", the "media data" box. This is the data blob.

heic_unpack.py is an MVP for unpacking data an HEVC decoder can display. The useful part starts with the ftyp box:

```python
ftyp = boxes[0]
if ftyp.type != "ftyp":
    raise ValueError("Missing ftyp", in_path)
brands = ftyp.parse_brands()
if "heic" not in brands:
    raise NotImplementedError("HEIF brand", brands)
```

Having written a box parser one might enthusiastically try passing mdat's contents to a decompressor just to find out what happens. Unfortunately... not much. Three additional steps are needed if you want images instead of error messages.

Step One: Which image should be decoded?
----------------------------------------

HEIF files can contain multiple images (like embedded thumbnails, burst photo mode, or animation).

HIEF has the concept of a "primary item", so let's unpack that.

```python
meta = get_one_box(boxes, "meta")
item_id = get_child_box(meta, "pitm").parse_item_id()
```

"pitm" is the boxtype of the "Primary Item" box. I'll elide most low-level box definitions from here out; feel free to check out the full source for them. The Primary Item box demonstrates a few notable patterns in BMFF, however.

```
unsigned int(8) version = v;
bit(24) flags = f;
if (version == 0) {
   unsigned int(16) item_ID;
} else {
   unsigned int(32) item_ID;
}
```

Like many BMFF boxes, Primary Item has "version" and "flags" fields. The flags are currently always zero, and the version is treated like a flag, specifying how large item_ID may be. This allows shaving a few bytes off the file size and provides future-proofing in case a later standard needs more fields in this box.

Step Two: Image Header
----------------------

Next, decoder parameters like image resolution must be found in the metadata and assembled into a header the decoder can understand.

Each image codec uses different metadata boxes. HIEF has a common way to associate them with image items. The "Item Property Container" box contains all the codec-specific boxes for all items in the file. The "Item Property Association" box maps from item IDs to arrays of property box indexes.

If item ID 1002 is associated with indexes 1 and 3 then that means the first and third boxes under the Item Property Container apply to that item. Properties can be associated with multiple items to save a few dozen bytes.

```python
# Get codec-specific properties from iprp ("item properties") box.
ipma = get_child_box(meta, "ipma").parse_associations()
ipco = get_child_box(meta, "ipco")
property_boxes = [ipco.children[i - 1] for i in ipma[item_id]]
```

If you're curious, the Item Property Association's arrays inside mappings inside a box are a great example of how BMFF can store complex data structures. Whether all of this indirection and future-proofing is actually necessary is another topic...

With the necessary metadata in hand, it's time to parse it and write
out that header!

```python
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
```

"Annex B" is the simplest way to store HEVC frames -- the closest thing HEVC has to a raw file format. I wrote a short helper function for copying data into an Annex B file because it's used while copying data, too.

```python
def copy_data_annex_b(structure, out_fh, length_size):
    length = structure.read_int(length_size)
    out_fh.write(b"\x00\x00\x00\x01")
    out_fh.write(structure.read(length))
```

Step Three: Image Data
----------------------

Finally, it's time to copy the image itself!

Image data gets stored in one or more pieces, called "extents", inside the mdat block. Each extent is described by a starting offset and length. In theory, extents from this image might be interleaved with another item, or even stored out of order!

The "Item Location" box stores a mapping from item ID to an array of offset/length pairs. That gives the byte ranges to copy into the Annex B output.

```python
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
```

Does it work?
=============

Try it yourself!

heic_unpack.py and MP4Box extract the same Annex B data from sample images like https://github.com/nokiatech/heif/blob/gh-pages/content/images/winter_1440x960.heic -- if they're simple enough for heic_unpack.py to understand.

It passes a round-trip smoke-test, too:

If I use FFMPEG and MP4Box to create a lossless HEIF from a PNG...

```
ffmpeg -i test.png -c:v libx265 -x265-params lossless=1 -f hevc test.hevc

MP4Box -add-image test.hevc:primary -ab heic -new test.heic

heic_unpack.py test.heic dumped_by_me.hevc

MP4Box -dump-item 1:path=dumped_by_mp4box.hevc test.heic
```

The Annex B files MP4Box and heic_unpack.py extracted should be
identical (unless MP4Box has changed since I wrote this):

```
sha256sum dumped_by_mp4box.hevc dumped_by_me.hevc

cmp dumped_by_mp4box.hevc dumped_by_me.hevc
```

The RGBA image data should match the original PNG, too. (Headers and such may differ slightly between this and the original HEVC, or if converting it back to a PNG, so I use RGBA to ensure only image data gets compared.)

```
ffmpeg -i test.hevc -vf format=rgba -f rawvideo test.rgba

ffmpeg -i dumped_by_me.hevc -vf format=rgba -f rawvideo dumped_by_me.rgba

sha256sum test.rgba dumped_by_me.rgba

cmp test.rgba dumped_by_me.rgba
```

So my answer is... yes. For a certain value of "work". I parsed only the bare minimum, omitting several validation steps or optimizations production code should include.

Supporting more codecs
======================

heif_unpack.py is a mode modular version of heic_unpack.py demonstrating two additional compression formats.

AVIF images follow the same basic pattern as HEIC. AV1's traditional raw-ish file format is IVF (Indeo video file), and IVF headers can be built with just the image width and height. The Alliance for Open Media even publishes AV1 specifications freely!

AVIC is a rarely-implemented HEIF type, identical to HEIC except that it uses AVC (H.264/MPEG-4 Part 10) instead of HEVC compression.

Other HEIF types
================

Most real-world HEIF images use additional features my simplistic parser doesn't handle. These are mostly straightforward extensions of the basics, however.

For example:
* iOS stores photos as a grid of smaller tiles instead of one giant image. This lets iPhones process tiles in parallel and with fewer resources per tile.
* iOS also adds an embedded thumbnail as a separate item so it needn't re-decode all the photos megapixels every time it scrolls past.
* Burst photos can be saved as a sequence of images with only the differences between each photo stored.
* Like JPEG, HEIF supports saving simple edits like rotation to metadata, without decoding and re-encoding the image and possibly losing quality.

References
==========

<dl>
  <dt>https://standards.iso.org/ittf/PubliclyAvailableStandards</dt>
  <dd>The ISO has free downloads for a small number of relevant standards, including HEIF-specific box specifications (23008-12) but not the older BMFF standards it builds upon (14496-12 and 14496-15).</dd>
  <dt>https://aomediacodec.github.io/av1-spec</dt>
  <dt>https://aomediacodec.github.io/av1-avif</dt>
  <dd>Unlike the ISO and MPEG, AOMedia make their specs widely available.</dd>
  <dt>https://github.com/gpac/gpac/blob/171d15f1913ff9bf0cb586d3482ab79687ed6782/src/isomedia/meta.c#L228</dt>
  <dd>Reading MP4Box's code for unpacking HEIC and AVIC files was enormously helpful.</dd>
  <dt>https://www.ffmpeg.org/doxygen/3.1/ivfenc_8c_source.html</dt>
  <dd>FFMPEG doesn't have full a HEIF implementation yet, but their implementations for other related header formats were useful references.</dd>
  <dt>https://github.com/strukturag/libheif</dt>
  <dd>libheif is what you should probably use instead of writing your own parser, if you have that option.</dd>
</dl>
