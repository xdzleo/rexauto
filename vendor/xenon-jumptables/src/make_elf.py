"""
Wrap a raw Xenon code image in a minimal 32-bit big-endian PowerPC ELF so IDA
loads it at the right virtual address with the right processor, no prompts.

A 32-bit ELF (ELFCLASS32) is used on purpose: a 64-bit container makes IDA
sign-extend 0x82.. addresses, which breaks reading the jump tables.

Usage:
    python make_elf.py image.bin image.elf --base 0x82000000 [--entry 0x82080000]
"""
import argparse
import struct


def make_elf(data: bytes, vaddr: int, entry: int, text_start=None, text_end=None) -> bytes:
    POFF = 0x1000
    end = vaddr + len(data)
    # Segment plan. Default (no text range): one RWX PT_LOAD over the whole image
    # (back-compat with jump-table recovery). With a text range: split into
    # data(R) / code(RX) / data(R) so IDA gets a real code/data boundary and does
    # NOT disassemble rodata/vtables as code -- this is what makes IDA's data-xref
    # / vtable-target discovery reliable for the deep-extraction pass.
    R, RX, RWX = 4, 5, 7
    if text_start is not None and text_end is not None:
        text_start = max(text_start, vaddr)
        text_end = min(text_end, end)
        segs = []  # (vstart, vend, flags)
        if text_start > vaddr:
            segs.append((vaddr, text_start, R))         # pre-text data (imports, some rodata/vtables)
        segs.append((text_start, text_end, RX))         # code
        if text_end < end:
            segs.append((text_end, end, R))             # post-text rodata/data
    else:
        segs = [(vaddr, end, RWX)]

    e_ident = b"\x7fELF" + bytes([1, 2, 1, 0]) + b"\x00" * 8  # ELFCLASS32, ELFDATA2MSB
    eh = e_ident
    eh += struct.pack(">H", 2)              # e_type  = ET_EXEC
    eh += struct.pack(">H", 20)             # e_machine = EM_PPC
    eh += struct.pack(">I", 1)              # e_version
    eh += struct.pack(">I", entry)          # e_entry
    eh += struct.pack(">I", 52)             # e_phoff
    eh += struct.pack(">I", 0)              # e_shoff
    eh += struct.pack(">I", 0)              # e_flags
    eh += struct.pack(">H", 52)             # e_ehsize
    eh += struct.pack(">H", 32)             # e_phentsize
    eh += struct.pack(">H", len(segs))      # e_phnum
    eh += struct.pack(">H", 0)              # e_shentsize
    eh += struct.pack(">H", 0)              # e_shnum
    eh += struct.pack(">H", 0)              # e_shstrndx
    ph = b""
    for (vs, ve, flags) in segs:
        # p_offset - p_vaddr = POFF - vaddr (constant) -> alignment mod 0x1000 preserved
        ph += struct.pack(">I", 1)                 # p_type = PT_LOAD
        ph += struct.pack(">I", POFF + (vs - vaddr))  # p_offset
        ph += struct.pack(">I", vs)                # p_vaddr
        ph += struct.pack(">I", vs)                # p_paddr
        ph += struct.pack(">I", ve - vs)           # p_filesz
        ph += struct.pack(">I", ve - vs)           # p_memsz
        ph += struct.pack(">I", flags)             # p_flags
        ph += struct.pack(">I", 0x1000)            # p_align
    out = eh + ph
    out += b"\x00" * (POFF - len(out))
    out += data
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("image", help="raw code image dump")
    ap.add_argument("elf", help="output ELF path")
    ap.add_argument("--base", required=True, help="image virtual base, e.g. 0x82000000")
    ap.add_argument("--entry", default=None, help="entry point (default: base + 0x80000)")
    ap.add_argument("--text-start", default=None,
                    help="code region start; with --text-end, splits code(RX)/data(R) so IDA "
                         "won't disassemble rodata as code (improves vtable-xref detection)")
    ap.add_argument("--text-end", default=None, help="code region end (exclusive)")
    args = ap.parse_args()

    base = int(args.base, 0)
    if base % 0x1000 != 0:
        raise SystemExit("--base must be 0x1000-aligned (got 0x%08X)" % base)
    entry = int(args.entry, 0) if args.entry else base + 0x80000
    ts = int(args.text_start, 0) if args.text_start else None
    te = int(args.text_end, 0) if args.text_end else None
    data = open(args.image, "rb").read()
    open(args.elf, "wb").write(make_elf(data, base, entry, ts, te))
    seg = "code/data split" if (ts and te) else "single RWX"
    print("wrote %s (%d bytes, base 0x%08X, %s)" % (args.elf, len(data) + 0x1000, base, seg))


if __name__ == "__main__":
    main()
