#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# WarmPlayer · 黑胶唱片播放器 + 磁带卡座
# Copyright (c) 2026 田瑞航 (Ryan) · github.com/aiventian-dev
# Licensed under the MIT License (see LICENSE)
"""
黑胶唱片机 · 本地服务
--------------------------------------------------
在浏览器里读取本机音乐文件（mp3 / m4a / flac / wav / aac / ogg …），
自己解析 ID3 / Vorbis / iTunes 标签与内嵌封面，把结果做成唱片架 + 唱盘机。

启动：双击「启动唱片机.command」，或用
      python3 vinyl_server.py [--port 8900] [--no-open]
"""

import argparse
import base64
import hashlib
import json
import mimetypes
import os
import re
import shutil
import signal
import struct
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
import socket
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def lan_ips():
    """本机的局域网 IPv4，用于打印访问地址"""
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.6)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip and ip not in ips:
            ips.append(ip)
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    return ips


def log(msg):
    """实时日志：立刻刷到终端，方便盯着扫描 / 添加过程。"""
    global _T0
    try:
        line = "[%s +%5.1fs] %s" % (time.strftime("%H:%M:%S"), time.time() - _T0, msg)
        print(line, flush=True)
        try:
            with open(os.path.join(CACHE_DIR, "run.log"), "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:
            pass
    except Exception:
        pass


_T0 = time.time()


class QuietServer(ThreadingHTTPServer):
    daemon_threads = True
    # 默认 backlog 只有 5，页面一次拉几十张封面 + 长连接会把 accept 队列塞满，
    # 表现就是"端口在监听但新连接直接超时"。抬到 128。
    request_queue_size = 128

    def handle_error(self, request, client_address):
        import sys
        et = sys.exc_info()[0]
        if et in (BrokenPipeError, ConnectionResetError):
            return
        super().handle_error(request, client_address)

APP_DIR = os.path.dirname(os.path.abspath(__file__))
# 数据根 override（v0.81 套壳预备）：缓存/设置/目录清单可脱离脚本目录，
# 供 mac app 把数据放进 ~/Library/Application Support；不传则完全保持旧行为。
_DATA_OVERRIDE = os.environ.get("WARMPLAYER_DATA") or next(
    (sys.argv[i + 1] for i, a in enumerate(sys.argv)
     if a == "--data-dir" and i + 1 < len(sys.argv)), "")
DATA_ROOT = os.path.abspath(os.path.expanduser(_DATA_OVERRIDE)) if _DATA_OVERRIDE else APP_DIR
CACHE_DIR = os.path.join(DATA_ROOT, "cache")
COVER_DIR = os.path.join(CACHE_DIR, "covers")
THUMB_DIR = os.path.join(CACHE_DIR, "covers_s")
CONF_FILE = os.path.join(DATA_ROOT, "folders.json")
TAGS_FILE = os.path.join(CACHE_DIR, "tags.json")
TINT_FILE = os.path.join(CACHE_DIR, "tints.json")
SEEN_FILE = os.path.join(CACHE_DIR, "seen.json")
PATHS_FILE = os.path.join(CACHE_DIR, "paths.json")
TRANS_DIR = os.path.join(CACHE_DIR, "trans")
SETTINGS_FILE = os.path.join(DATA_ROOT, "settings.json")
SETTINGS = {"trans": False}   # 默认不转码，交给浏览器原生解码
DEFAULT_FOLDER_PWD = "1987"   # 远程访问时进入「音乐目录」的密码，可在 settings.json 的 folder_pwd 改
TAG_VER = "v4"   # 缓存版本：加入音频文件内的版权标签
PAGE_FILE = os.path.join(os.environ.get("WARMPLAYER_PAGES") or APP_DIR, "唱片机.html")
TAPE_FILE = os.path.join(os.environ.get("WARMPLAYER_PAGES") or APP_DIR, "磁带机.html")

AUDIO_EXT = {".mp3", ".m4a", ".m4b", ".aac", ".flac", ".wav", ".aif", ".aiff", ".ogg", ".oga", ".opus"}
MIME = {
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".m4b": "audio/mp4", ".aac": "audio/aac",
    ".flac": "audio/flac", ".wav": "audio/wav", ".aif": "audio/aiff", ".aiff": "audio/aiff",
    ".ogg": "audio/ogg", ".oga": "audio/ogg", ".opus": "audio/ogg",
}
COVER_MIME = {b"\xff\xd8": "image/jpeg", b"\x89PNG": "image/png", b"GIF8": "image/gif", b"BM": "image/bmp"}

SIPS = "/usr/bin/sips" if os.path.exists("/usr/bin/sips") else (shutil.which("sips") or "")
# launchd 等守护进程环境的 PATH 只有系统四目录，which 会漏掉 homebrew 安装位；
# 启动期解析一次后全程复用，故这里必须自带常见安装位回退（v0.75）
FFMPEG = os.environ.get("WARMPLAYER_FFMPEG") or shutil.which("ffmpeg") or next(
    (c for c in ("/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg", "/usr/bin/ffmpeg")
     if os.path.isfile(c) and os.access(c, os.X_OK)), None)
if FFMPEG and not (os.path.isfile(FFMPEG) and os.access(FFMPEG, os.X_OK)):
    FFMPEG = shutil.which("ffmpeg") or None
METER_DEBUG = bool(os.environ.get("METER_DEBUG"))
START_TS = time.time()

LOCK = threading.RLock()
SCAN = {"active": False, "folderName": "", "done": 0, "total": 0}
FOLDERS = []          # [{id,name,path}]
ALBUMS = []           # 组装好的唱片
TRACKS = {}           # tid -> 绝对路径
TAGS = {}             # path -> {"m":mtime,"s":size,"tags":{...},"hasCover":bool}
PATHS = {}            # 目录 id -> [该目录下的音频文件路径]（和 enabled 无关，永远保留）
TINTS = {}            # album_id -> "#rrggbb"
SEEN = {}             # album_id -> 首次收录时间（秒），用于「添加时间」排序
HOLDERS = {}          # album_id -> 含封面的代表文件路径（按需抽封面用）
ANYCOVER = {}         # album_id -> 任意一首的路径（扫描没到时按需抽封面的兜底）
SCAN_GUARD = threading.Lock()   # 扫描单飞：同一时刻只允许一个 do_scan
COVER_LOCK = threading.Lock()   # 按需抽封面串行，避免同张专辑重复抽
METER_SLOTS = threading.BoundedSemaphore(2)  # 限制频谱解码并发


# ---------------------------------------------------------------- 基础工具
def short_hash(s, n=10):
    return hashlib.sha1(s.encode("utf-8", "ignore")).hexdigest()[:n]


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    # 串行 + 每次调用独立 tmp：并发调用共用一个 .tmp 会互相 replace 掉对方的临时文件
    # （FileNotFoundError），且 dump 期间别的线程改 dict 会炸迭代器。LOCK 是 RLock，可嵌套。
    with LOCK:
        tmp = "%s.tmp.%d.%d" % (path, os.getpid(), threading.get_ident())
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)


def sniff_image(data):
    for magic, mime in COVER_MIME.items():
        if data[:len(magic)] == magic:
            return mime
    return "image/jpeg"


# ---------------------------------------------------------------- 标签解析
def synchsafe(b):
    return (b[0] << 21) | (b[1] << 14) | (b[2] << 7) | b[3]


def de_unsync(b):
    if b"\xff\x00" not in b:
        return b
    return b.replace(b"\xff\x00", b"\xff")


def decode_text(raw, enc):
    try:
        if enc == 1:
            return raw.decode("utf-16", "ignore")
        if enc == 2:
            return raw.decode("utf-16-be", "ignore")
        if enc == 3:
            return raw.decode("utf-8", "ignore")
        return raw.decode("latin-1", "ignore")
    except Exception:
        return ""


def first_line(s):
    return (s or "").replace("\x00", " ").strip().split("\x00")[0].strip()


def read_id3v2(f, size):
    start = f.tell()
    head = f.read(10)
    if len(head) < 10 or head[:3] != b"ID3":
        f.seek(start)
        return {}, None
    ver = head[3]
    flags = head[5]
    body = synchsafe(head[6:10])
    raw = f.read(min(body, size - 10))
    if flags & 0x80:
        raw = de_unsync(raw)
    out, cover = {}, None
    i = 0
    idlen = 3 if ver == 2 else 4
    while i + idlen + (3 if ver == 2 else 4) <= len(raw):
        fid = raw[i:i + idlen]
        if not fid.strip(b"\x00"):
            break
        if ver == 2:
            fsize = (raw[i + 3] << 16) | (raw[i + 4] << 8) | raw[i + 5]
            hlen = 6
        else:
            fsize = synchsafe(raw[i + 4:i + 8]) if ver == 4 else int.from_bytes(raw[i + 4:i + 8], "big")
            hlen = 10
        body_start = i + hlen
        if fsize <= 0 or body_start + fsize > len(raw):
            break
        data = raw[body_start:body_start + fsize]
        key = fid.decode("latin-1")
        if key in ("TIT2", "TT2"):
            out["title"] = first_line(decode_text(data[1:], data[0]))
        elif key in ("TPE1", "TP1"):
            out["artist"] = first_line(decode_text(data[1:], data[0]))
        elif key in ("TPE2", "TP2"):
            out["albumartist"] = first_line(decode_text(data[1:], data[0]))
        elif key in ("TALB", "TAL"):
            out["album"] = first_line(decode_text(data[1:], data[0]))
        elif key in ("TDRC", "TYER", "TYE", "TDRL"):
            out["year"] = first_line(decode_text(data[1:], data[0]))[:4]
        elif key in ("TRCK", "TRK"):
            num = first_line(decode_text(data[1:], data[0]))
            out["track"] = int(re.split(r"[/\s]", num)[0]) if re.split(r"[/\s]", num)[0].isdigit() else 0
        elif key in ("TCON", "TCO"):
            out["genre"] = first_line(decode_text(data[1:], data[0])).strip("()")
        elif key in ("TCOP", "TCR"):
            out["copyright"] = first_line(decode_text(data[1:], data[0]))
        elif key == "APIC":
            enc = data[0]
            if enc in (1, 2):
                # mime 以 \x00 结尾，描述以 \x00\x00 结尾
                j = data.find(b"\x00\x00", 1)
                while j > 0 and data[j + 2:j + 3] != b"\x00" and j < len(data) - 4:
                    break
                mime_end = data.find(b"\x00", 1)
                mime = data[1:mime_end].decode("latin-1", "ignore")
                k = mime_end + 1 + 1  # 跳过图片类型
                rest = data[k:]
                desc_end = rest.find(b"\x00\x00")
                cover = rest[desc_end + 2:] if desc_end > 0 else rest
                cover = cover.lstrip(b"\x00")
                out["coverMime"] = mime
            else:
                mime_end = data.find(b"\x00", 1)
                mime = data[1:mime_end].decode("latin-1", "ignore")
                k = mime_end + 2
                desc_end = data.find(b"\x00", k)
                cover = data[desc_end + 1:] if desc_end > 0 else data[k:]
                out["coverMime"] = mime
        elif key == "PIC":
            mime = data[1:4].decode("latin-1", "ignore")
            k = 5
            desc_end = data.find(b"\x00", k)
            cover = data[desc_end + 1:] if desc_end > 0 else data[k:]
            out["coverMime"] = "image/" + mime.lower()
        i = body_start + fsize
    return out, cover


def read_id3v1(f, size):
    if size < 128:
        return {}
    f.seek(size - 128)
    tag = f.read(128)
    if tag[:3] != b"TAG":
        return {}
    g = lambda a, b: tag[a:b].decode("latin-1", "ignore").strip("\x00 ").strip()
    return {
        "title": g(3, 33), "artist": g(33, 63), "album": g(63, 93),
        "year": g(93, 97), "track": int(tag[125]) if tag[125] else 0,
    }


