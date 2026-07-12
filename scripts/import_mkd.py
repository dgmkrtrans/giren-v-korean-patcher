#!/usr/bin/env python3
"""Import rebuilt MKD files back into the original PSP ISO in-place.

This tool safely overwrites the MKD files inside an existing ISO image
without changing its logical sector structure, avoiding LBA shift bugs
that occur when using tools like UMDGen.
"""

import argparse
import os
import struct
import sys

def parse_iso9660(f):
    """Parse ISO9660 directory structure and return a dict of filename -> (LBA, size)."""
    files = {}
    f.seek(16 * 2048)
    pvd = f.read(2048)
    if pvd[1:6] != b'CD001':
        raise ValueError("Not a valid ISO9660 image.")

    # Root directory record is at offset 156 of PVD
    root_dr = pvd[156:156+34]
    root_extent = struct.unpack('<I', root_dr[2:6])[0]
    root_size = struct.unpack('<I', root_dr[10:14])[0]

    def read_dir(extent, size, path):
        f.seek(extent * 2048)
        dir_data = f.read(size)
        offset = 0
        while offset < size:
            length = dir_data[offset]
            if length == 0:
                break
            file_extent = struct.unpack('<I', dir_data[offset+2:offset+6])[0]
            file_size = struct.unpack('<I', dir_data[offset+10:offset+14])[0]
            flags = dir_data[offset+25]
            name_len = dir_data[offset+32]
            name_bytes = dir_data[offset+33:offset+33+name_len]
            
            if name_bytes == b'\x00':
                filename_str = "."
            elif name_bytes == b'\x01':
                filename_str = ".."
            else:
                filename_str = name_bytes.decode('utf-8', errors='ignore').split(';')[0]

            is_dir = bool(flags & 2)
            full_path = f"{path}/{filename_str}" if path else filename_str
            
            if filename_str not in [".", ".."]:
                if not is_dir:
                    files[full_path] = {'lba': file_extent, 'size': file_size}
                else:
                    read_dir(file_extent, file_size, full_path)
            
            offset += length

    read_dir(root_extent, root_size, "")
    return files

def main():
    parser = argparse.ArgumentParser(description="Import rebuilt MKD files into an ISO in-place.")
    parser.add_argument("--iso", default="game-patched.iso", help="Path to the ISO file to modify.")
    parser.add_argument("--mkd-dir", default="rebuilt_mkd", help="Directory containing rebuilt ZZZPSP*.MKD files.")
    
    args = parser.parse_args()

    iso_path = args.iso
    mkd_dir = args.mkd_dir

    if not os.path.exists(iso_path):
        print(f"Error: ISO file '{iso_path}' not found.", file=sys.stderr)
        return 1
    
    if not os.path.exists(mkd_dir):
        print(f"Error: MKD directory '{mkd_dir}' not found.", file=sys.stderr)
        return 1

    try:
        with open(iso_path, 'r+b') as f:
            print(f"Parsing ISO9660 structure from '{iso_path}'...")
            iso_files = parse_iso9660(f)
            
            # Find all MKD files in the input directory
            imported_count = 0
            for mkd_name in sorted(os.listdir(mkd_dir)):
                if not mkd_name.upper().endswith('.MKD'):
                    continue
                
                iso_target_path = f"PSP_GAME/USRDIR/{mkd_name}"
                if iso_target_path not in iso_files:
                    print(f"Warning: {mkd_name} not found in ISO. Skipping.")
                    continue
                
                iso_entry = iso_files[iso_target_path]
                iso_lba = iso_entry['lba']
                iso_size = iso_entry['size']
                
                mkd_path = os.path.join(mkd_dir, mkd_name)
                mkd_size = os.path.getsize(mkd_path)
                
                if mkd_size != iso_size:
                    print(f"Error: Size mismatch for {mkd_name}. ISO expects {iso_size} bytes, but file is {mkd_size} bytes.")
                    print("In-place import requires the exact same file size. Ensure rebuild_mkd.py was run without --relayout.")
                    return 1
                
                print(f"Importing {mkd_name} to LBA {iso_lba} ({mkd_size} bytes)...")
                f.seek(iso_lba * 2048)
                
                # Write in chunks to avoid huge memory usage
                with open(mkd_path, 'rb') as source_f:
                    while True:
                        chunk = source_f.read(8 * 1024 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
                
                imported_count += 1
                
            print(f"\nSuccessfully imported {imported_count} MKD file(s) into '{iso_path}'.")

    except Exception as e:
        print(f"Error processing ISO: {e}", file=sys.stderr)
        return 1

    return 0

if __name__ == '__main__':
    sys.exit(main())
