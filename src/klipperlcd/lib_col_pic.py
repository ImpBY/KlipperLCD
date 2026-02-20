# Copyright (c) 2023 Molodos
# The ElegooNeptuneThumbnails plugin is released under the terms of the AGPLv3 or higher.

"""Color-picture encoder used by the LCD thumbnail pipeline.

Important:
- This module is performance/compatibility sensitive.
- Formatting and comments were improved, but the algorithm and value flow
  are intentionally preserved.
"""


def ColPic_EncodeStr(fromcolor16, picw, pich, outputdata: bytearray, outputmaxtsize, colorsmax):
    """Encode color data, then convert packed bytes into LCD-specific ASCII-ish stream."""
    qty = 0
    temp = 0
    strindex = 0
    hexindex = 0
    temp_bytes = bytearray(4)

    qty = ColPicEncode(fromcolor16, picw, pich, outputdata, outputmaxtsize, colorsmax)
    if qty == 0:
        return 0

    # Pad to 3-byte groups for 4/3 expansion.
    temp = 3 - qty % 3
    while temp > 0 and qty < outputmaxtsize:
        outputdata[qty] = 0
        qty += 1
        temp -= 1

    if qty * 4 / 3 >= outputmaxtsize:
        return 0

    hexindex = qty
    strindex = qty * 4 / 3
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
        if chr(temp_bytes[0]) == '\\':
            temp_bytes[0] = 126
        temp_bytes[1] += 48
        if chr(temp_bytes[1]) == '\\':
            temp_bytes[1] = 126
        temp_bytes[2] += 48
        if chr(temp_bytes[2]) == '\\':
            temp_bytes[2] = 126
        temp_bytes[3] += 48
        if chr(temp_bytes[3]) == '\\':
            temp_bytes[3] = 126

        outputdata[int(strindex)] = temp_bytes[0]
        outputdata[int(strindex) + 1] = temp_bytes[1]
        outputdata[int(strindex) + 2] = temp_bytes[2]
        outputdata[int(strindex) + 3] = temp_bytes[3]

    qty = qty * 4 / 3
    outputdata[int(qty)] = 0
    return qty


def ColPicEncode(fromcolor16, picw, pich, outputdata: bytearray, outputmaxtsize, colorsmax):
    """Build palette + RLE-ish payload into outputdata buffer."""
    l0 = U16HEAD()
    head0 = ColPicHead3()
    listu16 = []
    for _ in range(1024):
        listu16.append(U16HEAD())

    listqty = 0
    enqty = 0
    dotsqty = picw * pich

    if colorsmax > 1024:
        colorsmax = 1024

    for i in range(dotsqty):
        listqty = ADList0(fromcolor16[i], listu16, listqty, 1024)

    # Sort by occurrence count (descending-ish via insertion moves).
    for index in range(1, listqty):
        l0 = listu16[index]
        for i in range(index):
            if l0.qty >= listu16[i].qty:
                alistu16 = blistu16 = listu16.copy()
                for j in range(index - i):
                    listu16[i + j + 1] = alistu16[i + j]
                listu16[i] = l0
                break

    # Merge least-frequent colors into nearest bucket until colorsmax.
    while listqty > colorsmax:
        l0 = listu16[listqty - 1]
        minval = 255
        fid = -1
        for i in range(colorsmax):
            cha0 = listu16[i].A0 - l0.A0
            if cha0 < 0:
                cha0 = 0 - cha0
            cha1 = listu16[i].A1 - l0.A1
            if cha1 < 0:
                cha1 = 0 - cha1
            cha2 = listu16[i].A2 - l0.A2
            if cha2 < 0:
                cha2 = 0 - cha2
            chall = cha0 + cha1 + cha2
            if chall < minval:
                minval = chall
                fid = i

        for i in range(dotsqty):
            if fromcolor16[i] == l0.colo16:
                fromcolor16[i] = listu16[fid].colo16

        listqty = listqty - 1

    for n in range(len(outputdata)):
        outputdata[n] = 0

    head0.encodever = 3
    head0.oncelistqty = 0
    head0.mark = 98419516
    head0.ListDataSize = listqty * 2

    # Header bytes
    outputdata[0] = 3
    outputdata[12] = 60
    outputdata[13] = 195
    outputdata[14] = 221
    outputdata[15] = 5
    outputdata[16] = listqty * 2 & 255
    outputdata[17] = (listqty * 2 & 65280) >> 8
    outputdata[18] = (listqty * 2 & 16711680) >> 16
    outputdata[19] = (listqty * 2 & 4278190080) >> 24

    sizeof_col_pic_head3 = 32
    for i in range(listqty):
        outputdata[sizeof_col_pic_head3 + i * 2 + 1] = (listu16[i].colo16 & 65280) >> 8
        outputdata[sizeof_col_pic_head3 + i * 2 + 0] = listu16[i].colo16 & 255

    enqty = Byte8bitEncode(
        fromcolor16,
        sizeof_col_pic_head3,
        head0.ListDataSize >> 1,
        dotsqty,
        outputdata,
        sizeof_col_pic_head3 + head0.ListDataSize,
        outputmaxtsize - sizeof_col_pic_head3 - head0.ListDataSize,
    )
    head0.ColorDataSize = enqty
    head0.PicW = picw
    head0.PicH = pich

    outputdata[4] = picw & 255
    outputdata[5] = (picw & 65280) >> 8
    outputdata[6] = (picw & 16711680) >> 16
    outputdata[7] = (picw & 4278190080) >> 24
    outputdata[8] = pich & 255
    outputdata[9] = (pich & 65280) >> 8
    outputdata[10] = (pich & 16711680) >> 16
    outputdata[11] = (pich & 4278190080) >> 24
    outputdata[20] = enqty & 255
    outputdata[21] = (enqty & 65280) >> 8
    outputdata[22] = (enqty & 16711680) >> 16
    outputdata[23] = (enqty & 4278190080) >> 24

    return sizeof_col_pic_head3 + head0.ListDataSize + head0.ColorDataSize


def ADList0(val, listu16, listqty, maxqty):
    """Add/update color histogram entry in listu16."""
    qty = listqty
    if qty >= maxqty:
        return listqty

    for i in range(qty):
        if listu16[i].colo16 == val:
            listu16[i].qty += 1
            return listqty

    a0 = val >> 11 & 31
    a1 = (val & 2016) >> 5
    a2 = val & 31
    listu16[qty].colo16 = val
    listu16[qty].A0 = a0
    listu16[qty].A1 = a1
    listu16[qty].A2 = a2
    listu16[qty].qty = 1
    listqty = qty + 1
    return listqty


def Byte8bitEncode(fromcolor16, listu16Index, listqty, dotsqty, outputdata: bytearray, outputdataIndex, decMaxBytesize):
    """Encode indexed color stream into packed control/data bytes."""
    listu16 = outputdata
    dots = 0
    srcindex = 0
    decindex = 0
    lastid = 0
    temp = 0

    while dotsqty > 0:
        dots = 1
        for i in range(dotsqty - 1):
            if fromcolor16[srcindex + i] != fromcolor16[srcindex + i + 1]:
                break
            dots += 1
            if dots == 255:
                break

        temp = 0
        for i in range(listqty):
            aa = listu16[i * 2 + 1 + listu16Index] << 8
            aa |= listu16[i * 2 + 0 + listu16Index]
            if aa == fromcolor16[srcindex]:
                temp = i
                break

        tid = int(temp % 32)
        if tid > 255:
            tid = 255
        sid = int(temp / 32)
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