MP3_BR = {
    (3, 1): [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320],
    (3, 2): [0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384],
    (3, 3): [0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448],
    (2, 1): [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
    (2, 2): [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
}
MP3_SR = {3: [44100, 48000, 32000], 2: [22050, 24000, 16000], 0: [11025, 12000, 8000]}
MP3_SPF = {(1, 3): 1152, (2, 3): 576, (1, 1): 384, (1, 2): 1152, (2, 1): 384, (2, 2): 576}


def mp3_duration(f, size, tag_end):
    f.seek(tag_end)
    buf = f.read(4096)
    idx = -1
    for i in range(len(buf) - 4):
        if buf[i] == 0xFF and (buf[i + 1] & 0xE0) == 0xE0 and (buf[i + 1] & 0x06):
            idx = i
            break
    if idx < 0:
        return 0.0
    h = buf[idx:idx + 4]
    ver = (h[1] >> 3) & 3          # 3=MPEG1 2=MPEG2 0=MPEG2.5
    layer = (h[1] >> 1) & 3        # 1=L3 2=L2 3=L1
    brx = (h[2] >> 4) & 0x0F
    srx = (h[2] >> 2) & 0x03
    if brx in (0, 15) or srx == 3 or layer == 0:
        return 0.0
    table = MP3_BR.get((3 if ver == 3 else 2, layer))
    if not table:
        return 0.0
    bitrate = table[brx] * 1000
    sr = MP3_SR.get(ver, [44100, 48000, 32000])[srx]
    spf = MP3_SPF.get((3 if ver == 3 else 2, layer), 1152)
    xing = buf[idx:idx + 200].find(b"Xing")
    if xing < 0:
        xing = buf[idx:idx + 200].find(b"Info")
    if xing >= 0:
        flags = int.from_bytes(buf[idx + xing + 4: idx + xing + 8], "big")
        pos = idx + xing + 8
        if flags & 1:
            frames = int.from_bytes(buf[pos:pos + 4], "big")
            if frames > 0:
                return frames * spf / float(sr)
    if bitrate <= 0:
        return 0.0
    return max(0.0, (size - tag_end) * 8.0 / bitrate)


def read_flac(f, size):
    pos = 0
    head = f.read(4)
    if head == b"ID3":
        f.seek(0)
        h = f.read(10)
        body = synchsafe(h[6:10])
        pos = 10 + body
        f.seek(pos)
        head = f.read(4)
    if head != b"fLaC":
        return {}, None, 0.0
    tags, cover, dur = {}, None, 0.0
    while True:
        hdr = f.read(4)
        if len(hdr) < 4:
            break
        btype = hdr[0] & 0x7F
        last = hdr[0] & 0x80
        blen = int.from_bytes(hdr[1:4], "big")
        data = f.read(blen)
        if btype == 0 and len(data) >= 18:
            p = data[10:18]
            sr = (p[0] << 12) | (p[1] << 4) | (p[2] >> 4)
            total = ((p[3] & 0x0F) << 32) | (p[4] << 24) | (p[5] << 16) | (p[6] << 8) | p[7]
            if sr:
                dur = total / float(sr)
        elif btype == 4:
            tags.update(vorbis_to_tags(parse_vorbis_comment(data)))
        elif btype == 6 and cover is None:
            try:
                cover, mime = parse_flac_picture(data)
                tags["coverMime"] = mime
            except Exception:
                pass
        if last:
            break
    return tags, cover, dur


def parse_vorbis_comment(data):
    out = {}
    try:
        vlen = struct.unpack("<I", data[0:4])[0]
        p = 4 + vlen
        n = struct.unpack("<I", data[p:p + 4])[0]
        p += 4
        for _ in range(min(n, 200)):
            ln = struct.unpack("<I", data[p:p + 4])[0]
            p += 4
            kv = data[p:p + ln].decode("utf-8", "ignore")
            p += ln
            if "=" in kv:
                k, v = kv.split("=", 1)
                out[k.upper()] = v
    except Exception:
        pass
    return out


def parse_flac_picture(data):
    p = 0
    ptype = struct.unpack(">I", data[p:p + 4])[0]; p += 4
    mlen = struct.unpack(">I", data[p:p + 4])[0]; p += 4
    mime = data[p:p + mlen].decode("latin-1", "ignore"); p += mlen
    dlen = struct.unpack(">I", data[p:p + 4])[0]; p += 4 + dlen
    p += 16
    ilen = struct.unpack(">I", data[p:p + 4])[0]; p += 4
    return data[p:p + ilen], (mime or "image/jpeg")


def vorbis_to_tags(vc):
    t = {}
    if vc.get("TITLE"):
        t["title"] = vc["TITLE"]
    if vc.get("ARTIST"):
        t["artist"] = vc["ARTIST"]
    if vc.get("ALBUMARTIST"):
        t["albumartist"] = vc["ALBUMARTIST"]
    if vc.get("ALBUM"):
        t["album"] = vc["ALBUM"]
    if vc.get("DATE") or vc.get("YEAR"):
        t["year"] = (vc.get("DATE") or vc.get("YEAR"))[:4]
    if vc.get("TRACKNUMBER"):
        num = re.split(r"[/\s]", vc["TRACKNUMBER"])[0]
        t["track"] = int(num) if num.isdigit() else 0
    if vc.get("GENRE"):
        t["genre"] = vc["GENRE"]
    if vc.get("COPYRIGHT") or vc.get("COPYRIGHTS"):
        t["copyright"] = vc.get("COPYRIGHT") or vc.get("COPYRIGHTS")
    return t


CODEC_ALIAS = {"ec-3": "eac3", "ac-4": "ac4", "ac-3": "ac3", "ec+3": "eac3", "dtsc": "dts"}


def stsd_codec(f, size):
    """读 moov/trak/mdia/minf/stbl/stsd 第一个条目的格式四字码（只读几十字节）。"""
    def atoms(start, end):
        p = start
        while p < end:
            f.seek(p)
            head = f.read(8)
            if len(head) < 8:
                return
            alen = int.from_bytes(head[0:4], "big")
            atype = head[4:8]
            hlen = 8
            if alen == 1:
                f.seek(p + 8)
                alen = int.from_bytes(f.read(8), "big")
                hlen = 16
            if alen <= 0 or p + alen > size:
                return
            yield atype, p + hlen, p + alen
            p += alen

    def find(start, end, want, depth=0):
        if depth > 6:
            return None
        for atype, ds, de in atoms(start, end):
            if atype == want:
                return (ds, de)
            if atype in (b"moov", b"trak", b"mdia", b"minf", b"stbl"):
                r = find(ds, de, want, depth + 1)
                if r:
                    return r
        return None

    stsd = find(0, size, b"stsd")
    if not stsd:
        return ""
    f.seek(stsd[0] + 8)
    f.read(4)
    code = f.read(4).decode("ascii", "ignore").strip()
    return CODEC_ALIAS.get(code, code)


_CODEC_MEM = {}


def mp4_codec(path):
    if path in _CODEC_MEM:
        return _CODEC_MEM[path]
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            code = stsd_codec(f, size)
        _CODEC_MEM[path] = code
        return code
    except Exception:
        return ""


def read_mp4(f, size):
    tags, cover, dur = {}, None, 0.0

    def atoms(start, end):
        # 自己维护游标，避免循环体里 seek 打断迭代
        p = start
        while p < end:
            f.seek(p)
            head = f.read(8)
            if len(head) < 8:
                return
            alen = int.from_bytes(head[0:4], "big")
            atype = head[4:8]
            if alen == 1:
                f.seek(p + 8)
                alen = int.from_bytes(f.read(8), "big")
                hlen = 16
            else:
                hlen = 8
            if alen <= 0 or p + alen > size:
                return
            yield atype, p + hlen, p + alen
            p = p + alen

    mov_off = None
    for atype, ds, de in atoms(0, size):
        if atype == b"moov":
            mov_off = (ds, de)
            break
    if not mov_off:
        return tags, cover, dur
    moov_start, moov_end = mov_off
    try:
        code = stsd_codec(f, size)
        if code:
            tags["codec"] = code
    except Exception:
        pass

    # mvhd
    for atype, ds, de in atoms(moov_start, moov_end):
        if atype == b"mvhd":
            f.seek(ds)
            ver = f.read(1)[0]
            f.seek(ds + (20 if ver == 1 else 12))
            ts = int.from_bytes(f.read(4), "big")
            du = int.from_bytes(f.read(8 if ver == 1 else 4), "big")
            if ts:
                dur = du / float(ts)

    def walk_meta(start, end, depth=0):
        nonlocal cover
        for atype, ds, de in atoms(start, end):
            if atype == b"meta":
                walk_meta(ds + 4, de, depth + 1)
            elif atype == b"udta":
                walk_meta(ds, de, depth + 1)
            elif atype == b"ilst":
                walk_ilst(ds, de)

    def walk_ilst(start, end):
        nonlocal cover
        for atype, ds, de in atoms(start, end):
            # 只读 data 头，不整块读（covr 里有几百 KB~几 MB 的图，网络卷上极慢）
            f.seek(ds)
            head = f.read(min(32, max(0, de - ds)))
            dpos = head.find(b"data")
            if dpos < 4:
                continue
            atom_len = int.from_bytes(head[dpos - 4:dpos], "big")
            plen = atom_len - 16
            if plen <= 0:
                continue
            poff = ds + dpos + 12
            key = atype
            if key == b"covr":
                tags["hasCovr"] = True       # 只标记有封面，等真要显示时再读
                continue
            f.seek(poff)
            payload = f.read(min(plen, 512))
            if key == b"\xa9nam":
                tags["title"] = payload.decode("utf-8", "ignore").strip()
            elif key == b"\xa9ART":
                tags["artist"] = payload.decode("utf-8", "ignore").strip()
            elif key == b"aART":
                tags["albumartist"] = payload.decode("utf-8", "ignore").strip()
            elif key == b"\xa9alb":
                tags["album"] = payload.decode("utf-8", "ignore").strip()
            elif key == b"\xa9day":
                tags["year"] = payload.decode("utf-8", "ignore").strip()[:4]
            elif key == b"\xa9gen":
                tags["genre"] = payload.decode("utf-8", "ignore").strip()
            elif key in (b"\xa9cpy", b"cprt"):
                tags["copyright"] = payload.decode("utf-8", "ignore").strip("\x00 \t\r\n")
            elif key == b"trkn" and len(payload) >= 6:
                tags["track"] = int.from_bytes(payload[2:4], "big")
            elif key == b"covr" and cover is None:
                cover = payload
                tags["coverMime"] = sniff_image(payload)

    # 读声道编码（判断 ALAC 这类浏览器不支持的格式）
    try:
        found = []

        def walk_codec(st, en, path):
            for atype, ds, de in atoms(st, en):
                if atype == path[0]:
                    if len(path) == 1:
                        f.seek(ds + 12)
                        found.append(f.read(4).decode("latin-1").strip())
                        return True
                    if walk_codec(ds, de, path[1:]):
                        return True
            return False

        walk_codec(moov_start, moov_end, [b"trak", b"mdia", b"minf", b"stbl", b"stsd"])
        if found:
            tags["codec"] = found[0]
    except Exception:
        pass

    walk_meta(moov_start, moov_end)
    return tags, cover, dur


def read_wav(f, size):
    tags, dur = {}, 0.0
    f.seek(0)
    head = f.read(12)
    if len(head) < 12 or head[:4] != b"RIFF":
        return tags, dur
    f.seek(12)
    byte_rate = 0
    while True:
        h = f.read(8)
        if len(h) < 8:
            break
        cid = h[:4]
        clen = int.from_bytes(h[4:8], "little")
        if cid == b"fmt ":
            data = f.read(clen)
            if len(data) >= 16:
                byte_rate = int.from_bytes(data[8:12], "little")
        elif cid == b"data":
            if byte_rate:
                dur = clen / float(byte_rate)
            break
        elif cid == b"LIST":
            start = f.tell()
            data = f.read(min(clen, 4096))
            for k, field in ((b"INAM", "title"), (b"IART", "artist"), (b"IPRD", "album"), (b"ICRD", "year"), (b"ICOP", "copyright")):
                i = data.find(k)
                if i >= 0:
                    ln = int.from_bytes(data[i + 4:i + 8], "little")
                    tags[field] = data[i + 8:i + 8 + ln].decode("utf-8", "ignore").strip("\x00").strip()[: (4 if field == "year" else 200)]
            f.seek(start + clen + (clen % 2))
        else:
            f.seek(clen + (clen % 2), 1)
    return tags, dur


def read_oonn(f, size):
    """ogg / opus：只取注释，时长交给浏览器"""
    tags, cover = {}, None
    f.seek(0)
    buf = f.read(min(size, 192 * 1024))
    vc = None
    i = buf.find(b"OpusTags")
    if i >= 0:
        body = buf[i + 8:]
        vc = parse_vorbis_comment_from(body)
        tags.update(vorbis_to_tags(vc))
    else:
        i = buf.find(b"\x03vorbis")
        if i >= 0:
            vc = parse_vorbis_comment_from(buf[i + 7:])
            tags.update(vorbis_to_tags(vc))
        else:
            i = buf.find(b"fLaC")
    if vc:
        pic = vc.get("METADATA_BLOCK_PICTURE")
        if pic:
            try:
                cover, mime = parse_flac_picture(base64.b64decode(pic))
                tags["coverMime"] = mime
            except Exception:
                pass
    return tags, cover


def parse_vorbis_comment_from(data):
    out = {}
    try:
        vlen = struct.unpack("<I", data[0:4])[0]
        p = 4 + vlen
        n = struct.unpack("<I", data[p:p + 4])[0]
        p += 4
        for _ in range(min(n, 200)):
            ln = struct.unpack("<I", data[p:p + 4])[0]
            p += 4
            kv = data[p:p + ln].decode("utf-8", "ignore")
            p += ln
            if "=" in kv:
                k, v = kv.split("=", 1)
                out[k.upper()] = v
    except Exception:
        pass
    return out


def read_tags(path):
    """返回 {"tags":{...}, "hasCover":bool}，并单独取一次封面用 read_cover"""
    ext = os.path.splitext(path)[1].lower()
    out = {"title": "", "artist": "", "albumartist": "", "album": "", "year": "", "track": 0, "genre": "", "copyright": "", "dur": 0.0}
    cover = None
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if ext == ".mp3":
                tags, cover = read_id3v2(f, size)
                dur = mp3_duration(f, size, f.tell() if f.tell() else 0)
                if not tags.get("title"):
                    tags.update({k: v for k, v in read_id3v1(f, size).items() if v})
                out.update({k: v for k, v in tags.items() if v})
                out["dur"] = dur
            elif ext == ".flac":
                tags, cover, dur = read_flac(f, size)
                out.update({k: v for k, v in tags.items() if v})
                out["dur"] = dur
            elif ext in (".m4a", ".m4b", ".aac", ".mp4"):
                tags, cover, dur = read_mp4(f, size)
                out.update({k: v for k, v in tags.items() if v})
                out["dur"] = dur
                if tags.get("hasCovr"):
                    out["hasCovr"] = True
            elif ext in (".wav", ".aif", ".aiff"):
                tags, dur = read_wav(f, size)
                out.update({k: v for k, v in tags.items() if v})
                out["dur"] = dur
            elif ext in (".ogg", ".oga", ".opus"):
                tags, cover = read_oonn(f, size)
                out.update({k: v for k, v in tags.items() if v})
    except Exception:
        pass
    return out, (cover if cover else None)


def read_mp4_cover(f, size):
    """只把 covr 这块读出来（1~3MB），别把整个 moov/ilst 拉一遍。"""
    def atoms(start, end):
        p = start
        while p < end:
            f.seek(p)
            head = f.read(8)
            if len(head) < 8:
                return
            alen = int.from_bytes(head[0:4], "big")
            atype = head[4:8]
            hlen = 8
            if alen == 1:
                f.seek(p + 8)
                alen = int.from_bytes(f.read(8), "big")
                hlen = 16
            if alen <= 0 or p + alen > size:
                return
            yield atype, p + hlen, p + alen
            p += alen

    moov = None
    for atype, ds, de in atoms(0, size):
        if atype == b"moov":
            moov = (ds, de)
            break
    if not moov:
        return None

    def find_ilst(start, end, depth=0):
        if depth > 6:
            return None
        for atype, ds, de in atoms(start, end):
            if atype == b"ilst":
                return (ds, de)
            if atype in (b"meta", b"udta"):
                r = find_ilst(ds + (4 if atype == b"meta" else 0), de, depth + 1)
                if r:
                    return r
        return None

    ilst = find_ilst(moov[0], moov[1])
    if not ilst:
        return None
    for atype, ds, de in atoms(ilst[0], ilst[1]):
        if atype != b"covr":
            continue
        f.seek(ds)
        head = f.read(min(32, max(0, de - ds)))
        dpos = head.find(b"data")
        if dpos < 4:
            return None
        atom_len = int.from_bytes(head[dpos - 4:dpos], "big")
        plen = atom_len - 16
        if plen <= 0 or plen > 12 * 1024 * 1024:
            return None
        f.seek(ds + dpos + 12)
        return f.read(plen)
    return None


def read_cover(path):
    """只为了拿封面字节（扫描阶段不读，避免每组都读整块 APIC）"""
    ext = os.path.splitext(path)[1].lower()
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if ext == ".mp3":
                tags, cover = read_id3v2(f, size)
                return cover
            if ext == ".flac":
                tags, cover, dur = read_flac(f, size)
                return cover
            if ext in (".m4a", ".m4b", ".aac", ".mp4"):
                return read_mp4_cover(f, size)
            if ext in (".ogg", ".oga", ".opus"):
                tags, cover = read_oonn(f, size)
                return cover
    except Exception:
        return None
    return None



# ---------------------------------------------------------------- 内嵌歌词
LYRIC_CACHE = {}
LRC_RE = re.compile(r"\[(\d+):(\d+(?:[.:]\d+)?)\]")

def _atoms(f, start, end):
    p = start
    while p < end:
        f.seek(p)
        head = f.read(8)
        if len(head) < 8:
            return
        alen = int.from_bytes(head[0:4], "big")
        atype = head[4:8]
        hlen = 8
        if alen == 1:
            f.seek(p + 8)
            alen = int.from_bytes(f.read(8), "big")
            hlen = 16
        if alen <= 0 or p + alen > end:
            return
        yield atype, p + hlen, p + alen
        p += alen


def mp4_lyrics(f, size):
    moov = None
    for atype, ds, de in _atoms(f, 0, size):
        if atype == b"moov":
            moov = (ds, de)
            break
    if not moov:
        return ""
    found = []

    def walk(s, e, depth):
        if depth > 6 or found:
            return
        for atype, ds, de in _atoms(f, s, e):
            if atype == b"ilst":
                found.append((ds, de))
                return
            if atype in (b"udta", b"meta"):
                walk(ds + (4 if atype == b"meta" else 0), de, depth + 1)
                if found:
                    return

    walk(moov[0], moov[1], 0)
    if not found:
        return ""
    for atype, ds, de in _atoms(f, found[0][0], found[0][1]):
        if atype not in (b"\xa9lyr", b"lyr"):
            continue
        for aa, ads, ade in _atoms(f, ds, de):
            if aa != b"data":
                continue
            f.seek(ads + 8)
            return f.read(max(0, ade - ads - 8)).decode("utf-8", "replace").strip()
    return ""


def id3_lyrics(f, size):
    f.seek(0)
    hdr = f.read(10)
    if len(hdr) < 10 or hdr[0:3] != b"ID3":
        return ""
    ver = hdr[3]
    flags = hdr[5]
    sz = (hdr[6] << 21) | (hdr[7] << 14) | (hdr[8] << 7) | hdr[9]
    body_start = 10 + (10 if flags & 0x40 else 0)
    p = body_start
    end = min(size, body_start + sz)
    while p + 10 <= end:
        f.seek(p)
        fh = f.read(10)
        if len(fh) < 10:
            break
        fid = fh[0:4]
        if fid == b"\x00\x00\x00\x00":
            break
        if ver == 4:
            flen = (fh[4] << 21) | (fh[5] << 14) | (fh[6] << 7) | fh[7]
        else:
            flen = int.from_bytes(fh[4:8], "big")
        if flen <= 0 or p + 10 + flen > end:
            break
        if fid == b"USLT":
            f.seek(p + 10)
            raw = f.read(flen)
            if len(raw) < 4:
                p += 10 + flen
                continue
            enc = raw[0]
            rest = raw[4:]
            if enc in (1, 2):
                try:
                    sep = rest.find(b"\x00\x00") if enc == 1 else rest.find(b"\x00\x00")
                    if sep > 0:
                        rest = rest[sep + 2:]
                    txt = rest.decode("utf-16", "replace") if enc == 1 else rest.decode("utf-16-be", "replace")
                except Exception:
                    txt = rest.decode("utf-8", "replace")
            else:
                cut = rest.find(b"\x00")
                if cut > 0:
                    rest = rest[cut + 1:]
                txt = rest.decode("utf-8", "replace") if enc == 3 else rest.decode("latin-1", "replace")
            txt = txt.replace("\ufeff", "").strip("\x00").strip()
            if txt:
                return txt
        p += 10 + flen
    return ""


def vorbis_lyrics(f, size):
    f.seek(0)
    head = f.read(4)
    if head == b"fLaC":
        f.seek(4)
        for _ in range(64):
            bh = f.read(4)
            if len(bh) < 4:
                break
            btype = bh[0] & 0x7f
            blen = int.from_bytes(bh[1:4], "big")
            data = f.read(blen)
            if len(data) < blen:
                break
            if btype == 4:
                return _parse_vorbis(data)
            if btype == 127:
                break
        return ""
    # ogg：直接扫文件里的 LYRICS=
    f.seek(0)
    buf = b""
    while True:
        chunk = f.read(262144)
        if not chunk:
            break
        buf += chunk
        idx = buf.find(b"LYRICS=")
        if idx >= 0:
            seg = buf[idx + 7: idx + 7 + 8000]
            for stop in (b"\x01", b"\x03", b"LYRICS"):
                j = seg.find(stop)
                if j > 0:
                    seg = seg[:j]
            try:
                txt = seg.decode("utf-8", "replace")
            except Exception:
                txt = ""
            return txt.strip()
        buf = buf[-64:]
    return ""


def _parse_vorbis(data):
    try:
        n = int.from_bytes(data[0:4], "little")
        p = 4 + n
        cnt = int.from_bytes(data[p:p + 4], "little")
        p += 4
        for _ in range(cnt):
            ln = int.from_bytes(data[p:p + 4], "little")
            p += 4
            item = data[p:p + ln].decode("utf-8", "replace")
            p += ln
            k, _, v = item.partition("=")
            if k.upper() in ("LYRICS", "UNSYNCEDLYRICS", "LYRIC"):
                return v.strip()
    except Exception:
        pass
    return ""


def sidecar_lrc(path):
    """同名 .lrc 文件（很多人用这种方式放歌词）"""
    base = os.path.splitext(path)[0]
    for e in (".lrc", ".LRC", ".Lrc", ".lrc.txt"):
        cand = base + e
        if os.path.exists(cand):
            try:
                raw = open(cand, "rb").read()
                txt = raw.decode("utf-8-sig", "replace")
                if "\ufffd" in txt:
                    txt = raw.decode("gb18030", "replace")
                if txt.strip():
                    return txt.strip()
            except Exception:
                pass
    # 同目录兜底：忽略点号/空格差异，找名字最接近的 .lrc（"01. 说爱你" ↔ "01 说爱你"）
    try:
        d = os.path.dirname(path)
        stem = os.path.splitext(os.path.basename(path))[0]
        def norm(x):
            return "".join(ch for ch in x.lower() if ch.isalnum())
        want = norm(stem)
        names = [f for f in os.listdir(d) if f.lower().endswith(".lrc")]
        best = None
        for f in names:
            if norm(os.path.splitext(f)[0]) == want:
                best = f
                break
        if best:
            raw = open(os.path.join(d, best), "rb").read()
            txt = raw.decode("utf-8-sig", "replace")
            if "\ufffd" in txt:
                txt = raw.decode("gb18030", "replace")
            if txt.strip():
                return txt.strip()
    except Exception:
        pass
    return ""


def read_lyrics(path):
    txt = sidecar_lrc(path)
    if txt:
        return txt
    ext = os.path.splitext(path)[1].lower()
    try:
        size = os.path.getsize(path)
    except Exception:
        return ""
    try:
        with open(path, "rb") as f:
            if ext == ".mp3":
                return id3_lyrics(f, size)
            if ext in (".m4a", ".m4b", ".aac", ".mp4"):
                return mp4_lyrics(f, size)
            if ext in (".flac", ".ogg", ".oga", ".opus"):
                return vorbis_lyrics(f, size)
    except Exception:
        return ""
    return ""


FALLBACK_LRC = {}


def _normname(x):
    return "".join(ch for ch in x.lower() if ch.isalnum())


def find_other_copy_lyrics(path):
    """同一个曲目在别的目录里可能有 .lrc（合并专辑时常见），借用一下"""
    stem = _normname(os.path.splitext(os.path.basename(path))[0])
    if not stem:
        return ""
    if stem in FALLBACK_LRC:
        return FALLBACK_LRC[stem]
    txt = ""
    try:
        for other in list(TRACKS.values()):
            if other == path:
                continue
            if _normname(os.path.splitext(os.path.basename(other))[0]) != stem:
                continue
            got = sidecar_lrc(other)
            if not got:
                try:
                    with open(other, "rb") as f:
                        ext = os.path.splitext(other)[1].lower()
                        if ext == ".mp3":
                            got = id3_lyrics(f, os.path.getsize(other))
                        elif ext in (".m4a", ".m4b", ".aac", ".mp4"):
                            got = mp4_lyrics(f, os.path.getsize(other))
                        elif ext in (".flac", ".ogg", ".oga", ".opus"):
                            got = vorbis_lyrics(f, os.path.getsize(other))
                except Exception:
                    got = ""
            if got:
                txt = got
                break
    except Exception:
        txt = ""
    if len(FALLBACK_LRC) > 600:
        FALLBACK_LRC.clear()
    FALLBACK_LRC[stem] = txt
    return txt


def lyrics_for(tid):
    fp = TRACKS.get(tid)
    if not fp:
        return None
    try:
        st = os.stat(fp)
        lrc_mt = 0
        base = os.path.splitext(fp)[0]
        for e in (".lrc", ".LRC", ".Lrc", ".lrc.txt"):
            cand = base + e
            if os.path.exists(cand):
                lrc_mt = os.stat(cand).st_mtime
                break
        key = (fp, st.st_mtime, st.st_size, lrc_mt)
    except Exception:
        return None
    hit = LYRIC_CACHE.get(key)
    if hit is not None:
        return hit
    txt = read_lyrics(fp)
    if not txt:
        txt = find_other_copy_lyrics(fp)
    item = {"lyrics": txt, "lrc": bool(LRC_RE.search(txt or ""))}
    if len(LYRIC_CACHE) > 400:
        LYRIC_CACHE.clear()
    LYRIC_CACHE[key] = item
    return item


# ---------------------------------------------------------------- 转码兜底
TRANS_LOCK = threading.Lock()
TRANS_BUSY = {}


def track_codec(path):
    t = (TAGS.get(path) or {}).get("tags") or {}
    c = (t.get("codec") or "").strip()
    c = CODEC_ALIAS.get(c, c)
    if not c and os.path.splitext(path)[1].lower() in (".m4a", ".m4b", ".mp4", ".aac"):
        c = mp4_codec(path)
    return c


def would_transcode(path):
    """这个文件浏览器原生解不了（ALAC 等），需要时才转"""
    if not FFMPEG:
        return False
    ext = os.path.splitext(path)[1].lower()
    if ext in (".m4a", ".m4b", ".mp4"):
        return track_codec(path) not in ("mp4a", "mp4a ")
    return False


AWAKE = {}                 # sid -> 最后一次心跳时间
AWAKE_PROC = None          # 播放中才起：防息屏 / 防锁屏
BASE_PROC = None           # 常驻：只防休眠，允许息屏
AWAKE_LOCK = threading.Lock()
AWAKE_PIDFILE = "/tmp/vinyl_awake_guards.pids"


def awake_beat(sid, on):
    """客户端播放时每 30s 打一次心跳；暂停就撤掉。"""
    if on:
        AWAKE[sid] = time.time()
    else:
        AWAKE.pop(sid, None)


def awake_active(window=75):
    now = time.time()
    for k, v in list(AWAKE.items()):
        if now - v > window:
            AWAKE.pop(k, None)
    return bool(AWAKE)


def _spawn_guard(cmd):
    """起一个 caffeinate，并把 pid 记进 pidfile，供下次启动清账。"""
    p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        with open(AWAKE_PIDFILE, "a") as f:
            f.write("%d\n" % p.pid)
    except Exception:
        pass
    return p


def _drop_guard(p):
    if p is None:
        return None
    try:
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=3)
            except Exception:
                p.kill()
    except Exception:
        pass
    return None


def awake_reap():
    """上一个服务进程被杀时可能留下孤儿 caffeinate（pid 记在 pidfile），启动时清掉，
    避免越重启越多、也避免"明明没在放歌却一直不息屏"。"""
    try:
        with open(AWAKE_PIDFILE) as f:
            pids = [int(x) for x in f.read().split()]
    except Exception:
        pids = []
    killed = 0
    for pid in pids:
        try:
            out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                                 capture_output=True, text=True).stdout
            if "caffeinate" in out:
                os.kill(pid, signal.SIGTERM)
                killed += 1
        except Exception:
            pass
    try:
        with open(AWAKE_PIDFILE, "w") as f:
            f.write("")
    except Exception:
        pass
    if killed:
        log("清掉上一次遗留的 caffeinate %d 个" % killed)


