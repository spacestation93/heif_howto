#!/usr/bin/python3
import sys

import box_parts

def dump_container(fh, boxes):
    def visit(fh, box, level):
        fh.write(" " * level)
        fh.write(str(box))
        fh.write("\n")
        if not box.children:
            fh.write(" " * (level + 1))
            fh.write(repr(box.get_payload()))
            fh.write("\n")
        for child in box.children:
            visit(fh, child, level=(level + 1))
    for box in boxes:
        visit(fh, box, 0)

def main():
    path = sys.argv[1]
    boxes = box_parts.parse_path(path)
    dump_container(sys.stdout, boxes)

if __name__ == "__main__":
    main()
