#!/usr/bin/env python3
"""
明星三缺一 2002 (Celebrity Mahjong 3-Missing-1 2002) - Asset Extractor
Developer: IGS (鈊象電子)

Handles two custom IGS container formats:
  - PCDATA01: Graphics/animation container (PCX_LZSS, TGA_LZSS, BMP_LZSS, PK0_LZSS, PALETTEs, ACT data)
  - IGSROM01: Audio container (WAVEDATA chunks, each is a raw RIFF/WAV file)

Also handles:
  - font_*.rom: Raw bitmap font data (no header, raw glyph bitmaps)
  - IGSMJ_P.SAV: Game save/settings file (IGSMJP07SETTING header)
  - Standard PE executables and DLLs (igsmj2.exe, server.exe, ace.dll, etc.)
  - Standard BMP, TGA, PCX, FLIC, WAV files (already in correct format)

LZSS Compression (used in all _LZSS chunk types):
  - Flag byte: bit set (1) = literal byte follows; bit clear (0) = back-reference
  - Back-reference: 2 bytes; offset = byte1 | ((byte2 & 0xF0) << 4); length = (byte2 & 0x0F) + 3
  - Sliding window: 4096 bytes, initialized to 0x20 (space), write position starts at 0xFEE
  - Pixel formats:
      PCX_LZSS → 8-bit indexed (1 byte/pixel); header: [uint16 w][uint16 h]
      TGA_LZSS → 16-bit ARGB1555 (2 bytes/pixel, bit15=alpha); header: [uint16 w][uint16 h]
      BMP_LZSS → 16-bit RGB555 (2 bytes/pixel, full background); header: [uint16 w][uint16 h]
      PK0_LZSS → 8-bit indexed sparse RLE sprite; header: [uint16 w][uint16 h][uint32 decompressed_size]
                 After LZSS decompression: row-by-row sparse RLE where each byte is either:
                   bit7=1 → skip (byte & 0x7f) transparent pixels (x advances, no data)
                   bit7=0 → copy next (byte) opaque palette-indexed pixels from stream
                 Rows complete when x reaches w; used for large character sprites in STAR_*.rom files.

PCDATA01 chunk types:
  BASEDATA  (12 bytes): Container metadata: num_palettes(u32), count(u32), extra(u32)
  ACTINDEX  (2 bytes):  Animation set count (uint16)
  PALETTE1  (768 bytes): 256-color RGB palette, 3 bytes per entry
  PCX_LZSS  (variable): LZSS-compressed 8-bit indexed sprite
  TGA_LZSS  (variable): LZSS-compressed 16-bit ARGB1555 sprite
  BMP_LZSS  (variable): LZSS-compressed 16-bit RGB555 full background
  PK0_LZSS  (variable): LZSS-compressed sparse RLE 8-bit indexed character sprite
  ACT_DATA  (44 bytes): Animation definition header
  ACT_STEP  (10 or 20 bytes): Animation frame/step data
  ACT_POOL  (10 bytes): Animation pool entry
  ACTBLOCK  (0 bytes):  End-of-animation-block sentinel

IGSROM01 chunk types:
  WAVEDATA (variable): Raw RIFF/WAV audio file

Usage:
    python3 igsmj2_extractor.py <input_file_or_directory> [output_directory]

Examples:
    python3 igsmj2_extractor.py play/mj.rom output/mj/
    python3 igsmj2_extractor.py menu/king.rom output/king/
    python3 igsmj2_extractor.py role/STAR_D00.rom output/STAR_D00/
    python3 igsmj2_extractor.py . output/all/
"""

import os
import sys
import struct
from pathlib import Path

try:
    from PIL import Image as _PILImage
except ImportError:
    raise SystemExit(
        "Pillow is required. Install it with:  pip install Pillow"
    )


# ─── LZSS Decompressor ────────────────────────────────────────────────────────

