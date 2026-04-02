# vdo2ts

Receive a [VDO.Ninja](https://vdo.ninja) stream and output it as MPEG-TS (file or UDP).

Handles the full VDO.Ninja signaling protocol: WebSocket handshake, AES-256-CBC encryption, 2-stage data channel negotiation, and H264 passthrough via GStreamer's webrtcbin.

## Install

System dependencies (GStreamer with WebRTC support):

```bash
# macOS
brew install gstreamer gst-plugins-base gst-plugins-bad gst-plugins-good gst-python

# Debian/Ubuntu
apt install gstreamer1.0-plugins-base gstreamer1.0-plugins-bad gstreamer1.0-plugins-good \
            gstreamer1.0-nice python3-gst-1.0
```

Python dependencies:

```bash
pip install -r requirements.txt
```

## Usage

```bash
# Save to file
python3 vdo2ts.py <stream_id>

# Output to UDP (use with ffplay, VLC, or any MPEG-TS receiver)
python3 vdo2ts.py <stream_id> udp://127.0.0.1:5000

# Play it
ffplay udp://127.0.0.1:5000

# Custom password
python3 vdo2ts.py <stream_id> udp://127.0.0.1:5000 --password mySecret
```

The `stream_id` is the part after `?view=` in the VDO.Ninja URL.
For example, if the URL is `https://vdo.ninja/?view=abc123`, the stream ID is `abc123`.

## How it works

1. Connects to VDO.Ninja's WSS signaling server
2. Sends an encrypted `play` request for the stream
3. Receives a datachannel-only SDP offer, negotiates WebRTC connection
4. Once the data channel opens, requests video
5. Publisher renegotiates with a video SDP via the data channel
6. H264 RTP is depayloaded and muxed into MPEG-TS

The video is **not transcoded** — it's a zero-copy passthrough from WebRTC RTP to MPEG-TS.

## Notes

- When joining a UDP stream mid-session, expect a few seconds of `PPS 0 referenced` errors in ffplay/ffmpeg — this is normal, it's waiting for the next keyframe.
- For best results with `ffplay`, use: `ffplay -analyzeduration 10000000 -probesize 10000000 udp://127.0.0.1:5000`
- VDO.Ninja's default encryption password is `someEncryptionKey123`. If the publisher uses a custom `&password=` parameter, pass the same value with `--password`.
