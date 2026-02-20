# Copyright (c) 2023 Molodos
# The ElegooNeptuneThumbnails plugin is released under the terms of the AGPLv3 or higher.

"""Color-picture encoder used by the LCD thumbnail pipeline.

Important:
- This module is performance/compatibility sensitive.
- Formatting and comments were improved, but the algorithm and value flow
  are intentionally preserved.
"""

MAX_COLORS = 1024
COLPIC_HEADER_SIZE = 32

MASK_5 = 0x1F
MASK_6 = 0x3F
MASK_8 = 0xFF
MASK_16 = 0xFFFF
MASK_24 = 0xFFFFFF
MASK_32 = 0xFFFFFFFF

BACKSLASH_CODE = ord("\\")
TILDE_CODE = ord("~")


def ColPic_EncodeStr(fromcolor16, picw, pich, outputdata: bytearray, outputmaxtsize, colorsmax):
    """Encode color data, then convert packed bytes into LCD-specific ASCII-ish stream."""
    if outputmaxtsize <= 0 or len(outputdata) == 0:
        return 0
    outputmaxtsize = min(outputmaxtsize, len(outputdata))

    temp_bytes = bytearray(4)

    qty = ColPicEncode(fromcolor16, picw, pich, outputdata, outputmaxtsize, colorsmax)
    if qty == 0:
        return 0

    # Pad to 3-byte groups for 4/3 expansion.
    pad = (3 - (qty % 3)) % 3
    while pad > 0 and qty < outputmaxtsize:
        outputdata[qty] = 0
        qty += 1
        pad -= 1

    encoded_len = (qty * 4) // 3
    if encoded_len >= outputmaxtsize:
        return 0

    hexindex = qty
    strindex = encoded_len
    while hexindex > 0:
        hexindex -= 3
        strindex -= 4

        temp_bytes[0] = outputdata[hexindex] >> 2
        temp_bytes[1] = outputdata[hexindex] & 3
        temp_bytes[1] <<= 4
        temp_bytes[1] += outputdata[hexindex + 1] >> 4
        temp_bytes[2] = outputdata[hexindex + 1] & 15
        temp_bytes[2] <<= 2
        temp_bytes[2] += outputdata[hexindex + 2] >> 6
        temp_bytes[3] = outputdata[hexindex + 2] & 63

        # Custom encoding base: +48, with backslash remapped to '~'.
        temp_bytes[0] += 48
        if temp_bytes[0] == BACKSLASH_CODE:
            temp_bytes[0] = TILDE_CODE
        temp_bytes[1] += 48
        if temp_bytes[1] == BACKSLASH_CODE:
            temp_bytes[1] = TILDE_CODE
        temp_bytes[2] += 48
        if temp_bytes[2] == BACKSLASH_CODE:
            temp_bytes[2] = TILDE_CODE
        temp_bytes[3] += 48
        if temp_bytes[3] == BACKSLASH_CODE:
            temp_bytes[3] = TILDE_CODE

        outputdata[strindex] = temp_bytes[0]
        outputdata[strindex + 1] = temp_bytes[1]
        outputdata[strindex + 2] = temp_bytes[2]
        outputdata[strindex + 3] = temp_bytes[3]

    outputdata[encoded_len] = 0
    return encoded_len


def ColPicEncode(fromcolor16, picw, pich, outputdata: bytearray, outputmaxtsize, colorsmax):
    """Build palette + RLE-ish payload into outputdata buffer."""
    if picw <= 0 or pich <= 0 or outputmaxtsize <= 0:
        return 0
    outputmaxtsize = min(outputmaxtsize, len(outputdata))
    if outputmaxtsize <= COLPIC_HEADER_SIZE:
        return 0

    head0 = ColPicHead3()
    listu16 = [U16HEAD() for _ in range(MAX_COLORS)]
    color_index = {}

    listqty = 0
    dotsqty = picw * pich
    if dotsqty > len(fromcolor16):
        dotsqty = len(fromcolor16)
    if dotsqty <= 0:
        return 0

    if colorsmax > MAX_COLORS:
        colorsmax = MAX_COLORS
    if colorsmax <= 0:
        colorsmax = 1

    for i in range(dotsqty):
        listqty = ADList0(fromcolor16[i], listu16, listqty, MAX_COLORS, color_index)

    # Sort by occurrence count descending.
    sorted_palette = sorted(listu16[:listqty], key=lambda item: item.qty, reverse=True)
    listu16[:listqty] = sorted_palette

    # Merge least-frequent colors into nearest bucket until colorsmax.
    while listqty > colorsmax:
        l0 = listu16[listqty - 1]
        minval = 255
        fid = -1
        for i in range(colorsmax):
            cha0 = abs(listu16[i].A0 - l0.A0)
            cha1 = abs(listu16[i].A1 - l0.A1)
            cha2 = abs(listu16[i].A2 - l0.A2)
            chall = cha0 + cha1 + cha2
            if chall < minval:
                minval = chall
                fid = i

        if fid < 0:
            return 0

        for i in range(dotsqty):
            if fromcolor16[i] == l0.colo16:
                fromcolor16[i] = listu16[fid].colo16

        listqty -= 1

    for n in range(outputmaxtsize):
        outputdata[n] = 0

    head0.encodever = 3
    head0.oncelistqty = 0
    head0.mark = 98419516
    head0.ListDataSize = listqty * 2
    if COLPIC_HEADER_SIZE + head0.ListDataSize >= outputmaxtsize:
        return 0

    # Header bytes
    outputdata[0] = 3
    outputdata[12] = 60
    outputdata[13] = 195
    outputdata[14] = 221
    outputdata[15] = 5
    outputdata[16] = listqty * 2 & MASK_8
    outputdata[17] = (listqty * 2 & MASK_16) >> 8
    outputdata[18] = (listqty * 2 & MASK_24) >> 16
    outputdata[19] = (listqty * 2 & MASK_32) >> 24

    for i in range(listqty):
        outputdata[COLPIC_HEADER_SIZE + i * 2 + 1] = (listu16[i].colo16 & MASK_16) >> 8
        outputdata[COLPIC_HEADER_SIZE + i * 2 + 0] = listu16[i].colo16 & MASK_8

    enqty = Byte8bitEncode(
        fromcolor16,
        COLPIC_HEADER_SIZE,
        head0.ListDataSize >> 1,
        dotsqty,
        outputdata,
        COLPIC_HEADER_SIZE + head0.ListDataSize,
        outputmaxtsize - COLPIC_HEADER_SIZE - head0.ListDataSize,
    )
    head0.ColorDataSize = enqty
    head0.PicW = picw
    head0.PicH = pich

    outputdata[4] = picw & MASK_8
    outputdata[5] = (picw & MASK_16) >> 8
    outputdata[6] = (picw & MASK_24) >> 16
    outputdata[7] = (picw & MASK_32) >> 24
    outputdata[8] = pich & MASK_8
    outputdata[9] = (pich & MASK_16) >> 8
    outputdata[10] = (pich & MASK_24) >> 16
    outputdata[11] = (pich & MASK_32) >> 24
    outputdata[20] = enqty & MASK_8
    outputdata[21] = (enqty & MASK_16) >> 8
    outputdata[22] = (enqty & MASK_24) >> 16
    outputdata[23] = (enqty & MASK_32) >> 24

    return COLPIC_HEADER_SIZE + head0.ListDataSize + head0.ColorDataSize