def lzss_decompress(data: bytes, start: int = 4, max_output: int = None) -> bytes:
    """
    Decompress IGS LZSS-compressed data.
    
    Format:
      - Flag byte: bits 0-7, each bit governs one token
          bit set   → literal byte follows (copy directly to output)
          bit clear → back-reference: next 2 bytes encode (offset, length)
      - Back-reference encoding (2 bytes b1, b2):
          offset = b1 | ((b2 & 0xF0) << 4)   (12-bit window offset)
          length = (b2 & 0x0F) + 3            (match length 3..18)
      - Sliding window: 4096 bytes, initialized to 0x20 (space char), write pos starts at 0xFEE
    
    Args:
      data:       raw chunk bytes (including any header before start)
      start:      offset into data where compressed stream begins (default 4, after w+h header)
      max_output: stop decompressing after this many output bytes (None = decompress all input)
    """
    output = bytearray()
    window = bytearray(b'\x20' * 4096)   # initialized to 0x20 (space), NOT zero
    win_pos = 0xFEE                       # write position starts at 0xFEE, NOT 0
    i = start

    while i < len(data):
        if max_output is not None and len(output) >= max_output:
            break
        flags = data[i]
        i += 1

        for bit in range(8):
            if i >= len(data):
                break
            if max_output is not None and len(output) >= max_output:
                break
            if flags & (1 << bit):
                # Literal byte
                c = data[i]
                i += 1
                output.append(c)
                window[win_pos] = c
                win_pos = (win_pos + 1) & 0xFFF  # % 4096
            else:
                # Back-reference
                if i + 1 >= len(data):
                    break
                b1 = data[i]
                b2 = data[i + 1]
                i += 2
                offset = b1 | ((b2 & 0xF0) << 4)
                length = (b2 & 0x0F) + 3
                for j in range(length):
                    if max_output is not None and len(output) >= max_output:
                        break
                    c = window[(offset + j) & 0xFFF]
                    output.append(c)
                    window[win_pos] = c
                    win_pos = (win_pos + 1) & 0xFFF

    return bytes(output)


# ─── Image Conversion ─────────────────────────────────────────────────────────

def palette_to_list(palette_data: bytes) -> list:
    """Convert 768-byte palette to list of (R, G, B) tuples."""
    colors = []
    for i in range(256):
        r = palette_data[i * 3]
        g = palette_data[i * 3 + 1]
        b = palette_data[i * 3 + 2]
        colors.append((r, g, b))
    
    # Swap index 0 and 255 colors
    if len(colors) == 256:
        colors[0], colors[255] = colors[255], colors[0]
        
    return colors


def indexed_to_png(pixels: bytes, width: int, height: int,
                   palette: list, out_path: str):
    """Save 8-bit indexed image as PNG using Pillow."""
    img = _PILImage.new('P', (width, height))
    img.putdata(pixels)
    flat_palette = []
    for r, g, b in palette:
        flat_palette.extend([r, g, b])
    while len(flat_palette) < 768:
        flat_palette.extend([0, 0, 0])
    img.putpalette(flat_palette[:768])
    img.save(out_path)


def rgb555_to_png(pixels: bytes, width: int, height: int, out_path: str,
                  has_alpha: bool = False):
    """
    Convert 16-bit RGB555 or ARGB1555 pixel data to PNG.
    Format: bit15=alpha(if has_alpha), bits14-10=R, bits9-5=G, bits4-0=B
    Each channel 5 bits → scale to 8 bits by: val * 255 // 31
    """
    img = _PILImage.new('RGBA' if has_alpha else 'RGB', (width, height))
    pixel_list = []
    for i in range(0, len(pixels) - 1, 2):
        val = pixels[i] | (pixels[i + 1] << 8)
        r = ((val >> 10) & 0x1F) * 255 // 31
        g = ((val >> 5) & 0x1F) * 255 // 31
        b = (val & 0x1F) * 255 // 31
        if has_alpha:
            a = 255 if (val & 0x8000) else 0
            pixel_list.append((r, g, b, a))
        else:
            pixel_list.append((r, g, b))
    img.putdata(pixel_list)
    img.save(out_path)


