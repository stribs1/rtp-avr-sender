"""
RTP Audio Sender - Captures from VB-Cable Input via WASAPI loopback
and streams 5.1 PCM (S16BE, 48kHz) to the T490 PipeWire RTP receiver.
System tray icon shows status. Right-click to quit.
"""

import gc
import pyaudiowpatch as pyaudio
import socket
import struct
import sys
import time
import audioop
import math
import threading
import queue
import random

import pystray
from PIL import Image, ImageDraw

# ── CONFIG ────────────────────────────────────────────────────────────────────
TARGET_IP         = "10.0.0.2"
TARGET_PORT       = 6980
CAPTURE_DEVICE    = "CABLE Input"
CHANNELS          = 6
SAMPLE_RATE       = 48000
SAMPLES_PER_FRAME = 103          # ~2.15ms per packet at 48kHz (matches PipeWire rtp-source internal ptime)
INTERFACE_IP      = "10.0.0.1"  # Force RTP out this NIC
RTP_PAYLOAD_TYPE  = 96           # dynamic PT, S16BE 6ch 48kHz
SSRC              = 0x4156524F   # fixed — rtp-source locks onto first SSRC and rejects changes
# ─────────────────────────────────────────────────────────────────────────────

# Precomputed channel reorder: Windows (FL FR FC LFE RL RR) → PipeWire (FL FR RL RR FC LFE)
_REORDER_IDX = []
for _i in range(SAMPLES_PER_FRAME):
    _b = _i * 6
    _REORDER_IDX.extend([_b, _b+1, _b+4, _b+5, _b+2, _b+3])
_REORDER_IDX = tuple(_REORDER_IDX)

stop_event = threading.Event()


def make_icon(color):
    img = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([4, 4, 60, 60], fill=color)
    return img


def build_rtp_header(seq: int, timestamp: int, ssrc: int) -> bytes:
    # Byte 1: V=2, P=0, X=0, CC=0 → 0x80
    # Byte 2: M=0, PT=96         → 0x60
    return struct.pack('!BBHII', 0x80, RTP_PAYLOAD_TYPE, seq & 0xFFFF, timestamp & 0xFFFFFFFF, ssrc)


def find_loopback_device(p: pyaudio.PyAudio, name_hint: str):
    for i in range(p.get_device_count()):
        dev = p.get_device_info_by_index(i)
        if (name_hint.lower() in dev['name'].lower()
                and dev.get('isLoopbackDevice', False)):
            return i, dev
    return None, None