def ADList0(val, listu16, listqty, maxqty, color_index=None):
    """Add/update color histogram entry in listu16."""
    qty = listqty
    if qty >= maxqty:
        return listqty

    if color_index is not None:
        existing = color_index.get(val)
        if existing is not None and existing < qty:
            listu16[existing].qty += 1
            return listqty
    else:
        for i in range(qty):
            if listu16[i].colo16 == val:
                listu16[i].qty += 1
                return listqty

    a0 = val >> 11 & MASK_5
    a1 = (val & 2016) >> 5
    a2 = val & MASK_5
    listu16[qty].colo16 = val
    listu16[qty].A0 = a0
    listu16[qty].A1 = a1
    listu16[qty].A2 = a2
    listu16[qty].qty = 1
    if color_index is not None:
        color_index[val] = qty
    listqty = qty + 1
    return listqty


def Byte8bitEncode(fromcolor16, listu16Index, listqty, dotsqty, outputdata: bytearray, outputdataIndex, decMaxBytesize):
    """Encode indexed color stream into packed control/data bytes."""
    if decMaxBytesize <= 0 or listqty <= 0 or dotsqty <= 0:
        return 0
    listu16 = outputdata
    dots = 0
    srcindex = 0
    decindex = 0
    lastid = 0
    temp = 0
    max_dotsqty = min(dotsqty, len(fromcolor16))
    if max_dotsqty <= 0:
        return 0

    # Build color->palette-index lookup once to avoid O(listqty) scan per run.
    palette_index = {}
    for i in range(listqty):
        aa = listu16[i * 2 + 1 + listu16Index] << 8
        aa |= listu16[i * 2 + 0 + listu16Index]
        palette_index[aa] = i

    dotsqty = max_dotsqty
    while dotsqty > 0:
        dots = 1
        for i in range(dotsqty - 1):
            if fromcolor16[srcindex + i] != fromcolor16[srcindex + i + 1]:
                break
            dots += 1
            if dots == 255:
                break

        temp = palette_index.get(fromcolor16[srcindex], 0)

        tid = int(temp % 32)
        if tid > 255:
            tid = 255
        sid = int(temp // 32)
        if sid > 255:
            sid = 255

        if lastid != sid:
            if decindex >= decMaxBytesize:
                dotsqty = 0
                break
            outputdata[decindex + outputdataIndex] = 7
            outputdata[decindex + outputdataIndex] <<= 5
            outputdata[decindex + outputdataIndex] += sid
            decindex += 1
            lastid = sid

        if dots <= 6:
            if decindex >= decMaxBytesize:
                dotsqty = 0
                break
            aa = dots
            if aa > 255:
                aa = 255
            outputdata[decindex + outputdataIndex] = aa
            outputdata[decindex + outputdataIndex] <<= 5
            outputdata[decindex + outputdataIndex] += tid
            decindex += 1
        else:
            if decindex >= decMaxBytesize:
                dotsqty = 0
                break
            outputdata[decindex + outputdataIndex] = 0
            outputdata[decindex + outputdataIndex] += tid
            decindex += 1
            if decindex >= decMaxBytesize:
                dotsqty = 0
                break
            aa = dots
            if aa > 255:
                aa = 255
            outputdata[decindex + outputdataIndex] = aa
            decindex += 1

        srcindex += dots
        dotsqty -= dots

    return decindex


class U16HEAD:
    """Palette entry helper used by ColPicEncode."""

    def __init__(self):
        self.colo16 = 0
        self.A0 = 0
        self.A1 = 0
        self.A2 = 0
        self.res0 = 0
        self.res1 = 0
        self.qty = 0


class ColPicHead3:
    """Header container matching expected encoded payload layout."""

    def __init__(self):
        self.encodever = 0
        self.res0 = 0
        self.oncelistqty = 0
        self.PicW = 0
        self.PicH = 0
        self.mark = 0
        self.ListDataSize = 0
        self.ColorDataSize = 0
        self.res1 = 0
        self.res2 = 0