def pk0_decode(rle_data: bytes, width: int, height: int) -> bytes:
    """
    Decode PK0 sparse RLE pixel data to raw 8-bit indexed pixels (width*height bytes).
    
    Encoding (per row, until x reaches width):
      - Each control byte:
          bit7 = 1 → skip (byte & 0x7f) transparent pixels (x advances, no palette index stored)
          bit7 = 0 → copy next (byte) palette-indexed bytes from stream as opaque pixels
      - Rows end when accumulated x count reaches width.
    Transparent pixels are filled with index 0 in the output.
    """
    out = bytearray(width * height)   # all transparent (index 0) by default
    src = 0
    n = len(rle_data)
    for row in range(height):
        x = 0
        while x < width and src < n:
            ctrl = rle_data[src]; src += 1
            if ctrl & 0x80:
                # Transparent skip
                x += ctrl & 0x7F
            else:
                # Opaque run: next `ctrl` bytes are palette indices
                count = ctrl
                for i in range(count):
                    if x < width and src < n:
                        out[row * width + x] = rle_data[src]; src += 1
                    x += 1
    return bytes(out)


def pk0_lzss_to_png(chunk_data: bytes, palette: list, out_path: str):
    """
    Decode and save a PK0_LZSS chunk as RGBA PNG.
    
    Chunk layout: [uint16 w][uint16 h][uint32 decompressed_size][LZSS compressed data...]
    After LZSS decompression: sparse RLE pixel data (see pk0_decode).
    Palette index 0 is treated as transparent (alpha=0).
    """
    w = struct.unpack_from('<H', chunk_data, 0)[0]
    h = struct.unpack_from('<H', chunk_data, 2)[0]
    decomp_size = struct.unpack_from('<I', chunk_data, 4)[0]
    if w == 0 or h == 0:
        return
    rle_data = lzss_decompress(chunk_data, start=8, max_output=decomp_size)
    pixels_indexed = pk0_decode(rle_data, w, h)
    img = _PILImage.new('RGBA', (w, h))
    pixel_list = []
    for idx in pixels_indexed:
        r, g, b = palette[idx] if idx < len(palette) else (0, 0, 0)
        a = 0 if idx == 0 else 255   # index 0 = transparent
        pixel_list.append((r, g, b, a))
    img.putdata(pixel_list)
    img.save(out_path)


# ─── Chunk Parser ─────────────────────────────────────────────────────────────

def parse_chunks(data: bytes) -> list:
    """
    Parse all chunks from a PCDATA01 or IGSROM01 file body.
    Returns list of (name, offset_of_data, data_bytes).
    The file begins with a magic header (12 bytes) that is skipped.
    """
    chunks = []
    offset = 12  # skip magic header (8 bytes magic + 4 bytes null)

    while offset + 12 <= len(data):
        chunk_name_raw = data[offset:offset + 8]
        try:
            chunk_name = chunk_name_raw.decode('ascii').rstrip('\x00 ')
        except UnicodeDecodeError:
            break

        if not chunk_name or not all(c.isalnum() or c == '_' for c in chunk_name):
            break

        chunk_size = struct.unpack_from('<I', data, offset + 8)[0]
        if offset + 12 + chunk_size > len(data):
            break

        chunk_data = data[offset + 12: offset + 12 + chunk_size]
        chunks.append((chunk_name, offset + 12, chunk_data))
        offset += 12 + chunk_size

    return chunks


# ─── IGSROM01 Extractor ───────────────────────────────────────────────────────

def extract_igsrom01(data: bytes, out_dir: str, base_name: str) -> dict:
    """
    Extract WAV files from an IGSROM01 container.
    Each WAVEDATA chunk is a complete RIFF/WAV file.
    Returns summary dict.
    """
    os.makedirs(out_dir, exist_ok=True)
    chunks = parse_chunks(data)
    wav_count = 0
    results = []

    for name, data_offset, chunk_data in chunks:
        if name == 'WAVEDATA':
            # Verify it starts with RIFF
            if len(chunk_data) >= 4 and chunk_data[:4] == b'RIFF':
                out_path = os.path.join(out_dir, f'{base_name}_{wav_count:03d}.wav')
                with open(out_path, 'wb') as f:
                    f.write(chunk_data)
                riff_size = struct.unpack_from('<I', chunk_data, 4)[0]
                results.append({
                    'index': wav_count,
                    'file': out_path,
                    'size': len(chunk_data),
                    'riff_size': riff_size
                })
                wav_count += 1

    return {'format': 'IGSROM01', 'wav_count': wav_count, 'files': results}