def awake_loop():
    """两条互不相干的守卫：
    BASE_PROC  常驻 `-ims`：本机要当服务器给别的设备用，所以永远不许系统休眠；
               但"显示可以睡"（-d 不加），没人放歌时屏照常熄。
    AWAKE_PROC 只在有客户端播放心跳时起 `-du`：顶住熄屏（屏不熄，屏保/锁屏就不触发）。
    """
    global AWAKE_PROC, BASE_PROC
    while True:
        try:
            if sys.platform == "darwin":
                active = awake_active()
                with AWAKE_LOCK:
                    if BASE_PROC is None or BASE_PROC.poll() is not None:
                        BASE_PROC = _spawn_guard(["caffeinate", "-ims"])
                        log("休眠守卫：常驻（允许息屏，禁止休眠）")
                    if active and (AWAKE_PROC is None or AWAKE_PROC.poll() is not None):
                        AWAKE_PROC = _spawn_guard(["caffeinate", "-du"])
                        log("播放中：已阻止熄屏 / 锁屏")
                    elif not active and AWAKE_PROC is not None:
                        AWAKE_PROC = _drop_guard(AWAKE_PROC)
                        log("播放结束：恢复息屏（休眠守卫仍常驻）")
        except Exception as e:
            log("熄屏守卫异常：" + repr(e))
        time.sleep(5)


