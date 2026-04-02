# vdo2ts

Receive a [VDO.Ninja](https://vdo.ninja) stream and output it as MPEG-TS — no browser, no OBS.

A single Python script that connects to VDO.Ninja's signaling server, negotiates WebRTC, and pipes the H264 video straight into an MPEG-TS file or UDP stream. Zero transcoding — the video passes through untouched.

## Why

VDO.Ninja is great for getting a camera feed from a phone or browser into a production. But the usual workflow requires OBS or a browser on the receiving end. Sometimes you just need a raw transport stream — to feed into an encoder, a routing system, or a multicast network. That's what this does.

## Requirements

**GStreamer** (1.20+) with WebRTC support:

```bash
# macOS
brew install gstreamer gst-plugins-base gst-plugins-bad gst-plugins-good gst-python

# Debian/Ubuntu
apt install gstreamer1.0-plugins-base gstreamer1.0-plugins-bad \
            gstreamer1.0-plugins-good gstreamer1.0-nice python3-gst-1.0
```

**Python packages:**

```bash
pip install websockets cryptography PyGObject
```

## Usage

```bash
# Record to file
python3 vdo2ts.py myStreamId

# Output to UDP — play with ffplay, VLC, or any MPEG-TS receiver
python3 vdo2ts.py myStreamId udp://127.0.0.1:5000

# Play it
ffplay udp://127.0.0.1:5000

# Multicast
python3 vdo2ts.py myStreamId udp://239.0.0.1:5000

# Custom password (if the publisher set &password= in their URL)
python3 vdo2ts.py myStreamId udp://127.0.0.1:5000 --password mySecret

# One-shot mode (exit on disconnect instead of retrying)
python3 vdo2ts.py myStreamId --no-retry
```

The `stream_id` is the part after `?view=` in the VDO.Ninja URL.
`https://vdo.ninja/?view=abc123` → stream ID is `abc123`.

## What it does

1. Connects to VDO.Ninja's WebSocket signaling server
2. Exchanges encrypted SDP offers/answers (AES-256-CBC)
3. Establishes a WebRTC peer connection via GStreamer's webrtcbin
4. Opens a data channel and requests video from the publisher
5. Receives H264 RTP, depayloads it, and muxes into MPEG-TS
6. If the publisher disconnects, waits and reconnects automatically

No transcoding. The H264 from the browser/phone goes straight into the transport stream.

## Good to know

- **Slow start with ffplay/VLC:** When joining a UDP stream, the player has to wait for the next keyframe before it can decode. This can take a few seconds. If it doesn't start, quit and relaunch — you probably just missed the keyframe window. Using `ffplay -analyzeduration 10000000 -probesize 10000000` helps.

- **Auto-reconnect:** If the publisher drops, the bridge retries automatically with backoff (5s → 15s → 30s → 60s). Disable with `--no-retry`.

- **Encryption:** VDO.Ninja encrypts signaling by default with the password `someEncryptionKey123`. If the publisher uses a custom `&password=`, pass the same value with `--password`.

- **VDO.Ninja protocol:** This tool implements VDO.Ninja's signaling from scratch (WebSocket handshake, AES encryption, stream ID hashing, 2-stage data channel negotiation). This protocol is not a public API and may change. Built by studying [raspberry_ninja](https://github.com/steveseguin/raspberry_ninja).

## License

MIT