# ─── PCDATA01 Extractor ───────────────────────────────────────────────────────

def extract_pcdata01(data: bytes, out_dir: str, base_name: str) -> dict:
    """
    Extract all assets from a PCDATA01 container.
    Returns summary dict describing what was found and extracted.
    """
    os.makedirs(out_dir, exist_ok=True)
    chunks = parse_chunks(data)

    palettes = []
    pcx_images = []
    tga_images = []
    bmp_images = []
    pk0_images = []
    act_data_list = []
    act_steps = []
    act_pools = []
    basedata_info = None
    actindex_val = None

    pcx_idx = 0
    tga_idx = 0
    bmp_idx = 0
    pk0_idx = 0
    act_idx = 0
    step_idx = 0
    pool_idx = 0

    # First pass: collect all palettes
    for name, data_offset, chunk_data in chunks:
        if name == 'PALETTE1' and len(chunk_data) == 768:
            palettes.append(palette_to_list(chunk_data))

    # Use first palette as default
    default_palette = palettes[0] if palettes else [(i, i, i) for i in range(256)]

    # Save palettes as .pal files
    for i, pal in enumerate(palettes):
        pal_path = os.path.join(out_dir, f'{base_name}_palette_{i:02d}.pal')
        with open(pal_path, 'wb') as f:
            for r, g, b in pal:
                f.write(bytes([r, g, b]))

    # Second pass: process all other chunks
    for name, data_offset, chunk_data in chunks:

        if name == 'BASEDATA' and len(chunk_data) == 12:
            num_pal = struct.unpack_from('<I', chunk_data, 0)[0]
            count   = struct.unpack_from('<I', chunk_data, 4)[0]
            extra   = struct.unpack_from('<I', chunk_data, 8)[0]
            basedata_info = {'num_palettes': num_pal, 'count': count, 'extra': extra}

        elif name == 'ACTINDEX' and len(chunk_data) == 2:
            actindex_val = struct.unpack_from('<H', chunk_data, 0)[0]

        elif name == 'PCX_LZSS' and len(chunk_data) >= 4:
            w = struct.unpack_from('<H', chunk_data, 0)[0]
            h = struct.unpack_from('<H', chunk_data, 2)[0]
            if w > 0 and h > 0:
                pixels = lzss_decompress(chunk_data, start=4)
                expected = w * h
                if len(pixels) >= expected:
                    pixels = pixels[:expected]
                out_path = os.path.join(out_dir, f'{base_name}_pcx_{pcx_idx:04d}_{w}x{h}.png')
                indexed_to_png(pixels, w, h, default_palette, out_path)
                pcx_images.append({
                    'index': pcx_idx, 'width': w, 'height': h,
                    'compressed_size': len(chunk_data), 'decompressed_size': len(pixels),
                    'file': out_path
                })
            pcx_idx += 1

        elif name == 'TGA_LZSS' and len(chunk_data) >= 4:
            w = struct.unpack_from('<H', chunk_data, 0)[0]
            h = struct.unpack_from('<H', chunk_data, 2)[0]
            if w > 0 and h > 0:
                pixels = lzss_decompress(chunk_data, start=4)
                expected = w * h * 2
                if len(pixels) >= expected:
                    pixels = pixels[:expected]
                out_path = os.path.join(out_dir, f'{base_name}_tga_{tga_idx:04d}_{w}x{h}.png')
                rgb555_to_png(pixels, w, h, out_path, has_alpha=True)
                tga_images.append({
                    'index': tga_idx, 'width': w, 'height': h,
                    'compressed_size': len(chunk_data), 'decompressed_size': len(pixels),
                    'file': out_path
                })
            tga_idx += 1

        elif name == 'BMP_LZSS' and len(chunk_data) >= 4:
            w = struct.unpack_from('<H', chunk_data, 0)[0]
            h = struct.unpack_from('<H', chunk_data, 2)[0]
            if w > 0 and h > 0:
                pixels = lzss_decompress(chunk_data, start=4)
                expected = w * h * 2
                if len(pixels) >= expected:
                    pixels = pixels[:expected]
                out_path = os.path.join(out_dir, f'{base_name}_bmp_{bmp_idx:04d}_{w}x{h}.png')
                rgb555_to_png(pixels, w, h, out_path, has_alpha=False)
                bmp_images.append({
                    'index': bmp_idx, 'width': w, 'height': h,
                    'compressed_size': len(chunk_data), 'decompressed_size': len(pixels),
                    'file': out_path
                })
            bmp_idx += 1

        elif name == 'PK0_LZSS' and len(chunk_data) >= 8:
            w = struct.unpack_from('<H', chunk_data, 0)[0]
            h = struct.unpack_from('<H', chunk_data, 2)[0]
            decomp_size = struct.unpack_from('<I', chunk_data, 4)[0]
            if w > 0 and h > 0:
                out_path = os.path.join(out_dir, f'{base_name}_pk0_{pk0_idx:04d}_{w}x{h}.png')
                pk0_lzss_to_png(chunk_data, default_palette, out_path)
                pk0_images.append({
                    'index': pk0_idx, 'width': w, 'height': h,
                    'compressed_size': len(chunk_data),
                    'decompressed_size': decomp_size,
                    'file': out_path
                })
            pk0_idx += 1

        elif name == 'ACT_DATA' and len(chunk_data) == 44:
            act_info = {
                'index': act_idx,
                'raw': chunk_data.hex(),
                'fields': [struct.unpack_from('<I', chunk_data, i*4)[0] for i in range(11)]
            }
            act_data_list.append(act_info)
            act_idx += 1

        elif name == 'ACT_STEP':
            steps_count = len(chunk_data) // 10
            for s in range(steps_count):
                step_data = chunk_data[s*10:(s+1)*10]
                act_steps.append({
                    'index': step_idx,
                    'raw': step_data.hex(),
                    'fields': list(step_data)
                })
                step_idx += 1

        elif name == 'ACT_POOL' and len(chunk_data) == 10:
            act_pools.append({
                'index': pool_idx,
                'raw': chunk_data.hex(),
                'fields': list(chunk_data)
            })
            pool_idx += 1

    # Write animation metadata as JSON
    import json
    anim_meta = {
        'basedata': basedata_info,
        'actindex': actindex_val,
        'palette_count': len(palettes),
        'pcx_sprite_count': len(pcx_images),
        'tga_sprite_count': len(tga_images),
        'bmp_background_count': len(bmp_images),
        'pk0_sprite_count': len(pk0_images),
        'animation_count': len(act_data_list),
        'animations': act_data_list,
        'steps': act_steps,
        'pools': act_pools,
    }
    meta_path = os.path.join(out_dir, f'{base_name}_metadata.json')
    with open(meta_path, 'w', encoding='utf-8') as f:
        json.dump(anim_meta, f, indent=2)

    return {
        'format': 'PCDATA01',
        'palettes': len(palettes),
        'pcx_images': pcx_images,
        'tga_images': tga_images,
        'bmp_images': bmp_images,
        'pk0_images': pk0_images,
        'animations': len(act_data_list),
        'metadata_file': meta_path
    }