def health_loop(port):
    """自检：本机每 30s 打一次自己的最小请求。连续 3 次打不通（端口还在监听但
    没人应答，也就是上面那次那种"进程活着、服务死了"）就原地 execv 重启自己。
    socket 都是 CLOEXEC，execv 时会被关掉，新镜像能立刻重新 bind。"""
    fails = 0
    while True:
        time.sleep(30)
        ok = False
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=6)
            s.sendall(b"GET /api/awake?on=0&sid=__wdt HTTP/1.1\r\n"
                      b"Host: 127.0.0.1\r\nConnection: close\r\n\r\n")
            s.settimeout(6)
            head = s.recv(64)
            ok = head.startswith(b"HTTP/1.1 200") or head.startswith(b"HTTP/1.0 200")
            s.close()
        except Exception:
            ok = False
        if ok:
            fails = 0
            continue
        fails += 1
        log("自检不通（第 %d 次）" % fails)
        if fails >= 3:
            try:
                log("自检连续 3 次不通，原地重启服务")
                with AWAKE_LOCK:
                    _drop_guard(AWAKE_PROC)
                    _drop_guard(BASE_PROC)
                os.execv(sys.executable, [sys.executable, "-u"] + sys.argv[1:])
            except Exception as e:
                log("自检重启失败：%r" % (e,))


def needs_transcode(path, tr=None):
    """tr 由客户端按浏览器能力传：Safari 能直接解 ALAC 就不转；其它默认转。
    tr=None 时退回服务端全局设置（老页面 / 直接访问接口）"""
    if tr is None:
        tr = 1 if SETTINGS.get("trans") else 0
    if not tr:
        return False
    return would_transcode(path)


DOLBY_CODECS = {"eac3", "ac3", "ac4", "truehd", "mlp", "dts", "dca"}


def is_dolby(path):
    return track_codec(path).strip().lower() in DOLBY_CODECS


def dolby_channels():
    return 6 if SETTINGS.get("dolby") == "51" else 2



# ---------------------------------------------------------------- 杜比全景声标记
ATMOS_CACHE = os.path.join(CACHE_DIR, "atmos.json")
_atmos = None

def _load_atmos():
    global _atmos
    if _atmos is None:
        try:
            with open(ATMOS_CACHE, "r", encoding="utf-8") as f:
                _atmos = json.load(f)
        except Exception:
            _atmos = {}
    return _atmos

def _save_atmos():
    try:
        with LOCK:
            data = dict(_load_atmos())
            tmp = ATMOS_CACHE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, ATMOS_CACHE)
    except Exception:
        pass

def album_atmos(aid):
    """整张专辑是否有 Atmos 音轨：只 probe 第一首杜比曲目，结果落盘缓存。
    - 命中 True 且专辑未变动 -> 直接返回
    - 命中 False 只当 6 小时内且专辑未变动才信；ffprobe 不可用/超时时不写缓存
    这样既不会每次重 probe，也不会把一次失败永久固化成“不是杜比”。"""
    cache = _load_atmos()
    alb = None
    with LOCK:
        for a in ALBUMS:
            if a.get("id") == aid:
                alb = a
                break
    ver = (alb or {}).get("ver") or ""
    ent = cache.get(aid)
    now = int(time.time())
    if isinstance(ent, bool):
        if ent is True:
            return True                    # 老的确定结果
        ent = None                         # 老的 false 一律重测
    if isinstance(ent, dict):
        same = (ent.get("ver") or "") == ver
        if ent.get("v") and same:
            return True
        if same and not ent.get("v") and now - int(ent.get("t") or 0) < 6 * 3600:
            return False
    paths = []
    if alb:
        for t in alb.get("tracks", []):
            fp = TRACKS.get(t.get("id"))
            if fp:
                paths.append(fp)
    probe = os.environ.get("WARMPLAYER_FFPROBE") or shutil.which("ffprobe") or ""
    if not probe:
        return False                       # 不写缓存，免得固化
    result, trusted = False, False
    for fp in paths:
        if not is_dolby(fp):
            continue
        try:
            out = subprocess.run([probe, "-v", "error", "-select_streams", "a:0",
                                  "-show_entries", "stream=profile", "-of", "default=nw=1:nk=1", fp],
                                 capture_output=True, text=True, timeout=20).stdout
            result = "Atmos" in out
            trusted = True
        except Exception:
            trusted = False
        break
    if trusted:
        cache[aid] = {"v": bool(result), "ver": ver, "t": now}
        _save_atmos()
    return result

def dolby_plan(q):
    """auto：客户端能解 E-AC3（车机/原生播放器）就原样给源文件，否则下混。
    也可手动指定 raw / 51 / down。"""
    if (q.get("m") or [""])[0] == "1":
        return "down"          # 电平表专用副本：一律给低码率立体声
    mode = (q.get("dm") or [""])[0] or SETTINGS.get("dolby", "auto")
    if mode not in ("auto", "down", "51", "raw"):
        mode = "auto"
    if mode == "auto":
        return "raw" if (q.get("d") or [""])[0] == "1" else "down"
    return mode


def remix_target(path, ch):
    st = os.stat(path)
    tid = short_hash(path, 12)
    rate = 44100 if ch == 2 else 48000
    return os.path.join(TRANS_DIR, "%s-%d-%d-aac%d-%d.m4a"
                        % (tid, int(st.st_mtime), st.st_size, ch, rate))


