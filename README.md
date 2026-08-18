# rtp-avr-sender

Streams 5.1 surround audio from a Windows PC to a Linux AVR/HTPC over a dedicated
network link, using RTP instead of a general-purpose remote-desktop or Bluetooth
audio hack. Built to get real discrete 5.1 out of Windows and into a Linux box
driving an AVR over HDMI, with low enough latency to stay in sync with video.

```
Windows PC                              Linux (PipeWire) box
┌─────────────────────────┐             ┌──────────────────────────────┐
│ App audio → VB-Cable     │             │ PipeWire rtp-source module    │
│   → WASAPI loopback      │   RTP/UDP   │   → hdmi-surround ALSA sink   │
│   capture (vban_sender)  │────────────▶│   → AVR (HDMI, 5.1)           │
│   dedicated NIC          │  port 6980  │   dedicated NIC                │
└─────────────────────────┘             └──────────────────────────────┘
```

Despite the filename (`vban_sender.py`, a holdover from an earlier VBAN-based
version), the sender speaks plain RTP (RFC 3550): dynamic payload type 96,
S16BE PCM, 6 channels, 48kHz, fixed SSRC. The receiver is PipeWire's built-in
`libpipewire-module-rtp-source` — no custom receiver process required.

## Requirements

**Sender (Windows):**
- Python 3.8–3.12 (the sender uses the stdlib `audioop` module, which was
  removed in Python 3.13 — this will not run on 3.13+ without changes)
- [VB-Cable](https://vb-audio.com/Cable/) installed, with the app you want to
  stream set to output to "CABLE Input"
- A network interface dedicated to the link to the receiver (see "Networking"
  below) — the code assumes a point-to-point connection, not general LAN
  routing

**Receiver (Linux):**
- PipeWire + WirePlumber (most modern distros ship this by default)
- An HDMI or other multichannel ALSA output capable of a 5.1 surround profile
- systemd (user services) for the reference autostart/watchdog setup

## Sender setup

```bash
pip install -r requirements.txt
```

Edit the config block at the top of `vban_sender.py` for your network:

```python
TARGET_IP         = "10.0.0.2"   # receiver's IP on the dedicated link
TARGET_PORT       = 6980
CAPTURE_DEVICE    = "CABLE Input"
INTERFACE_IP      = "10.0.0.1"   # this machine's IP on the dedicated link
```

Run directly:

```bash
python vban_sender.py
```

Or build a standalone `.exe` (no Python required on the target machine) via
PyInstaller:

```bat
rebuild.bat
```

This produces `vban_sender.exe`. A tray icon shows live status (streaming
level in dBFS, or an error if VB-Cable/the network bind fails).

## Receiver setup

The `receiver-example/` directory has the actual working configuration from
the reference deployment, with machine-specific values (NIC name, ALSA card
index, PCI address, DRM connector) called out in comments — **substitute your
own values**, found via the commands noted inline (`ip link`, `pactl list
sinks short`, `aplay -l`, `ls /sys/class/drm/`).

| File | Installs to |
|---|---|
| `52-rtp-recv.conf.example` | `~/.config/wireplumber/wireplumber.conf.d/52-rtp-recv.conf` |
| `rtp-avr-start.sh.example` | `~/rtp-avr-start.sh` |
| `vban-recv.service.example` | `~/.config/systemd/user/vban-recv.service` |
| `rtp-watchdog.sh.example` | `~/rtp-watchdog.sh` |
| `rtp-watchdog.service.example` | `~/.config/systemd/user/rtp-watchdog.service` |
| `hdmi-force-detect.example` | `/usr/local/bin/hdmi-force-detect` (root:root, 755) |

`hdmi-force-detect` and the HDMI-hotplug logic in `rtp-avr-start.sh` work
around a specific issue where an idle/disabled HDMI output won't report ELD
(and PipeWire won't bind the surround sink) until a DRM re-detect is forced.
**This is likely specific to certain GPU/driver combinations (Intel i915 +
Mutter, in the reference setup)** — if your receiver's HDMI output stays
active/enabled at all times, you probably don't need this part at all.

Once the config is in place:

```bash
systemctl --user daemon-reload
systemctl --user enable --now vban-recv.service rtp-watchdog.service
```

## Networking

Both ends assume a **dedicated point-to-point network link** between sender
and receiver (e.g. a direct Ethernet cable, or its own NIC/VLAN) rather than
general LAN traffic — this keeps the audio stream off shared network paths
and off the interface handling everything else. `INTERFACE_IP` (sender) and
`local.ifname`/`source.ip` (receiver) must match whatever IPs/NIC you set up
for that link.

## Protocol

| Property | Value |
|---|---|
| Transport | RTP over UDP |
| Payload type | 96 (dynamic) |
| Format | S16BE PCM |
| Channels | 6 (5.1) |
| Sample rate | 48000 Hz |
| Frame size | 103 samples/packet (~2.15ms) |
| Channel order (wire) | FL, FR, RL, RR, FC, LFE |
| SSRC | fixed (PipeWire's `rtp-source` locks to the first SSRC seen and rejects changes mid-session) |

## Known limitations

- Sender is Windows-only (WASAPI loopback via `pyaudiowpatch`).
- Receiver assumes PipeWire/WirePlumber; no PulseAudio-only or non-Linux
  receiver is provided.
- `audioop` (used for level metering) was removed in Python 3.13 — pin to
  3.12 or earlier, or replace that one call if you need 3.13+.
- The HDMI-hotplug workaround in `receiver-example/` is a workaround for one
  specific hardware/driver quirk, not a general requirement.
