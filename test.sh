#!/bin/sh
set -e

check() {
  local heif=$1
  local expected=$2
  for c in ./heic_unpack.py ./heif_unpack.py; do
    check1 $heif $expected $c
  done
}

check1() {
  local heif=$1
  local expected=$2
  local c=$3
  $c $heif test_tmp/py_out
  actual=$(sha1sum test_tmp/py_out | cut -d' ' -f1)
  if [ $actual != $expected ]; then
    echo "FAILED $c $heif"
    exit 1
  fi
}

rm -rf test_tmp
mkdir test_tmp

check test_images/nokia/winter_1440x960.heic c9fece0c7f038b03c081a32c23d5611daf74fdb5
MP4Box -dump-item 1002:path=test_tmp/gpac_out test_images/nokia/winter_1440x960.heic
cmp test_tmp/*

check1 test_images/link-u/kimono.avif be0eb3e0ca2e8088e291e4f85f75333165dc7720 ./heif_unpack.py

rm -rf test_tmp