def ensure_dolby(path, ch=None):
    """杜比（E-AC3 / AC-4 / TrueHD）浏览器解不好：转成 AAC。
    2 声道走标准 ITU 下混（含中置），6 声道保留 5.1 交给系统做空间音频。"""
    if not FFMPEG or not is_dolby(path):
        return None
    ch = ch or dolby_channels()
    out = remix_target(path, ch)
    if os.path.exists(out) and os.path.getsize(out) > 4096:
        return out
    with TRANS_LOCK:
        busy = TRANS_BUSY.get(out)
        if busy is None:
            busy = threading.Lock()
            TRANS_BUSY[out] = busy
    with busy:
        if os.path.exists(out) and os.path.getsize(out) > 4096:
            return out
        os.makedirs(TRANS_DIR, exist_ok=True)
        tmp = out + ".part"
        t0 = time.time()
        try:
            r = subprocess.run([FFMPEG, "-v", "error", "-y", "-i", path, "-vn", "-sn", "-map", "0:a:0",
                                "-c:a", "aac", "-b:a", ("384k" if ch == 6 else "256k"),
                                "-ar", ("44100" if ch == 2 else "48000"), "-ac", str(ch),
                                "-movflags", "+faststart",
                                "-f", "mp4", tmp],
                               timeout=600, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            if r.returncode != 0:
                log("杜比下混失败 %s：%s" % (os.path.basename(path),
                                          (r.stderr or b"").decode("utf-8", "ignore")[:160]))
                return None
            os.replace(tmp, out)
            log("杜比 %d 声道 → AAC%s 完成：%s（%.1fs）"
                % (ch, "5.1" if ch == 6 else "立体声", os.path.basename(path), time.time() - t0))
        except Exception as e:
            log("杜比下混异常 %s：%s" % (os.path.basename(path), e))
            return None
        return out


# ---------------------------------------------------------------- 杜比流式（边转边发）
DOLBY_MP3_JOBS = {}
DOLBY_MP3_LOCK = threading.Lock()


def dolby_mp3_target(path):
    st = os.stat(path)
    tid = short_hash(path, 12)
    return os.path.join(TRANS_DIR, "%s-%d-%d-dol.mp3" % (tid, int(st.st_mtime), st.st_size))


def dolby_mp3_ok(out):
    return (os.path.exists(out + ".ok") and os.path.exists(out)
            and os.path.getsize(out) > 4096)


def dolby_mp3_job(path, out):
    """点火一个流式转码任务（写 out，边写边可读）；返回 state dict"""
    fail = out + ".fail"
    with DOLBY_MP3_LOCK:
        st = DOLBY_MP3_JOBS.get(out)
        if st and not st.get("done"):
            return st
        if dolby_mp3_ok(out):
            st = {"done": True, "ok": True, "out": out}
            DOLBY_MP3_JOBS[out] = st
            return st
        if not FFMPEG:
            return {"done": True, "ok": False, "out": out}
        os.makedirs(TRANS_DIR, exist_ok=True)
        for f in (out + ".ok", fail):
            try:
                if os.path.exists(f):
                    os.remove(f)
            except OSError:
                pass
        try:
            p = subprocess.Popen(
                [FFMPEG, "-v", "error", "-y", "-i", path, "-vn", "-sn", "-map", "0:a:0",
                 "-c:a", "libmp3lame", "-b:a", "192k", "-ar", "44100", "-ac", "2",
                 "-f", "mp3", "-"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        except Exception as e:
            log("杜比流式启动失败：%r" % (e,))
            return {"done": True, "ok": False, "out": out}
        st = {"done": False, "ok": False, "out": out, "t0": time.time()}
        DOLBY_MP3_JOBS[out] = st

    def pump():
        ok = False
        try:
            with open(out, "wb") as f:
                while True:
                    b = p.stdout.read(65536)
                    if not b:
                        break
                    f.write(b)
        except Exception as e:
            log("杜比流式写盘异常：%r" % (e,))
        finally:
            try:
                p.stdout.close()
            except Exception:
                pass
            rc = p.wait()
            ok = (rc == 0 and os.path.exists(out) and os.path.getsize(out) > 4096)
            if ok:
                try:
                    with open(out + ".ok", "wb") as f:
                        f.write(b"1")
                except OSError:
                    pass
                log("杜比流式完成：%s（%.1fs）"
                    % (os.path.basename(path), time.time() - st["t0"]))
            else:
                try:
                    with open(fail, "wb") as f:
                        f.write(b"%d" % rc)
                except OSError:
                    pass
                log("杜比流式失败：%s（rc=%d）" % (os.path.basename(path), rc))
            st["done"] = True
            st["ok"] = ok

    threading.Thread(target=pump, daemon=True).start()
    return st


def raw_target(path):
    """把源文件里的封面视频流剥掉，只留原始 E-AC3 音频（-c:a copy，不重编码）。
    车机/原生播放器直接把这份当 Dolby 源播。"""
    st = os.stat(path)
    tid = short_hash(path, 12)
    return os.path.join(TRANS_DIR, "%s-%d-%d-raw.m4a" % (tid, int(st.st_mtime), st.st_size))


def ensure_raw(path):
    if not FFMPEG:
        return path
    out = raw_target(path)
    if os.path.exists(out) and os.path.getsize(out) > 4096:
        return out
    with TRANS_LOCK:
        busy = TRANS_BUSY.get(out)
        if busy is None:
            busy = threading.Lock()
            TRANS_BUSY[out] = busy
    with busy:
        if os.path.exists(out) and os.path.getsize(out) > 4096:
            return out
        os.makedirs(TRANS_DIR, exist_ok=True)
        tmp = out + ".part"
        t0 = time.time()
        try:
            r = subprocess.run([FFMPEG, "-v", "error", "-y", "-i", path, "-vn", "-sn", "-map", "0:a:0",
                                "-c:a", "copy", "-movflags", "+faststart",
                                "-f", "mp4", tmp],
                               timeout=180, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            if r.returncode != 0:
                log("原样封装失败 %s：%s" % (os.path.basename(path),
                                          (r.stderr or b"").decode("utf-8", "ignore")[:160]))
                return path
            os.replace(tmp, out)
            log("原样 E-AC3 封装完成：%s（%.1fs）" % (os.path.basename(path), time.time() - t0))
        except Exception as e:
            log("原样封装异常 %s：%s" % (os.path.basename(path), e))
            return path
        return out


def trans_target(path):
    st = os.stat(path)
    tid = short_hash(path, 12)
    return os.path.join(TRANS_DIR, "%s-%d-%d.mp3" % (tid, int(st.st_mtime), st.st_size))


def ensure_transcode(path):
    """返回可直接播的 mp3 路径；失败返回 None"""
    if not FFMPEG:
        return None
    out = trans_target(path)
    if os.path.exists(out) and os.path.getsize(out) > 4096:
        return out
    with TRANS_LOCK:
        busy = TRANS_BUSY.get(out)
        if busy is None:
            busy = threading.Lock()
            TRANS_BUSY[out] = busy
    with busy:
        if os.path.exists(out) and os.path.getsize(out) > 4096:
            return out
        os.makedirs(TRANS_DIR, exist_ok=True)
        tmp = out + ".part"
        try:
            r = subprocess.run([FFMPEG, "-v", "error", "-y", "-i", path, "-vn", "-sn", "-map", "0:a:0",
                                "-c:a", "libmp3lame", "-b:a", "192k", "-ar", "44100", "-ac", "2", "-f", "mp3", tmp],
                               timeout=240, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            if r.returncode != 0:
                print("[trans] fail %s: %s" % (os.path.basename(path),
                                               (r.stderr or b"").decode("utf-8", "ignore")[:200]))
                return None
        except Exception as e:
            print("[trans] error %s: %r" % (os.path.basename(path), e))
            return None
        if os.path.exists(tmp) and os.path.getsize(tmp) > 4096:
            os.replace(tmp, out)
            return out
        try:
            os.remove(tmp)
        except OSError:
            pass
    return None


# ---------------------------------------------------------------- 封面与主色
def cover_path(album_id):
    for ext in (".jpg", ".png", ".webp"):
        p = os.path.join(COVER_DIR, album_id + ext)
        if os.path.exists(p):
            return p
    return None


def thumb_path(album_id):
    p = os.path.join(THUMB_DIR, album_id + ".jpg")
    if os.path.exists(p):
        return p
    src = cover_path(album_id)
    if not src:
        return None
    if not SIPS:
        return src
    os.makedirs(THUMB_DIR, exist_ok=True)
    tmp = p + ".tmp"
    try:
        subprocess.run([SIPS, "-s", "format", "jpeg", "-s", "formatOptions", "78",
                        "-Z", "420", src, "--out", tmp],
                       check=True, capture_output=True, timeout=20)
        os.replace(tmp, p)
        return p
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return src


def save_cover(album_id, data, mime):
    os.makedirs(COVER_DIR, exist_ok=True)
    raw_ext = ".png" if mime == "image/png" else ".jpg"
    raw = os.path.join(COVER_DIR, album_id + raw_ext)
    with open(raw, "wb") as f:
        f.write(data)
    if SIPS:
        stamp = os.path.join(COVER_DIR, album_id + ".jpg")
        try:
            subprocess.run([SIPS, "-s", "format", "jpeg", "-Z", "820", raw, "--out", stamp],
                           check=True, capture_output=True, timeout=20)
            if os.path.getsize(stamp) > 0:
                trim_white_bars(stamp)
                if raw_ext != ".jpg" and os.path.exists(raw):
                    os.remove(raw)
                return stamp
        except Exception:
            pass
    return raw


def trim_white_bars(path):
    """封面图里若左右各有一条纯白竖边（扫描件常见），裁掉它们，让画面尽量铺满。
    只在两侧白边宽度接近时才动手，避免误裁本身带白框的设计。"""
    try:
        from PIL import Image
        im = Image.open(path).convert("RGB")
        w, h = im.size
        if w < 80 or h < 80:
            return
        step = max(1, h // 48)

        def white_col(x):
            for y in range(0, h, step):
                p = im.getpixel((x, y))
                if min(p) < 240:
                    return False
            return True

        l = 0
        while l < int(w * 0.42) and white_col(l):
            l += 1
        r = w - 1
        while r > int(w * 0.58) and white_col(r):
            r -= 1
        lb, rb = l, w - 1 - r
        if lb < int(w * 0.06) or rb < int(w * 0.06):
            return
        if abs(lb - rb) > int(w * 0.04):
            return
        if white_col(min(w - 1, l + 2)) and lb > int(w * 0.30):
            return
        box = (max(0, l - 1), 0, min(w, r + 2), h)
        im.crop(box).save(path, "JPEG", quality=90, optimize=True)
    except Exception:
        pass


def avg_tint(img_path):
    """ffmpeg 缩到 1 像素取平均色；没有 ffmpeg 就返回 None"""
    if not FFMPEG:
        return None
    try:
        r = subprocess.run([FFMPEG, "-v", "error", "-i", img_path, "-vf", "scale=1:1",
                            "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                           capture_output=True, timeout=10)
        if r.returncode == 0 and len(r.stdout) >= 3:
            r_, g_, b_ = r.stdout[0], r.stdout[1], r.stdout[2]
            # 稍微提亮，避免侧缝发黑
            mx = max(r_, g_, b_)
            if mx < 46:
                k = 46.0 / max(mx, 1)
                r_, g_, b_ = int(r_ * k), int(g_ * k), int(b_ * k)
            return "#%02x%02x%02x" % (min(r_, 255), min(g_, 255), min(b_, 255))
    except Exception:
        return None
    return None


def extract_cover_now(aid):
    """缓存没有就现抽一张（扫描没跑完时页面也能出封面）。串行防重复抽。"""
    with COVER_LOCK:
        cp = cover_path(aid)
        if cp:
            return cp
        if not HOLDERS and TAGS:
            with LOCK:
                for p, e in TAGS.items():
                    if e.get("hasCover"):
                        HOLDERS.setdefault(short_hash(album_key(p)[2], 12), p)
        holder = HOLDERS.get(aid)
        if not holder:
            # 扫描还没到这张专辑：拿任意一首直接读（read_cover 自带标签解析）
            if not ANYCOVER and TAGS:
                with LOCK:
                    for p in TAGS:
                        ANYCOVER.setdefault(short_hash(album_key(p)[2], 12), p)
            holder = ANYCOVER.get(aid)
        if not holder:
            return None
        try:
            raw = read_cover(holder)
            if not raw:
                return None
            cp = save_cover(aid, raw, sniff_image(raw))
            tint = avg_tint(cp) if cp else None
            with LOCK:
                for a in ALBUMS:
                    if a["id"] == aid:
                        a["cover"] = True
                        a["ver"] = short_hash(aid + str(time.time()), 6)
                        if tint:
                            a["tint"] = tint
                            TINTS[aid] = tint
                        break
            return cp
        except Exception as e:
            log("按需抽封面失败 %s：%r" % (aid, e))
            return None


# ---------------------------------------------------------------- 扫描
def iter_audio(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in ("__MACOSX",)]
        for fn in filenames:
            if fn.startswith("."):
                continue
            ext = os.path.splitext(fn)[1].lower()
            if ext in AUDIO_EXT:
                yield os.path.join(dirpath, fn)


def file_entry(path):
    st = os.stat(path)
    key = (int(st.st_mtime), st.st_size)
    cached = TAGS.get(path)
    if cached and (cached.get("m"), cached.get("s")) == key and cached.get("v") == TAG_VER:
        return cached
    tags, cover = read_tags(path)
    entry = {"m": key[0], "s": key[1], "v": TAG_VER, "tags": tags,
             "hasCover": bool(cover) or bool(tags.get("hasCovr"))}
    if cover:
        entry["coverRaw"] = {"mime": tags.get("coverMime") or sniff_image(cover), "len": len(cover)}
    return entry


def guess_tags(path):
    """不读文件、只按目录/文件名先猜一版标签：网络卷上能秒出唱片墙。"""
    fn = os.path.basename(path)
    stem = os.path.splitext(fn)[0]
    album = os.path.basename(os.path.dirname(path)) or "未命名唱片"
    artist = os.path.basename(os.path.dirname(os.path.dirname(path))) or "未知艺术家"
    track = 0
    title = stem
    m = re.match(r"^\s*0*(\d{1,3})\s*[-._、]?\s+(.*)$", stem)
    if m:
        track = int(m.group(1)); title = m.group(2)
    return {"title": title.strip(), "artist": "", "albumartist": artist, "album": album,
            "year": "", "track": track, "genre": "", "dur": 0.0}


def album_key(p):
    e = TAGS.get(p) or {}
    t = e.get("tags") or {}
    album = (t.get("album") or "").strip()
    if not album:
        album = os.path.basename(os.path.dirname(p)) or "未命名唱片"
    artist = (t.get("albumartist") or "").strip() or (t.get("artist") or "").strip()
    if not artist:
        artist = os.path.basename(os.path.dirname(os.path.dirname(p))) or "未知艺术家"
    return album, artist, album.lower() + "|" + artist.lower()


def build_albums(paths, fast=False):
    groups = {}
    for p in paths:
        e = TAGS.get(p)
        if not e:
            continue
        t = e["tags"]
        album, artist, key = album_key(p)

        g = groups.setdefault(key, {"album": album, "artist": artist, "year": "", "genre": "", "copyright": "", "paths": []})
        g["paths"].append(p)
        if not g["year"] and t.get("year"):
            y = re.search(r"(19|20)\d{2}", str(t.get("year")))
            g["year"] = y.group(0) if y else ""
        if not g["genre"] and t.get("genre"):
            g["genre"] = t["genre"]
        if not g["copyright"] and t.get("copyright"):
            g["copyright"] = str(t["copyright"]).replace("\x00", " ").strip()
    albums = []
    for key, g in groups.items():
        files = sorted(g["paths"], key=lambda p: (TAGS[p]["tags"].get("track") or 999, os.path.basename(p).lower()))
        aid = short_hash(key, 12)
        if aid not in SEEN:
            if fast:
                SEEN[aid] = int(time.time())
            else:
                try:
                    SEEN[aid] = int(max(os.path.getmtime(p) for p in files))
                except Exception:
                    SEEN[aid] = int(time.time())
        cover = cover_path(aid)
        if not cover and not fast:
            holder = [p for p in files if TAGS[p].get("hasCover")]
            if holder:
                raw = read_cover(holder[0])
                if raw:
                    cover = save_cover(aid, raw, TAGS[holder[0]]["tags"].get("coverMime") or sniff_image(raw))
        tracks = []
        total = 0.0
        for i, p in enumerate(files):
            t = TAGS[p]["tags"]
            dur = float(t.get("dur") or 0)
            total += dur
            tid = short_hash(p, 12)
            TRACKS[tid] = p
            tracks.append({
                "id": tid,
                "title": (t.get("title") or "").strip() or os.path.splitext(os.path.basename(p))[0],
                "artist": (t.get("artist") or "").strip(),
                "dur": round(dur, 2),
                "url": "/api/audio/" + tid + "?x=" + urllib.parse.quote(os.path.splitext(os.path.basename(p))[0][:40]),
                "t": (2 if is_dolby(p) else (1 if would_transcode(p) else 0)),
            })
        tint = TINTS.get(aid)
        if tint is None and cover and not fast:
            tint = avg_tint(cover)
            if tint:
                TINTS[aid] = tint
        albums.append({
            "id": aid, "artist": g["artist"], "title": g["album"], "year": g["year"],
            "genre": g["genre"], "copyright": g["copyright"], "count": len(tracks), "total": round(total, 1),
            "cover": bool(cover), "tint": tint or "",
            "ct": (int(os.path.getmtime(cover)) if cover and os.path.exists(cover) else 0),
            "ver": short_hash(key + str(len(tracks)) + str(int(total)), 6),
            "add": SEEN.get(aid, 0),
            "tracks": tracks,
        })
    albums.sort(key=lambda a: (a["artist"].lower(), a["year"] or "9999", a["title"].lower()))
    return albums


def enabled_paths():
    """所有「开着的」目录的音频路径并集；用缓存的路径，不再依赖上一次扫的是谁。"""
    out = []
    for f in FOLDERS:
        if f.get("enabled", True):
            out.extend(PATHS.get(f["id"], []))
    return out


def refresh_paths(fdir):
    """只走目录、不读标签：几秒钟把这张目录的曲目列表登记下来，唱片墙立刻能出。"""
    try:
        got = list(iter_audio(fdir["path"]))
    except Exception as e:
        log("登记目录失败：%s（%s）" % (fdir["path"], e))
        return 0
    with LOCK:
        PATHS[fdir["id"]] = got
        for p in got:
            if p not in TAGS:
                TAGS[p] = {"m": 0, "s": 0, "v": "", "tags": guess_tags(p), "hasCover": False}
        save_json(PATHS_FILE, PATHS)
    log("登记目录：%s（%d 个音频文件）" % (fdir["name"], len(got)))
    return len(got)


def rebuild_albums():
    global ALBUMS
    with LOCK:
        ALBUMS = build_albums(enabled_paths(), fast=True)
    return ALBUMS


def do_scan(folder=None):
    # 单飞：大曲库一轮扫描几十分钟，期间加目录/点重扫会再起一个扫描线程，
    # 两线程同写 TAGS/SCAN/缓存文件是并发崩溃的根源。重复触发直接忽略。
    if not SCAN_GUARD.acquire(blocking=False):
        log("已有扫描在进行，忽略重复触发")
        return
    try:
        return _do_scan_inner(folder)
    except Exception as e:
        log("扫描被异常中断（状态已复位，可重新扫描）：%r" % e)
    finally:
        with LOCK:
            SCAN.update({"active": False, "stage": ""})
        SCAN_GUARD.release()


def _do_scan_inner(folder=None):
    t0 = time.time()
    with LOCK:
        SCAN.update({"active": True, "folderName": (folder["name"] if folder else "全部目录"), "done": 0, "total": 0})
    targets = [folder] if folder else [f for f in FOLDERS if f.get("enabled", True)]
    all_paths = []
    log("扫描开始：%s（%d 个目录）" % ("、".join(f["name"] for f in targets), len(targets)))
    for fdir in targets:
        if not os.path.isdir(fdir["path"]):
            log("跳过（目录不存在或没挂载）：%s" % fdir["path"])
            continue
        log("遍历目录：%s  %s" % (fdir["name"], fdir["path"]))
        n0 = len(all_paths)
        got = list(iter_audio(fdir["path"]))
        all_paths.extend(got)
        with LOCK:
            PATHS[fdir["id"]] = got
            save_json(PATHS_FILE, PATHS)
        log("   目录内找到 %d 个音频文件（累计 %d）" % (len(all_paths) - n0, len(all_paths)))
    # 去重：始终以「所有开着的目录」的并集为准
    seen = set()
    uniq = []
    for p in enabled_paths():
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    with LOCK:
        SCAN["total"] = len(uniq)

    # 先用「目录名 + 文件名」占位，立刻把唱片墙填上（这时候一个字节都不用读）
    guessed = 0
    with LOCK:
        for p in uniq:
            if p not in TAGS:
                TAGS[p] = {"m": 0, "s": 0, "v": 0, "tags": guess_tags(p), "hasCover": False}
                guessed += 1
    global ALBUMS
    fresh = build_albums(uniq, fast=True)
    with LOCK:
        ALBUMS = fresh
    log("先用目录名出了 %d 张专辑 / %d 首（其中 %d 首还没读标签），标签在后台继续解析"
        % (len(ALBUMS), len(uniq), guessed))

    done = 0
    last_pub = [0]

    def work(p):
        nonlocal done
        global ALBUMS
        try:
            entry = file_entry(p)
        except Exception:
            entry = None
        with LOCK:
            if entry is not None:
                TAGS[p] = entry
                if entry.get("hasCover"):
                    HOLDERS[short_hash(album_key(p)[2], 12)] = p
            done += 1
            SCAN["done"] = done
        # 每 300 首把结果发布一次，页面边扫边涨
        if done - last_pub[0] >= 300:
            last_pub[0] = done
            fresh2 = build_albums(uniq, fast=True)
            with LOCK:
                ALBUMS = fresh2
            if done % 900 == 0:
                save_json(TAGS_FILE, TAGS)
        if done % 500 == 0 or done == SCAN.get("total"):
            tot = SCAN.get("total") or 0
            el = time.time() - t0
            pct = (done * 100.0 / tot) if tot else 0
            eta = (el * (tot - done) / done) if done else 0
            log("   解析标签 %d / %d（%.0f%%，已用 %.0fs，预计还需 %.0fs） 当前已出 %d 张专辑"
                % (done, tot, pct, el, eta, len(ALBUMS)))

    # 网络卷（AFP/SMB）每个文件的标签读取都是网络往返，多开线程明显更快
    on_volume = any(f["path"].startswith("/Volumes/") for f in targets)
    workers = 24 if on_volume else 8
    with ThreadPoolExecutor(max_workers=max(8, workers)) as ex:
        # 逐项收结果：单首异常只跳过该首，不再把整轮扫描炸死（ex.map 会抛断迭代）
        futs = [ex.submit(work, p) for p in uniq]
        for fu in as_completed(futs):
            try:
                fu.result()
            except Exception as e:
                log("扫描单项异常（跳过该首，不影响整轮）：%r" % e)

    # 清掉已被删掉的路径
    existing = set(uniq)
    with LOCK:
        scanned = [f["path"].rstrip("/") for f in targets]
        for p in list(TAGS.keys()):
            if p not in existing and any(p == sp or p.startswith(sp + "/") for sp in scanned):
                TAGS.pop(p, None)
        save_json(TAGS_FILE, TAGS)
        ALBUMS = build_albums(list(existing), fast=True)
    log("标签解析完成：%d 张专辑 / %d 首，用时 %.1fs" % (len(ALBUMS), len(TRACKS), time.time() - t0))

    # 封面单独后台补：每张专辑只挑一首有封面的文件去读，绝不阻塞接口
    # 先用解析完成的标签重建一次快照：todo 必须基于真实专辑键，
    # 不能沿用扫描中途「占位键+真实键」混合的旧 ALBUMS（否则补错键、真键漏补）
    with LOCK:
        ALBUMS = build_albums(list(existing), fast=True)
    holders = {}
    for p in existing:
        if TAGS.get(p, {}).get("hasCover"):
            aid = short_hash(album_key(p)[2], 12)
            holders.setdefault(aid, p)
    todo = [a for a in ALBUMS if not a.get("cover") and a["id"] in holders]
    with LOCK:
        SCAN.update({"stage": "封面", "coverDone": 0, "coverTotal": len(todo)})
    log("开始补封面：%d 张专辑需要抽封面（后台进行，页面可正常用）" % len(todo))
    for i, a in enumerate(todo, 1):
        try:
            raw = read_cover(holders[a["id"]])
            if raw:
                cp = save_cover(a["id"], raw, sniff_image(raw))
                tint = avg_tint(cp) if cp else None
                with LOCK:
                    a["cover"] = True
                    a["ver"] = short_hash(a["id"] + str(time.time()), 6)
                    if tint:
                        a["tint"] = tint
                        TINTS[a["id"]] = tint
        except Exception:
            pass
        with LOCK:
            SCAN["coverDone"] = i
        if i % 40 == 0 or i == len(todo):
            log("   补封面 %d / %d" % (i, len(todo)))

    with LOCK:
        # 收尾再重建：封面标志/主色以缓存文件为准，状态与磁盘一致
        ALBUMS = build_albums(list(existing), fast=True)
        save_json(TINT_FILE, TINTS)
        save_json(SEEN_FILE, SEEN)
        SCAN.update({"active": False, "stage": "", "done": done, "total": len(uniq)})
    log("扫描全部完成：%d 张专辑 / %d 首，总用时 %.1fs" % (len(ALBUMS), len(TRACKS), time.time() - t0))
    return ALBUMS


def state_payload(local=True):
    with LOCK:
        counts = {}
        for a in ALBUMS:
            for t in a["tracks"]:
                p = TRACKS.get(t["id"])
                if not p:
                    continue
                for f in FOLDERS:
                    if p.startswith(f["path"].rstrip("/") + "/") or p.startswith(f["path"]):
                        counts[f["id"]] = counts.get(f["id"], 0) + 1
        folders = [dict(f, count=counts.get(f["id"], 0)) for f in FOLDERS]
        off = [f["path"].rstrip("/") for f in FOLDERS if not f.get("enabled", True)]
        albums = ALBUMS
        if off:
            def hidden(path):
                return any(path == o or path.startswith(o + "/") for o in off)
            albums = []
            for a in ALBUMS:
                keep = [t for t in a["tracks"] if not hidden(TRACKS.get(t["id"], ""))]
                if keep:
                    albums.append(dict(a, tracks=keep))
        if not local:
            folders = [{"id": f["id"], "enabled": f.get("enabled", True), "count": f.get("count", 0)} for f in folders]
        return {
            "folders": folders,
            "albums": albums,
            "scan": dict(SCAN),
            "cover": bool(SIPS),
            "local": bool(local),
            "locked": not bool(local),
        }


# ---------------------------------------------------------------- 配置
def load_folders():
    global FOLDERS
    data = load_json(CONF_FILE, {"folders": []})
    if isinstance(data, list):
        data = {"folders": data}
    FOLDERS = []
    for f in data.get("folders", []):
        p = os.path.expanduser(f.get("path", ""))
        if os.path.isdir(p):
            FOLDERS.append({"id": short_hash(p, 10), "name": f.get("name") or os.path.basename(p.rstrip("/")) or p,
                            "path": p, "enabled": bool(f.get("enabled", True))})
    return FOLDERS


def save_folders():
    save_json(CONF_FILE, {"folders": [{"id": f["id"], "name": f["name"], "path": f["path"],
                                      "enabled": bool(f.get("enabled", True))} for f in FOLDERS]})


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "VinylLocal/1.0"
    protocol_version = "HTTP/1.1"
    # keep-alive 空闲连接不该永久占着一个线程/一个 fd：25s 没动静就收掉。
    # 这也让"客户端走掉但服务端还在 readline"的连接能被及时关掉，不会堆成 CLOSE_WAIT。
    timeout = 25

    def log_message(self, fmt, *args):
        pass

    # ---- 工具
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _err(self, msg, code=400):
        self._json({"error": msg}, code)

    # ---- 访问来源与目录锁
    def _client_local(self):
        ip = (self.client_address or ("",))[0]
        if ip.startswith("::ffff:"):
            ip = ip[7:]
        if ip in ("::1", "127.0.0.1") or ip.startswith("127.") or ip.startswith("192.168.") \
           or ip.startswith("10.") or ip.startswith("169.254."):
            return True
        if ip.startswith("172."):
            try:
                second = int(ip.split(".")[1])
                return 16 <= second <= 31
            except Exception:
                return False
        return False

    def _pwd_ok(self):
        if self._client_local():
            return True
        key = (self.headers.get("X-Vinyl-Key") or "").strip()
        want = str(SETTINGS.get("folder_pwd") or DEFAULT_FOLDER_PWD)
        return bool(key) and key == want

    def _need_key(self):
        if self._pwd_ok():
            return False
        self._err("需要密码才能打开音乐目录", 403)
        return True

    def _read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def _send_file(self, path, ctype, cache=False):
        if not os.path.isfile(path):
            self._err("文件不存在", 404)
            return
        size = os.path.getsize(path)
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            start = int(m.group(1)) if m and m.group(1) else 0
            end = int(m.group(2)) if m and m.group(2) else size - 1
            if start >= size:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % size)
                self.end_headers()
                return
            end = min(end, size - 1)
            length = end - start + 1
            self.send_response(206)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(length))
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            with open(path, "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(262144, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "public, max-age=86400" if cache else "no-store")
        self.end_headers()
        # Stream the file in bounded chunks; do not hold an entire album in RAM.
        try:
            with open(path, "rb") as f:
                while True:
                    chunk = f.read(262144)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _serve_dolby_mp3(self, fp):
        out = dolby_mp3_target(fp)
        if dolby_mp3_ok(out):
            self._send_file(out, "audio/mpeg")
            return True
        st = dolby_mp3_job(fp, out)
        if not st or not st.get("out"):
            return False
        self._stream_growing(out, st, "audio/mpeg")
        return True

    def _stream_growing(self, out, st, ctype):
        """边转码边发送：读正在增长的 mp3，用 chunked 连续吐给浏览器"""
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        sent = 0
        deadline = time.time() + 900
        try:
            while True:
                try:
                    size = os.path.getsize(out)
                except OSError:
                    size = 0
                if size > sent:
                    with open(out, "rb") as f:
                        f.seek(sent)
                        while True:
                            b = f.read(65536)
                            if not b:
                                break
                            self.wfile.write(b"%x\r\n" % len(b))
                            self.wfile.write(b)
                            self.wfile.write(b"\r\n")
                            sent += len(b)
                if st.get("done"):
                    try:
                        size = os.path.getsize(out)
                    except OSError:
                        size = 0
                    if size <= sent:
                        break
                    continue
                if time.time() > deadline:
                    break
                time.sleep(0.04)
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            # A client that seeks, changes track, or closes its tab must not
            # leave the transcode stream handler spinning for up to 15 minutes.
            pass

    # ---- 路由
    # ---------- 服务端频谱：按歌曲时间逐帧算好，按实时节奏推给前端 ----------
    def api_meter(self, fp, t0):
        import json, math
        if not FFMPEG:
            self._err("没有 ffmpeg", 503)
            return
        try:
            import numpy as np
        except Exception:
            self._err("没有 numpy", 503)
            return
        if not METER_SLOTS.acquire(blocking=False):
            self._err("频谱服务繁忙，请稍后重试", 503)
            return
        SR, HOP, WIN = 22050, 735, 2048          # 30 帧/秒
        ratio = (16000.0 / 20.0) ** (1.0 / 21.0)
        edges = [20.0 * (ratio ** i) for i in range(22)]
        nb = 1 + WIN // 2
        bin_hz = SR / float(WIN)
        bands = []
        for i in range(21):
            lo = max(1, int(math.floor(edges[i] / bin_hz)))
            hi = max(lo + 1, int(math.ceil(edges[i + 1] / bin_hz)))
            bands.append((min(lo, nb - 1), min(hi, nb)))
        log("频谱流开启：《%s》 @%.1fs" % (os.path.basename(fp)[:36], t0))
        try:
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception:
            pass
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            proc = subprocess.Popen(
                [FFMPEG, "-v", "error", "-ss", "%.3f" % max(0.0, t0 - 0.25), "-i", fp,
                 "-vn", "-sn", "-map", "0:a:0", "-ac", "2", "-ar", str(SR),
                 "-f", "f32le", "-acodec", "pcm_f32le", "-"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        except Exception as e:
            METER_SLOTS.release()
            log("频谱启动失败：%r" % (e,))
            return
        win = np.hanning(WIN).astype(np.float32)
        cond = threading.Condition()
        st = {"buf": np.zeros(0, dtype=np.float32), "done": False}

        def _reader():
            # 后台把 ffmpeg 的管道持续读空：macOS 管道只有 ~16KB，边读边算会让发送节奏抽一下
            try:
                while True:
                    with cond:
                        while st["buf"].shape[0] > SR * 8:      # 屯超过 8s 就先等一等
                            cond.wait(0.05)
                    c = proc.stdout.read(65536)                 # 16384 采样 ≈ 0.74s
                    if not c:
                        break
                    arr = np.frombuffer(c, dtype="<f4")
                    with cond:
                        st["buf"] = np.concatenate([st["buf"], arr])
                        cond.notify_all()
            except Exception:
                pass
            finally:
                with cond:
                    st["done"] = True
                    cond.notify_all()

        threading.Thread(target=_reader, daemon=True).start()
        idx = 0
        seek0 = max(0.0, t0 - 0.25)
        wall0 = time.time()
        LEAD = 1.5                 # 只要求比实时超前 2s：客户端按 currentTime 取最近帧，超前无害
        try:
            while True:
                need = WIN * 2
                _dbg0 = time.time() if METER_DEBUG else 0.0
                _dbgav = 0
                _t1 = time.time() if METER_DEBUG else 0.0
                with cond:
                    while st["buf"].shape[0] < need and not st["done"]:
                        cond.wait(0.05)
                    if st["buf"].shape[0] < need:
                        break
                    _dbgav = int(st["buf"].shape[0])
                    seg = st["buf"][:need].reshape(-1, 2)
                    st["buf"] = st["buf"][HOP * 2:]
                _t2 = time.time() if METER_DEBUG else 0.0
                L = seg[:, 0]; R = seg[:, 1]
                rmsL = 20.0 * math.log10(float(np.sqrt(np.mean(L * L))) + 1e-9)
                rmsR = 20.0 * math.log10(float(np.sqrt(np.mean(R * R))) + 1e-9)
                pkL = 20.0 * math.log10(float(np.max(np.abs(L))) + 1e-9)
                pkR = 20.0 * math.log10(float(np.max(np.abs(R))) + 1e-9)
                out = {}
                for ch, sig in (("l", L), ("r", R)):
                    mag = np.abs(np.fft.rfft(sig * win)) / (WIN / 2.0)
                    db = 20.0 * np.log10(mag + 1e-9)
                    vals = []
                    for lo, hi in bands:
                        v = float(np.max(db[lo:hi])) if hi > lo else -90.0
                        b = int(round((v + 90.0) / 80.0 * 255.0))
                        vals.append(0 if b < 0 else (255 if b > 255 else b))
                    out[ch] = vals
                frame_t = seek0 + idx * HOP / float(SR)
                line = json.dumps({"t": round(frame_t, 3), "l": out["l"], "r": out["r"],
                                   "rl": round(rmsL, 1), "rr": round(rmsR, 1),
                                   "pl": round(pkL, 1), "pr": round(pkR, 1)},
                                  separators=(",", ":"))
                self.wfile.write((line + "\n").encode("utf-8"))
                try:
                    self.wfile.flush()
                except Exception:
                    pass
                idx += 1
                ahead = idx * HOP / float(SR) - (time.time() - wall0)
                if ahead > LEAD:
                    time.sleep(min(0.25, ahead - LEAD))
                if METER_DEBUG and idx <= 70:
                    log("meter 帧 %d 等数据 %.1fms 计算 %.1fms avail=%d" % (
                        idx, (_t2 - _t1) * 1000, (time.time() - _t2) * 1000, _dbgav))
                elif METER_DEBUG:
                    _gap = time.time() - _dbg0
                    if _gap > 0.06:
                        log("meter 慢帧 %.0fms avail=%d" % (_gap * 1000, _dbgav))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log("频谱推送异常：%r" % (e,))
        finally:
            with cond:
                st["done"] = True
                cond.notify_all()
            try:
                proc.kill()
            except Exception:
                pass
            try:
                proc.stdout.close()
            except Exception:
                pass
            try:
                proc.wait(timeout=1)
            except Exception:
                pass
            METER_SLOTS.release()

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        p = urllib.parse.unquote(u.path)
        q = urllib.parse.parse_qs(u.query)

        if p in ("/tape", "/tape.html", "/磁带机.html"):
            self._send_file(TAPE_FILE, "text/html; charset=utf-8")
            return
        if p in ("/", "/index.html"):
            ua = self.headers.get("User-Agent", "") or ""
            if q.get("vw"):
                log("访问端视口：%s x %s  dpr=%s  car=%s  UA：%s" % (
                    (q.get("vw") or ["?"])[0], (q.get("vh") or ["?"])[0],
                    (q.get("dpr") or ["?"])[0], (q.get("car") or ["?"])[0], ua[:120]))
            else:
                log("访问端 UA：%s" % ua[:220])
            self._send_file(PAGE_FILE, "text/html; charset=utf-8")
            return
        if p == "/api/health":
            try:
                import numpy as _np
                nv = _np.__version__
            except Exception:
                nv = None
            self._json({"ok": True, "py": sys.version.split()[0], "ffmpeg": FFMPEG,
                        "numpy": nv, "albums": len(ALBUMS),
                        "uptime": int(time.time() - START_TS)})
            return
        if p == "/api/state":
            self._json(state_payload(self._client_local()))
            return
        if p == "/api/folders":
            if self._need_key():
                return
            with LOCK:
                self._json({"folders": [dict(f) for f in FOLDERS]})
            return
        if p == "/api/browse":
            if self._need_key():
                return
            self.api_browse((q.get("path") or ["~"])[0])
            return
        if p == "/api/volumes":
            if self._need_key():
                return
            self.api_volumes()
            return
        if p.startswith("/api/thumb/"):
            aid = re.sub(r"[^0-9a-f]", "", p.split("/")[-1])
            tp = thumb_path(aid) if aid else None
            if not tp:
                self._err("没有封面", 404)
                return
            self._send_file(tp, "image/jpeg", cache=True)
            return
        if p.startswith("/api/cover/"):
            aid = p.split("/")[-1]
            aid = re.sub(r"[^0-9a-f]", "", aid)
            cp = cover_path(aid) or extract_cover_now(aid)
            if not cp:
                self._err("没有封面", 404)
                return
            self._send_file(cp, mimetypes.guess_type(cp)[0] or "image/jpeg", cache=True)
            return
        if p == "/api/meter":
            tid = re.sub(r"[^0-9a-zA-Z]", "", (q.get("tid") or [""])[0])
            fp = TRACKS.get(tid)
            if not fp or not os.path.isfile(fp):
                self._err("没有这个曲目", 404)
                return
            try:
                t0 = float((q.get("pos") or ["0"])[0])
            except Exception:
                t0 = 0.0
            self.api_meter(fp, max(0.0, t0))
            return
        if p.startswith("/api/audio/"):
            tid = re.sub(r"[^0-9a-zA-Z]", "", p.split("/")[-1])
            fp = TRACKS.get(tid)
            if not fp or not os.path.isfile(fp):
                self._err("没有这个曲目", 404)
                return
            if is_dolby(fp):
                plan = dolby_plan(q)
                if plan == "raw":
                    rp = ensure_raw(fp)
                    self._send_file(rp, "audio/mp4")
                    return
                if plan == "51":
                    dp = ensure_dolby(fp, 6)
                    if dp:
                        self._send_file(dp, "audio/mp4")
                        return
                elif self._serve_dolby_mp3(fp):
                    return
            tr = (q.get("tr") or [""])[0]
            trv = None if tr == "" else (1 if tr == "1" else 0)
            if needs_transcode(fp, trv):
                tp = ensure_transcode(fp)
                if tp:
                    self._send_file(tp, "audio/mpeg")
                    return
            ext = os.path.splitext(fp)[1].lower()
            self._send_file(fp, MIME.get(ext, "application/octet-stream"))
            return
        if p.startswith("/api/lyrics/"):
            tid = re.sub(r"[^0-9a-zA-Z]", "", p.split("/")[-1])
            try:
                it = lyrics_for(tid)
            except Exception:
                it = None
            if not it:
                self._json({"ok": False, "lyrics": "", "lrc": False})
            else:
                self._json({"ok": bool(it["lyrics"]), "lyrics": it["lyrics"][:20000], "lrc": it["lrc"]})
            return
        if p.startswith("/api/atmos/"):
            aid = re.sub(r"[^0-9a-f]", "", p.split("/")[-1])
            try:
                self._json({"atmos": album_atmos(aid) if aid else False})
            except Exception:
                self._json({"atmos": False})
            return
        if p.startswith("/api/prep/"):
            tid = re.sub(r"[^0-9a-zA-Z]", "", p.split("/")[-1])
            fp = TRACKS.get(tid)
            if not fp:
                self._err("没有这个曲目", 404)
                return
            if is_dolby(fp):
                plan = dolby_plan(q)
                if plan == "raw":
                    rp = ensure_raw(fp)
                    self._json({"ok": bool(rp), "ready": bool(rp), "dolby": True, "raw": True})
                    return
                if plan == "51":
                    dp = ensure_dolby(fp, 6)
                    self._json({"ok": bool(dp), "ready": bool(dp), "dolby": True})
                    return
                out = dolby_mp3_target(fp)
                if not dolby_mp3_ok(out):
                    dolby_mp3_job(fp, out)        # 只点火，不等待
                self._json({"ok": True, "ready": True, "dolby": True, "stream": True})
                return
            tr = (q.get("tr") or [""])[0]
            trv = None if tr == "" else (1 if tr == "1" else 0)
            if not needs_transcode(fp, trv):
                self._json({"ok": True, "ready": True})
                return
            tp = ensure_transcode(fp)
            self._json({"ok": bool(tp), "ready": bool(tp)})
            return
        if p == "/api/awake":
            on = (q.get("on") or ["1"])[0] != "0"
            sid = (q.get("sid") or ["-"])[0][:40]
            awake_beat(sid, on)
            self._json({"ok": True, "on": awake_active(), "clients": len(AWAKE)})
            return
        if p == "/api/config":
            self._json({"trans": bool(SETTINGS.get("trans")), "ffmpeg": bool(FFMPEG),
                        "dolby": SETTINGS.get("dolby", "auto")})
            return
        if p in ("/apple-touch-icon.png", "/apple-touch-icon-precomposed.png"):
            # 主屏幕图标：存到桌面后用它做 App 图标
            self._send_file(os.path.join(os.path.dirname(PAGE_FILE), "apple-touch-icon.png"), "image/png", cache=True)
            return
        if p == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        self._err("没有这个地址", 404)

    def do_POST(self):
        global FOLDERS
        p = urllib.parse.urlparse(self.path).path
        body = self._read_body()
        if p == "/api/folders":
            if self._need_key():
                return
            self.api_add_folder(body.get("path") or "")
            return
        if p == "/api/pick-folder":
            if self._need_key():
                return
            self.api_pick_folder()
            return
        if p == "/api/config":
            if "trans" in body:
                SETTINGS["trans"] = bool(body["trans"])
                save_json(SETTINGS_FILE, SETTINGS)
                threading.Thread(target=do_scan, daemon=True).start()
            if "dolby" in body:
                SETTINGS["dolby"] = body["dolby"] if body["dolby"] in ("auto", "down", "51", "raw") else "down"
                save_json(SETTINGS_FILE, SETTINGS)
            self._json({"trans": bool(SETTINGS.get("trans")), "ffmpeg": bool(FFMPEG),
                        "dolby": SETTINGS.get("dolby", "auto")})
            return
        if p == "/api/folders/toggle":
            if self._need_key():
                return
            fid = body.get("id") or ""
            on = bool(body.get("on", True))
            hit = None
            with LOCK:
                for f in FOLDERS:
                    if f["id"] == fid:
                        f["enabled"] = on
                        hit = f
            save_folders()
            rebuild_albums()
            if on and hit and not PATHS.get(hit["id"]):
                refresh_paths(hit)
                rebuild_albums()
                threading.Thread(target=do_scan, args=(hit,), daemon=True).start()
            log(("启用" if on else "暂时关闭") + "目录：" + fid)
            self._json({"ok": True, "enabled": on, "albums": len(ALBUMS)})
            return
        if p == "/api/folders/remove":
            if self._need_key():
                return
            fid = body.get("id") or ""
            with LOCK:
                FOLDERS = [f for f in FOLDERS if f["id"] != fid]
            PATHS.pop(fid, None)
            save_folders()
            save_json(PATHS_FILE, PATHS)
            threading.Thread(target=do_scan, daemon=True).start()
            self._json({"ok": True})
            return
        if p == "/api/scan":
            if self._need_key():
                return
            fid = body.get("id")
            with LOCK:
                folder = next((f for f in FOLDERS if f["id"] == fid), None) if fid else None
            threading.Thread(target=do_scan, args=(folder,), daemon=True).start()
            self._json({"ok": True})
            return
        self._err("没有这个地址", 404)

    # ---- 具体接口
    def api_add_folder(self, path):
        path = os.path.expanduser((path or "").strip())
        if not path or not os.path.isdir(path):
            self._err("目录不存在：%s" % path)
            return
        path = os.path.realpath(path)
        with LOCK:
            if any(os.path.realpath(f["path"]) == path for f in FOLDERS):
                self._err("这个目录已经在列表里了")
                return
            FOLDERS.append({"id": short_hash(path, 10), "name": os.path.basename(path.rstrip("/")) or path,
                            "path": path, "enabled": True})
        save_folders()
        log("已添加目录：%s  →  %s" % (path, os.path.basename(path)))
        threading.Thread(target=do_scan, daemon=True).start()
        self._json({"ok": True})

    def api_volumes(self):
        """已挂载的卷（含 Finder 里挂上的 SMB / AFP 网络共享）。"""
        out = []
        for base in ("/Volumes",):
            try:
                names = sorted(os.listdir(base))
            except Exception:
                continue
            skip = {"home", "homes", "net", "video", "Macintosh HD"}
            for name in names:
                if name.startswith(".") or name.startswith("com.apple.") or name in skip:
                    continue
                full = os.path.join(base, name)
                if not os.path.isdir(full):
                    continue
                out.append({"name": name, "path": full})
        home = os.path.expanduser("~")
        out.insert(0, {"name": "个人目录", "path": home})
        out.insert(1, {"name": "音乐", "path": os.path.expanduser("~/Music")})
        self._json({"volumes": [v for v in out if os.path.isdir(v["path"])]})

    def api_pick_folder(self):
        """弹 macOS 原生选目录框（Finder）。对话框出现在运行服务的这台 Mac 上。"""
        if sys.platform != "darwin":
            self._err("原生选择框只在 macOS 上可用")
            return
        script = ('POSIX path of (choose folder with prompt '
                  '"选择音乐文件夹（网络共享请先在 Finder 里挂载）")')
        try:
            r = subprocess.run(["osascript", "-e", script],
                               capture_output=True, text=True, timeout=900)
        except subprocess.TimeoutExpired:
            self._err("选择超时")
            return
        except Exception as e:
            self._err("调用 Finder 失败：%s" % e)
            return
        if r.returncode != 0:
            msg = (r.stderr or "").strip()
            if "-128" in msg or "cancel" in msg.lower():
                self._json({"ok": False, "cancelled": True})
            else:
                self._err("选择失败：%s" % (msg or "未知原因"))
            return
        path = (r.stdout or "").strip().rstrip("/")
        if not path or not os.path.isdir(path):
            self._err("没有拿到有效目录")
            return
        log("Finder 选择目录：%s" % path)
        self._json({"ok": True, "path": os.path.realpath(path)})

    def api_browse(self, path):
        path = os.path.expanduser((path or "~").strip())
        if not os.path.isdir(path):
            path = os.path.expanduser("~")
        path = os.path.realpath(path)
        try:
            entries = sorted(os.listdir(path))
        except PermissionError:
            self._err("没有权限读取这里")
            return
        dirs = []
        for name in entries:
            if name.startswith(".") or name in ("__MACOSX", "node_modules", "System Volume Information"):
                continue
            full = os.path.join(path, name)
            if not os.path.isdir(full):
                continue
            n = 0
            try:
                with os.scandir(full) as it:
                    for e in it:
                        if e.is_file() and os.path.splitext(e.name)[1].lower() in AUDIO_EXT:
                            n += 1
            except Exception:
                n = 0
            dirs.append({"name": name, "path": full, "audio": n})
        parent = os.path.dirname(path.rstrip("/")) or "/"
        self._json({"path": path, "parent": parent, "dirs": dirs})


def warm_thumbs():
    """后台把已有封面的 300px 缩略图批量生成好，避免第一次滚动时逐张等 sips。"""
    try:
        os.makedirs(THUMB_DIR, exist_ok=True)
        for name in os.listdir(COVER_DIR):
            if not name.endswith(".jpg") or name.endswith(".s.jpg"):
                continue
            aid = name[:-4]
            if os.path.exists(os.path.join(THUMB_DIR, aid + ".jpg")):
                continue
            thumb_path(aid)
    except Exception:
        pass


def _raise_fd_limit():
    """macOS 默认每进程只有 256 个 fd，频谱流(每条: socket+管道)跑几小时就会
    Errno 24 'Too many open files'，表现出来就是"有声音没频谱"。启动时抬到 4096。"""
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        want = 4096
        if hard != resource.RLIM_INFINITY:
            want = min(want, hard)
        if soft != resource.RLIM_INFINITY and soft < want:   # 只抬不降
            resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
            log("句柄上限 %s → %s" % (soft, want))
    except Exception as e:
        try: log("抬句柄上限失败：%r" % (e,))
        except Exception: pass


def _single_instance_lock():
    # flock 由内核在进程死亡时自动释放，无陈旧锁问题；持住文件对象防止 GC 闭锁。
    # 锁跟随数据目录（DATA_ROOT），不再固定 ~/.warmplayer —— 不同目录的副本
    # （例如两份对照版本）各有各的锁，可以同时跑，端口各自自动避让；同一数据目录仍只
    # 允许一份实例，防止重复扫描 / 抢 cache、settings、转码临时目录。
    import fcntl
    d = DATA_ROOT
    lock_path = os.path.join(d, "server.lock")
    try:
        os.makedirs(d, exist_ok=True)
        fh = open(lock_path, "w")
    except OSError as e:
        # 只读挂载 / 无权限时不硬失败，退化为不加锁（单机自用场景可接受）
        print("警告：数据目录不可写，跳过单实例锁（%s）" % e)
        return
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("该数据目录已有 WarmPlayer 服务在运行（锁：%s），本实例退出。" % lock_path)
        sys.exit(0)
    fh.write(str(os.getpid()))
    fh.flush()
    globals()["LOCK_FH"] = fh
    globals()["LOCK_PATH"] = lock_path


def main():
    _single_instance_lock()
    _raise_fd_limit()
    ap = argparse.ArgumentParser(description="黑胶唱片机本地服务")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--data-dir", default=None,
                    help="数据根（cache/settings/folders）；默认跟随脚本目录。也可用环境变量 WARMPLAYER_DATA")
    ap.add_argument("--host", default="0.0.0.0",
                    help="监听地址，默认 0.0.0.0（局域网可访问）；只想本机用就填 127.0.0.1")
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()

    os.makedirs(COVER_DIR, exist_ok=True)
    globals()["SETTINGS"].update(load_json(SETTINGS_FILE, {}) or {})
    load_folders()
    globals()["TAGS"] = load_json(TAGS_FILE, {})
    globals()["PATHS"] = load_json(PATHS_FILE, {})
    globals()["TINTS"] = load_json(TINT_FILE, {})
    globals()["SEEN"] = load_json(SEEN_FILE, {})

    # 有目录就先扫一遍（有缓存，很快）
    threading.Thread(target=warm_thumbs, daemon=True).start()

    port = args.port or 0
    if port == 0:
        for p in range(8900, 8940):
            try:
                srv = QuietServer((args.host, p), Handler)
                port = p
                break
            except OSError:
                continue
        else:
            print("端口都被占用了")
            sys.exit(1)
    else:
        srv = QuietServer((args.host, port), Handler)

    url = "http://127.0.0.1:%d/" % port
    print("黑胶唱片机 → %s" % url)
    if args.host in ("0.0.0.0", "::", ""):
        for ip in lan_ips():
            print("   局域网   http://%s:%d/" % (ip, port))
        print("   车机 / 手机用上面这个地址；外网靠路由器把 %d 端口映射到本机即可" % port)
    for f in FOLDERS:
        if f.get("enabled", True) and not PATHS.get(f["id"]):
            refresh_paths(f)
    rebuild_albums()
    log("已载入 %d 个音乐目录 %d 张唱片（缓存），先起服务、后台扫描"
        % (len(FOLDERS), len(ALBUMS)))
    if FOLDERS:
        threading.Thread(target=do_scan, daemon=True).start()
    awake_reap()
    try:
        import numpy as _np
        _npver = _np.__version__
    except Exception:
        _npver = None
    log("自检：python=%s ffmpeg=%s numpy=%s"
        % (sys.version.split()[0], FFMPEG or "缺失", _npver or "缺失"))
    if not FFMPEG:
        log("警告：未找到 ffmpeg，频谱/转码/取色不可用；安装后重启服务")
    if not _npver:
        log("警告：未找到 numpy，频谱不可用；在运行环境 pip install numpy 后重启")

    def _term_handler(signum, frame):
        with AWAKE_LOCK:
            _drop_guard(AWAKE_PROC)
            _drop_guard(BASE_PROC)
        os._exit(0)

    try:
        signal.signal(signal.SIGTERM, _term_handler)
        signal.signal(signal.SIGINT, _term_handler)
    except Exception:
        pass
    threading.Thread(target=awake_loop, daemon=True).start()
    threading.Thread(target=health_loop, args=(port,), daemon=True).start()
    if not args.no_open:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        with AWAKE_LOCK:
            _drop_guard(AWAKE_PROC)
            _drop_guard(BASE_PROC)


if __name__ == "__main__":
    main()
