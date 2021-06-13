#!/usr/bin/python3
"""
Extract code blocks from README.md to generate heic_unpack.py
so I don't accidentally let them fall out of sync.

I guess the cool kids use "notebooks" now instead of LP?
"""

TEMPLATE = '''\
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
    {1}

    {2}

    {3}

    {4}

    {6}

{5}

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
'''

def main():
    code_sections = []
    in_code = False
    code_lines = []
    for l in open("README.md"):
        if in_code:
            if l.startswith("```"):
                if code_lines[0].startswith("def "):
                    indent = ""
                else:
                    indent = " " * 4
                code_section = indent.join(code_lines)
                code_section = code_section.strip()
                code_section = code_section.replace(
                    ("\n" + indent + "\n"),
                    "\n\n")
                code_sections.append(code_section)
                code_lines.clear()
                in_code = False
            else:
                code_lines.append(l)
        elif l.startswith("```python"):
            in_code = True

    py = TEMPLATE.format(*code_sections)
    with open("heic_unpack.py", "w") as fh:
        fh.write(py)

if __name__ == "__main__":
    main()