# ─── Font Extractor ───────────────────────────────────────────────────────────

def analyze_font(data: bytes, filename: str) -> dict:
    """
    Analyze raw bitmap font data (font_a24.rom, font_e24.rom, font_l24.rom).
    
    The files contain raw glyph bitmaps with no file header.
    Naming convention: font_X24.rom where X is encoding and 24 is pixel height.
      - font_a24.rom: ASCII glyphs, 24px height, 8px wide  → 24 bytes/glyph
        12288 bytes / 24 bytes = 512 glyphs
      - font_e24.rom: Extended/English glyphs, 24px height, 16px wide → 48 bytes/glyph
        29376 bytes / 48 bytes = 612 glyphs (but 29376/24=1224 if 8-wide)
      - font_l24.rom: Large CJK charset (Chinese), 24px height, 24px wide → 72 bytes/glyph
        942768 bytes / 72 bytes = 13094 glyphs (Big5 Chinese)
    """
    size = len(data)
    base = os.path.splitext(os.path.basename(filename))[0]

    info = {
        'filename': filename,
        'size': size,
    }

    if 'font_a24' in base:
        # 8×24 pixel glyphs, 1 bit per pixel, 8 pixels/byte → 1 byte/row × 24 rows = 24 bytes/glyph
        glyph_size = 24
        info.update({'glyph_width': 8, 'glyph_height': 24, 'glyph_bytes': glyph_size,
                     'glyph_count': size // glyph_size, 'encoding': 'ASCII (1-byte)'})
    elif 'font_e24' in base:
        # Possibly 16×24 pixel glyphs, 2 bytes/row × 24 rows = 48 bytes/glyph
        glyph_size_16 = 48
        glyph_size_8 = 24
        if size % glyph_size_16 == 0:
            info.update({'glyph_width': 16, 'glyph_height': 24, 'glyph_bytes': glyph_size_16,
                         'glyph_count': size // glyph_size_16, 'encoding': 'Extended (1-byte wide)'})
        else:
            info.update({'glyph_width': 8, 'glyph_height': 24, 'glyph_bytes': glyph_size_8,
                         'glyph_count': size // glyph_size_8, 'encoding': 'Extended (1-byte)'})
    elif 'font_l24' in base:
        # 24×24 pixel CJK glyphs, 3 bytes/row × 24 rows = 72 bytes/glyph
        glyph_size = 72
        info.update({'glyph_width': 24, 'glyph_height': 24, 'glyph_bytes': glyph_size,
                     'glyph_count': size // glyph_size, 'encoding': 'Big5 CJK (2-byte)'})

    return info


# ─── Save File Parser ─────────────────────────────────────────────────────────

def parse_save_file(data: bytes) -> dict:
    """
    Parse the IGSMJ_P.SAV game save/settings file.
    Magic: IGSMJP07SETTING (16 bytes)
    """
    if not data[:8].startswith(b'IGSMJP'):
        return {'error': 'Not an IGSMJP save file'}

    magic = data[:16].decode('ascii', errors='replace').rstrip('\x00 ')
    version = data[6:8].decode('ascii', errors='replace')

    return {
        'magic': magic,
        'version': version,
        'size': len(data),
        'raw_hex_preview': data[16:64].hex(),
        'note': 'Settings/save data. Version field: ' + version
    }


# ─── Main Dispatcher ──────────────────────────────────────────────────────────

def detect_format(data: bytes, filename: str) -> str:
    """Detect the format of a file based on magic bytes."""
    magic8 = data[:8] if len(data) >= 8 else data

    if magic8 == b'PCDATA01':
        return 'PCDATA01'
    elif magic8 == b'IGSROM01':
        return 'IGSROM01'
    elif data[:4] == b'RIFF' and data[8:12] == b'WAVE':
        return 'WAV'
    elif data[:4] in (b'BM', ) and len(data) > 2:
        if data[:2] == b'BM':
            return 'BMP'
    elif data[:4] == b'\x11\xAF' or (len(data) > 1 and data[0] == 0x11 and data[1] in (0xAF, 0x12)):
        return 'FLIC'
    elif data[:2] == b'MZ':
        return 'PE_EXECUTABLE'
    elif data[:8].startswith(b'IGSMJP'):
        return 'IGSMJP_SAVE'
    elif all(b == 0 for b in data[:16]) and 'font' in filename.lower():
        return 'RAW_FONT'
    elif filename.lower().endswith('.tga'):
        return 'TGA'
    elif filename.lower().endswith('.pcx'):
        return 'PCX'
    elif filename.lower().endswith('.wav') or filename.lower().endswith('.hk') or filename.lower().endswith('.tw'):
        # wav/homemenu.hk etc. are IGSROM01
        if magic8 == b'IGSROM01':
            return 'IGSROM01'
    return 'UNKNOWN'


def extract_file(input_path: str, output_dir: str, verbose: bool = True) -> dict:
    """
    Extract assets from a single file.
    Auto-detects format and routes to appropriate extractor.
    """
    input_path = Path(input_path)
    if not input_path.exists():
        return {'error': f'File not found: {input_path}'}

    with open(input_path, 'rb') as f:
        data = f.read()

    filename = input_path.name
    base_name = input_path.stem
    fmt = detect_format(data, filename)

    out_dir = os.path.join(output_dir, base_name)

    if verbose:
        print(f"[{fmt}] {filename} ({len(data):,} bytes)")

    if fmt == 'PCDATA01':
        result = extract_pcdata01(data, out_dir, base_name)
        if verbose:
            pk0_count = len(result.get('pk0_images', []))
            pk0_str = f", {pk0_count} PK0 sprites" if pk0_count else ""
            print(f"  → {result['palettes']} palettes, "
                  f"{len(result['pcx_images'])} PCX sprites, "
                  f"{len(result['tga_images'])} TGA sprites, "
                  f"{len(result['bmp_images'])} BMP backgrounds"
                  f"{pk0_str}, "
                  f"{result['animations']} animations")

    elif fmt == 'IGSROM01':
        result = extract_igsrom01(data, out_dir, base_name)
        if verbose:
            print(f"  → {result['wav_count']} WAV files extracted")

    elif fmt == 'RAW_FONT':
        info = analyze_font(data, str(input_path))
        result = {'format': 'RAW_FONT', 'info': info}
        if verbose:
            print(f"  → Raw bitmap font: {info.get('glyph_count','?')} glyphs, "
                  f"{info.get('glyph_width','?')}×{info.get('glyph_height','?')}px, "
                  f"{info.get('encoding','unknown')}")

    elif fmt == 'IGSMJP_SAVE':
        info = parse_save_file(data)
        result = {'format': 'IGSMJP_SAVE', 'info': info}
        if verbose:
            print(f"  → Save file: {info.get('magic','')} (version {info.get('version','')})")

    elif fmt in ('WAV', 'BMP', 'TGA', 'PCX', 'FLIC'):
        result = {'format': fmt, 'note': 'Already in standard format, no extraction needed'}
        if verbose:
            print(f"  → Standard {fmt} file, no extraction needed")

    elif fmt == 'PE_EXECUTABLE':
        result = {'format': 'PE_EXECUTABLE', 'note': 'Windows PE executable or DLL'}
        if verbose:
            print(f"  → Windows PE executable/DLL")

    else:
        result = {'format': 'UNKNOWN', 'size': len(data),
                  'magic': data[:16].hex()}
        if verbose:
            print(f"  → Unknown format, magic: {data[:16].hex()}")

    result['source_file'] = str(input_path)
    result['format_detected'] = fmt
    return result


def extract_directory(input_dir: str, output_dir: str, verbose: bool = True) -> list:
    """Extract all game assets from a directory recursively."""
    results = []
    input_dir = Path(input_dir)

    for root, dirs, files in os.walk(input_dir):
        # Skip puzzle subdirectory as PCX files there are already extracted
        dirs[:] = [d for d in dirs if d != 'puzzle']
        for fname in sorted(files):
            fpath = Path(root) / fname
            rel = fpath.relative_to(input_dir)
            out_subdir = os.path.join(output_dir, str(rel.parent))
            try:
                result = extract_file(str(fpath), out_subdir, verbose=verbose)
                results.append(result)
            except Exception as e:
                print(f"  ERROR processing {fpath}: {e}")
                results.append({'source_file': str(fpath), 'error': str(e)})

    return results


# ─── CLI Entry Point ──────────────────────────────────────────────────────────

def print_format_reference():
    print(__doc__)


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 igsmj2_extractor.py <input> [output_dir]")
        print("       python3 igsmj2_extractor.py --help")
        sys.exit(1)

    if sys.argv[1] in ('--help', '-h', 'help'):
        print_format_reference()
        sys.exit(0)

    input_path = sys.argv[1]
    output_dir = sys.argv[2] if len(sys.argv) > 2 else 'extracted'

    if os.path.isdir(input_path):
        print(f"Extracting directory: {input_path} → {output_dir}/")
        results = extract_directory(input_path, output_dir)
        total = len(results)
        errors = sum(1 for r in results if 'error' in r)
        print(f"\nDone: {total} files processed, {errors} errors.")
    elif os.path.isfile(input_path):
        result = extract_file(input_path, output_dir)
        if 'error' in result:
            print(f"Error: {result['error']}")
            sys.exit(1)
    else:
        print(f"Error: {input_path} not found")
        sys.exit(1)


if __name__ == '__main__':
    main()