def stream_thread(tray_icon):
    import ctypes
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind((INTERFACE_IP, TARGET_PORT))
    except Exception as e:
        tray_icon.title = f"RTP Sender — BIND FAILED: {e}"
        tray_icon.icon  = make_icon("red")
        sock.close()
        return

    # Decouple network send from the WASAPI callback — callback enqueues,
    # send_worker sends, so the audio thread is never blocked on a syscall.
    send_queue    = queue.Queue(maxsize=200)
    pending_title = [None]

    def send_worker():
        while True:
            item = send_queue.get()
            if item is None:
                break
            try:
                sock.sendto(item[0], item[1])
            except Exception:
                pass
            title = pending_title[0]
            if title is not None:
                pending_title[0] = None
                tray_icon.title = title

    send_thread = threading.Thread(target=send_worker, daemon=True)
    send_thread.start()

    p = pyaudio.PyAudio()

    idx, dev = find_loopback_device(p, CAPTURE_DEVICE)
    if idx is None:
        tray_icon.title = "RTP Sender — ERROR: CABLE Input not found"
        tray_icon.icon  = make_icon("red")
        p.terminate()
        sock.close()
        return

    capture_ch = int(dev['maxInputChannels'])

    # Precompute struct formats and zero-pad — these are constant for the session
    _pack_in   = struct.Struct(f'<{SAMPLES_PER_FRAME * capture_ch}h')
    _pack_in6  = struct.Struct(f'<{SAMPLES_PER_FRAME * 6}h')
    _pack_out  = struct.Struct(f'>{SAMPLES_PER_FRAME * CHANNELS}h')
    _zero_pad  = [0] * (CHANNELS - capture_ch) if capture_ch < CHANNELS else []

    # RTP session state — randomised at stream start per RFC 3550
    ssrc   = SSRC
    seq    = [random.randint(0, 0xFFFF)]
    rtp_ts = [random.randint(0, 0xFFFFFFFF)]

    frame_count      = [0]
    max_rms          = [0]
    report_every     = 200
    mmcss_registered = [False]

    def audio_callback(in_data, frame_count_cb, time_info, status):
        # Runs in WASAPI's high-priority audio thread — no sleep(), hardware-timed
        if not mmcss_registered[0]:
            mmcss_registered[0] = True
            try:
                task_idx = ctypes.c_ulong(0)
                handle = ctypes.windll.avrt.AvSetMmThreadCharacteristicsW(
                    "Pro Audio", ctypes.byref(task_idx))
                if not handle:
                    err = ctypes.windll.kernel32.GetLastError()
                    pending_title[0] = f"RTP Sender — WARNING: MMCSS failed (err {err})"
            except Exception:
                pass

        if capture_ch < CHANNELS:
            src = list(_pack_in.unpack(in_data))
            dst = []
            for i in range(SAMPLES_PER_FRAME):
                frame_s = src[i * capture_ch:(i + 1) * capture_ch]
                frame_s += _zero_pad
                dst.extend(frame_s)
            pcm_data = _pack_out.pack(*dst)
        else:
            # Reorder 6ch using precomputed index table, then pack big-endian for RTP L16
            samples  = _pack_in6.unpack(in_data)
            pcm_data = _pack_out.pack(*[samples[i] for i in _REORDER_IDX])

        rms = audioop.rms(in_data, 2)
        if rms > max_rms[0]:
            max_rms[0] = rms
        frame_count[0] += 1

        if frame_count[0] % report_every == 0:
            if max_rms[0] > 0:
                db = 20 * math.log10(max_rms[0] / 32768)
                pending_title[0] = f"RTP Sender — {db:+.1f} dBFS → {TARGET_IP}"
            else:
                pending_title[0] = f"RTP Sender — SILENT → {TARGET_IP}"
            max_rms[0] = 0

        header = build_rtp_header(seq[0], rtp_ts[0], ssrc)
        seq[0]    = (seq[0]    + 1)                  & 0xFFFF
        rtp_ts[0] = (rtp_ts[0] + SAMPLES_PER_FRAME) & 0xFFFFFFFF

        try:
            send_queue.put_nowait((header + pcm_data, (TARGET_IP, TARGET_PORT)))
        except queue.Full:
            pass  # drop packet rather than block the audio thread

        return (None, pyaudio.paContinue)

    stream = p.open(
        format=pyaudio.paInt16,
        channels=capture_ch,
        rate=SAMPLE_RATE,
        input=True,
        input_device_index=idx,
        frames_per_buffer=SAMPLES_PER_FRAME,
        stream_callback=audio_callback,
    )

    tray_icon.icon  = make_icon("green")
    tray_icon.title = f"RTP Sender — Streaming to {TARGET_IP}:{TARGET_PORT}"

    gc.disable()  # prevent GC pauses mid-callback; we collect manually between sleeps
    stream.start_stream()
    try:
        while stream.is_active() and not stop_event.is_set():
            time.sleep(0.1)
            gc.collect(0)  # reclaim any cycles before they accumulate; gen-0 only to avoid GIL stalls on the audio callback thread
    finally:
        stream.stop_stream()
        stream.close()
        gc.enable()
        p.terminate()
        send_queue.put(None)
        send_thread.join(timeout=2)
        sock.close()


def quit_action(tray_icon, item):
    stop_event.set()
    tray_icon.stop()


def main():
    mutex_name = "RTPSenderMutex"
    import ctypes
    mutex = ctypes.windll.kernel32.CreateMutexW(None, False, mutex_name)
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        ctypes.windll.user32.MessageBoxW(
            0,
            "RTP Sender is already running.\nCheck the system tray.",
            "RTP Sender",
            0x40
        )
        sys.exit(0)

    ctypes.windll.kernel32.SetPriorityClass(
        ctypes.windll.kernel32.GetCurrentProcess(),
        0x00000080  # HIGH_PRIORITY_CLASS
    )
    ctypes.windll.winmm.timeBeginPeriod(1)

    icon = pystray.Icon(
        name="rtp_sender",
        icon=make_icon("orange"),
        title=f"RTP Sender — Starting... (IF:{INTERFACE_IP})",
        menu=pystray.Menu(
            pystray.MenuItem("RTP Sender", None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(f"Target: {TARGET_IP}:{TARGET_PORT}", None, enabled=False),
            pystray.MenuItem(
                f"{CHANNELS}ch  {SAMPLE_RATE}Hz  PT:{RTP_PAYLOAD_TYPE}", None, enabled=False
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", quit_action),
        )
    )

    t = threading.Thread(target=stream_thread, args=(icon,), daemon=True)
    t.start()

    icon.run()
    ctypes.windll.winmm.timeEndPeriod(1)


if __name__ == '__main__':
    main()
