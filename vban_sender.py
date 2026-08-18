"""
VBAN Sender - Captures audio from VB-Cable Input via WASAPI loopback
and streams it to a VBAN receiver (Lenovo T490 running PipeWire VBAN recv).
System tray icon shows status. Right-click tray icon to quit.
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
import os

import pystray
from PIL import Image, ImageDraw

# ── CONFIG ────────────────────────────────────────────────────────────────────
TARGET_IP         = "10.0.0.2"
TARGET_PORT       = 6980
STREAM_NAME       = "Stream1"
CAPTURE_DEVICE    = "CABLE Input"
CHANNELS          = 6
SAMPLE_RATE       = 48000
SAMPLES_PER_FRAME = 119
INTERFACE_IP      = "10.0.0.1"   # Force VBAN out this NIC (office switch)
# ─────────────────────────────────────────────────────────────────────────────

VBAN_MAGIC        = b'VBAN'
VBAN_SR_48000     = 3
VBAN_PROTOCOL_PCM = 0x00
VBAN_FORMAT_INT16 = 0x01

# Precomputed channel reorder: Windows (FL FR FC LFE RL RR) → PipeWire (FL FR RL RR FC LFE)
# Built once at import time so the audio callback does no per-frame index arithmetic.
_REORDER_IDX = []
for _i in range(SAMPLES_PER_FRAME):
    _b = _i * 6
    _REORDER_IDX.extend([_b, _b+1, _b+4, _b+5, _b+2, _b+3])
_REORDER_IDX = tuple(_REORDER_IDX)

stop_event = threading.Event()


def make_icon(color):
    """Create a simple coloured circle icon for the tray."""
    img = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([4, 4, 60, 60], fill=color)
    return img


def build_vban_header(frame_counter: int) -> bytes:
    sr_byte = VBAN_SR_48000 | (VBAN_PROTOCOL_PCM << 5)
    nbs     = SAMPLES_PER_FRAME - 1
    nbc     = CHANNELS - 1
    fmt     = VBAN_FORMAT_INT16
    name    = STREAM_NAME.encode()[:16].ljust(16, b'\x00')
    return struct.pack('<4sBBBB16sI',
                      VBAN_MAGIC, sr_byte, nbs, nbc, fmt, name, frame_counter)


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
        sock.bind((INTERFACE_IP, 0))
    except Exception as e:
        tray_icon.title = f"VBAN Sender — BIND FAILED: {e}"
        tray_icon.icon  = make_icon("red")
        sock.close()
        return

    # Decouple network send from the WASAPI callback — callback enqueues,
    # this thread sends, so the audio thread is never blocked on a syscall.
    # Tray title updates also happen here to keep Win32 calls off the audio thread.
    send_queue = queue.Queue(maxsize=200)
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
        tray_icon.title = "VBAN Sender — ERROR: CABLE Input not found"
        tray_icon.icon  = make_icon("red")
        p.terminate()
        sock.close()
        return

    capture_ch  = int(dev['maxInputChannels'])
    frame_counter = [0]
    frame_count   = [0]
    max_rms       = [0]
    report_every  = 200
    mmcss_registered = [False]

    def audio_callback(in_data, frame_count_cb, time_info, status):
        # Runs in WASAPI's high-priority audio thread — no sleep(), hardware-timed
        if not mmcss_registered[0]:
            mmcss_registered[0] = True
            try:
                task_idx = ctypes.c_ulong(0)
                handle = ctypes.windll.avrt.AvSetMmThreadCharacteristicsW("Pro Audio", ctypes.byref(task_idx))
                if not handle:
                    err = ctypes.windll.kernel32.GetLastError()
                    pending_title[0] = f"VBAN Sender — WARNING: MMCSS failed (err {err})"
            except Exception:
                pass
        if capture_ch < CHANNELS:
            src = list(struct.unpack(f'<{SAMPLES_PER_FRAME * capture_ch}h', in_data))
            dst = []
            for i in range(SAMPLES_PER_FRAME):
                frame_s = src[i*capture_ch:(i+1)*capture_ch]
                frame_s += [0] * (CHANNELS - capture_ch)
                dst.extend(frame_s)
            pcm_data = struct.pack(f'<{SAMPLES_PER_FRAME * CHANNELS}h', *dst)
        else:
            # Reorder 6ch using precomputed index table (no per-frame arithmetic)
            samples = struct.unpack(f'<{SAMPLES_PER_FRAME * 6}h', in_data)
            pcm_data = struct.pack(f'<{SAMPLES_PER_FRAME * 6}h',
                                   *[samples[i] for i in _REORDER_IDX])

        rms = audioop.rms(in_data, 2)
        if rms > max_rms[0]:
            max_rms[0] = rms
        frame_count[0] += 1

        if frame_count[0] % report_every == 0:
            if max_rms[0] > 0:
                db = 20 * math.log10(max_rms[0] / 32768)
                pending_title[0] = f"VBAN Sender — {db:+.1f} dBFS → {TARGET_IP}"
            else:
                pending_title[0] = f"VBAN Sender — SILENT → {TARGET_IP}"
            max_rms[0] = 0

        header = build_vban_header(frame_counter[0])
        try:
            send_queue.put_nowait((header + pcm_data, (TARGET_IP, TARGET_PORT)))
        except queue.Full:
            pass  # drop packet rather than block the audio thread
        frame_counter[0] = (frame_counter[0] + 1) & 0xFFFFFFFF

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
    tray_icon.title = f"VBAN Sender — Streaming to {TARGET_IP}:{TARGET_PORT}"

    gc.disable()  # prevent GC pauses from stalling the audio callback thread
    stream.start_stream()
    try:
        while stream.is_active() and not stop_event.is_set():
            time.sleep(0.1)
    finally:
        stream.stop_stream()
        stream.close()
        gc.enable()
        p.terminate()
        send_queue.put(None)  # signal send thread to exit
        send_thread.join(timeout=2)
        sock.close()


def quit_action(tray_icon, item):
    stop_event.set()
    tray_icon.stop()


def main():
    # Prevent multiple instances
    mutex_name = "VBANSenderMutex"
    import ctypes
    mutex = ctypes.windll.kernel32.CreateMutexW(None, False, mutex_name)
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        # Already running — show message and exit
        ctypes.windll.user32.MessageBoxW(
            0,
            "VBAN Sender is already running.\nCheck the system tray.",
            "VBAN Sender",
            0x40  # MB_ICONINFORMATION
        )
        sys.exit(0)
        
    # Set high process priority to reduce packet timing jitter
    ctypes.windll.kernel32.SetPriorityClass(
        ctypes.windll.kernel32.GetCurrentProcess(),
        0x00000080  # HIGH_PRIORITY_CLASS
    )

    # Set 1ms timer resolution for precise sleep() intervals (default is 15.6ms)
    ctypes.windll.winmm.timeBeginPeriod(1)

    icon = pystray.Icon(
        name="vban_sender",
        icon=make_icon("orange"),
        title=f"VBAN Sender — Starting... (IF:{INTERFACE_IP})",
        menu=pystray.Menu(
            pystray.MenuItem("VBAN Sender", None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                f"Target: {TARGET_IP}:{TARGET_PORT}", None, enabled=False
            ),
            pystray.MenuItem(
                f"Stream: {STREAM_NAME}  {CHANNELS}ch  {SAMPLE_RATE}Hz", None, enabled=False
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", quit_action),
        )
    )

    # Run streaming in background thread
    t = threading.Thread(target=stream_thread, args=(icon,), daemon=True)
    t.start()

    # Run tray icon (blocks until icon.stop() is called)
    icon.run()

    ctypes.windll.winmm.timeEndPeriod(1)


if __name__ == '__main__':
    main()
