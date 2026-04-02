#!/usr/bin/env python3
"""
vdo2ts — VDO.Ninja stream to MPEG-TS bridge.

Connects to a VDO.Ninja stream via WebSocket signaling + WebRTC,
receives H264 video, and outputs MPEG-TS to a file or UDP.

Usage:
    python3 vdo2ts.py <stream_id> [output]

Arguments:
    stream_id   VDO.Ninja stream ID (the part after ?view= in the URL)
    output      File path (.ts) or udp://host:port (default: /tmp/vdo_out.ts)

Options:
    --password  VDO.Ninja encryption password (default: someEncryptionKey123)
    --server    Custom WSS server (default: wss://wss.vdo.ninja:443)

Examples:
    python3 vdo2ts.py fshSvdAvq                           # Save to /tmp/vdo_out.ts
    python3 vdo2ts.py fshSvdAvq /tmp/stream.ts             # Save to file
    python3 vdo2ts.py fshSvdAvq udp://127.0.0.1:5000       # Output to UDP
    python3 vdo2ts.py fshSvdAvq udp://239.0.0.99:5000      # Output to multicast

Note:
    The signaling protocol used by VDO.Ninja (handshake server, message
    format, 2-stage negotiation via data channel) is not a stable public
    API. It may change without notice. See https://docs.vdo.ninja/.

Requires:
    - GStreamer 1.20+ with gst-plugins-bad (webrtcbin)
    - Python: websockets, cryptography, PyGObject
"""
import argparse
import asyncio
import hashlib
import json
import os
import random
import ssl
import sys
import threading

import gi
gi.require_version('Gst', '1.0')
gi.require_version('GstWebRTC', '1.0')
gi.require_version('GstSdp', '1.0')
from gi.repository import Gst, GstSdp, GstWebRTC, GLib
Gst.init(None)

import websockets
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding as sym_padding


# ---------------------------------------------------------------------------
# VDO.Ninja AES-256-CBC encryption
# ---------------------------------------------------------------------------

def _derive_key(passphrase):
    return hashlib.sha256(passphrase.encode()).digest()

def encrypt_msg(plaintext, passphrase):
    """Encrypt a value for VDO.Ninja signaling. Returns (ciphertext_hex, iv_hex)."""
    key = _derive_key(passphrase)
    iv = os.urandom(16)
    padder = sym_padding.PKCS7(128).padder()
    data = plaintext if isinstance(plaintext, bytes) else json.dumps(plaintext).encode()
    padded = padder.update(data) + padder.finalize()
    ct = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return (ct.update(padded) + ct.finalize()).hex(), iv.hex()

def decrypt_msg(ct_hex, iv_hex, passphrase):
    """Decrypt a VDO.Ninja signaling message."""
    key = _derive_key(passphrase)
    padded = Cipher(algorithms.AES(key), modes.CBC(bytes.fromhex(iv_hex))).decryptor()
    padded = padded.update(bytes.fromhex(ct_hex)) + padded.finalize()
    pad_len = padded[-1]
    if 1 <= pad_len <= 16 and all(b == pad_len for b in padded[-pad_len:]):
        return padded[:-pad_len].decode()
    return padded.decode()


# ---------------------------------------------------------------------------
# Bridge
# ---------------------------------------------------------------------------

class VDOBridge:
    # Reconnection backoff schedule (seconds)
    RETRY_DELAYS = [5, 15, 30, 60, 60, 60]  # then stays at 60s

    def __init__(self, stream_id, output, password, wss_url, auto_retry=True):
        self.stream_id = stream_id
        self.output = output
        self.password = password
        self.wss_url = wss_url
        self.auto_retry = auto_retry

        # VDO.Ninja derives the salt from the WSS hostname
        from urllib.parse import urlparse
        hostname = urlparse(wss_url).hostname or 'vdo.ninja'
        parts = hostname.split('.')
        self.salt = '.'.join(parts[-2:]) if len(parts) >= 2 else hostname

        self.passkey = self.password + self.salt
        self.hashcode = hashlib.sha256(self.passkey.encode()).hexdigest()[:6]
        self.full_stream_id = self.stream_id + self.hashcode
        self.our_id = str(random.randint(10_000_000, 99_999_999_999))

        self._reset_state()

    def _reset_state(self):
        """Reset all mutable state for a fresh connection attempt."""
        self.peer_uuid = None
        self.session_id = None
        self.ws_conn = None
        self.aio_loop = None
        self.dc_channel = None

        self.pipe = None
        self.webrtc = None
        self.glib_loop = GLib.MainLoop()
        self.got_video = False
        self.remote_desc_set = False
        self.ice_buffer = []
        self._shutdown = False

    # -- WebSocket helpers --------------------------------------------------

    def _send_ws(self, msg):
        if not self.ws_conn or not self.aio_loop:
            return
        msg['from'] = self.our_id
        for field in ('description', 'candidates', 'candidate'):
            if field in msg:
                msg[field], msg['vector'] = encrypt_msg(msg[field], self.passkey)
                break
        asyncio.run_coroutine_threadsafe(
            self.ws_conn.send(json.dumps(msg)), self.aio_loop
        )

    # -- GStreamer output ---------------------------------------------------

    def _make_output(self):
        mux = Gst.ElementFactory.make('mpegtsmux', 'mux')
        if self.output.startswith('udp://'):
            parts = self.output[6:].split(':')
            host = parts[0]
            port = int(parts[1].split('?')[0]) if len(parts) > 1 else 5000
            sink = Gst.ElementFactory.make('udpsink', 'sink')
            sink.set_property('host', host)
            sink.set_property('port', port)
            sink.set_property('auto-multicast', True)
            sink.set_property('sync', False)
        else:
            sink = Gst.ElementFactory.make('filesink', 'sink')
            sink.set_property('location', self.output)
        return mux, sink

    # -- WebRTC callbacks ---------------------------------------------------

    def _on_pad_added(self, _, pad):
        if pad.get_direction() != Gst.PadDirection.SRC:
            return
        caps = pad.get_current_caps()
        if not caps:
            return
        enc = caps.get_structure(0).get_string('encoding-name') or ''
        if enc == 'RTX' or enc != 'H264' or self.got_video:
            return
        self.got_video = True

        depay = Gst.ElementFactory.make('rtph264depay', 'depay')
        parse = Gst.ElementFactory.make('h264parse', 'parse')
        parse.set_property('config-interval', -1)
        mux, sink = self._make_output()

        for el in (depay, parse, mux, sink):
            self.pipe.add(el)
            el.sync_state_with_parent()
        depay.link(parse)
        parse.link(mux)
        mux.link(sink)
        pad.link(depay.get_static_pad('sink'))
        print(f'[OK] H264 → {self.output}', flush=True)

    def _on_ice_candidate(self, _, mline, candidate):
        if ' TCP ' in candidate:
            return
        msg = {
            'candidates': [{'candidate': candidate, 'sdpMLineIndex': mline}],
            'type': 'remote',
        }
        if self.peer_uuid:
            msg['UUID'] = self.peer_uuid
        if self.session_id:
            msg['session'] = self.session_id
        self._send_ws(msg)

    def _on_ice_gathering_state(self, webrtcbin, pspec):
        state = webrtcbin.get_property('ice-gathering-state')
        if state == GstWebRTC.WebRTCICEGatheringState.COMPLETE:
            msg = {
                'candidates': [{'candidate': '', 'sdpMLineIndex': 0}],
                'type': 'remote',
            }
            if self.peer_uuid:
                msg['UUID'] = self.peer_uuid
            if self.session_id:
                msg['session'] = self.session_id
            self._send_ws(msg)
            print('[ICE] Gathering complete', flush=True)

    def _on_connection_state(self, webrtcbin, pspec):
        state = webrtcbin.get_property('connection-state')
        names = {0: 'new', 1: 'connecting', 2: 'connected', 3: 'disconnected', 4: 'failed', 5: 'closed'}
        print(f'[WebRTC] {names.get(state, state)}', flush=True)
        # Signal disconnection to the WS loop so it can retry
        if state >= 3 and self.ws_conn and self.aio_loop:  # disconnected/failed/closed
            self._shutdown = True
            asyncio.run_coroutine_threadsafe(self.ws_conn.close(), self.aio_loop)

    # -- Data channel (VDO.Ninja 2-stage negotiation) -----------------------

    def _on_data_channel(self, _, channel):
        channel.connect('on-message-string', self._on_dc_message)
        channel.connect('on-open', self._on_dc_open)

    def _on_dc_open(self, channel):
        self.dc_channel = channel
        channel.emit('send-string', json.dumps({"audio": False, "video": True}))
        print('[DC] Requesting video...', flush=True)

    def _on_dc_message(self, channel, msg_str):
        try:
            msg = json.loads(msg_str)
        except json.JSONDecodeError:
            return

        if 'vector' in msg:
            for field in ('description', 'candidates', 'candidate'):
                if field in msg:
                    try:
                        msg[field] = json.loads(decrypt_msg(msg[field], msg['vector'], self.passkey))
                    except Exception:
                        return
                    break

        if 'description' in msg:
            desc = msg['description']
            if isinstance(desc, str):
                try:
                    desc = json.loads(desc)
                except Exception:
                    return
            if desc.get('type') == 'offer':
                sdp = desc.get('sdp', '')
                n = sdp.count('m=')
                print(f'[DC] Re-offer: {n} media', flush=True)
                self.remote_desc_set = False
                GLib.idle_add(lambda s=sdp: self._process_offer(s, reoffer=True) or False)

        elif 'candidates' in msg or 'candidate' in msg:
            key = 'candidates' if 'candidates' in msg else 'candidate'
            cands = msg[key]
            if not isinstance(cands, list):
                cands = [cands]
            GLib.idle_add(lambda c=cands: self._add_ice(c) or False)

    # -- SDP handling -------------------------------------------------------

    def _process_offer(self, sdp_text, reoffer=False):
        res, sdpmsg = GstSdp.SDPMessage.new_from_text(sdp_text)
        if res != GstSdp.SDPResult.OK:
            print(f'[SDP] Parse failed', flush=True)
            return
        offer = GstWebRTC.WebRTCSessionDescription.new(GstWebRTC.WebRTCSDPType.OFFER, sdpmsg)

        def on_remote_set(promise, _, __):
            promise.wait()
            self.remote_desc_set = True
            self._flush_ice()
            self.webrtc.emit('create-answer', None,
                             Gst.Promise.new_with_change_func(on_answer, None, None))

        def on_answer(promise, _, __):
            try:
                promise.wait()
                reply = promise.get_reply()
                answer = reply.get_value('answer') if reply else None
                if not answer:
                    return
                text = answer.sdp.as_text()

                p = Gst.Promise.new()
                self.webrtc.emit('set-local-description', answer, p)
                p.interrupt()

                if reoffer and self.dc_channel:
                    ct, iv = encrypt_msg({'type': 'answer', 'sdp': text}, self.passkey)
                    dc_msg = {'description': ct, 'vector': iv}
                    if self.peer_uuid:
                        dc_msg['UUID'] = self.peer_uuid
                    if self.session_id:
                        dc_msg['session'] = self.session_id
                    self.dc_channel.emit('send-string', json.dumps(dc_msg))
                else:
                    msg = {'description': {'type': 'answer', 'sdp': text}}
                    if self.peer_uuid:
                        msg['UUID'] = self.peer_uuid
                    if self.session_id:
                        msg['session'] = self.session_id
                    self._send_ws(msg)

                tag = 'DC' if reoffer else 'WS'
                print(f'[{tag}] Answer sent ({len(text)}B)', flush=True)
            except Exception as e:
                print(f'[SDP] Error: {e}', flush=True)

        self.webrtc.emit('set-remote-description', offer,
                         Gst.Promise.new_with_change_func(on_remote_set, None, None))

    def _add_ice(self, candidates):
        if not self.remote_desc_set:
            self.ice_buffer.extend(candidates)
            return
        for c in candidates:
            if isinstance(c, dict) and 'candidate' in c:
                self.webrtc.emit('add-ice-candidate',
                                 c.get('sdpMLineIndex', 0), c['candidate'])

    def _flush_ice(self):
        if self.ice_buffer:
            self._add_ice(self.ice_buffer[:])
            self.ice_buffer.clear()

    # -- Cleanup ------------------------------------------------------------

    def _teardown(self):
        """Tear down GStreamer pipeline and GLib loop."""
        if self.pipe:
            self.pipe.set_state(Gst.State.NULL)
            self.pipe = None
        if self.glib_loop.is_running():
            self.glib_loop.quit()
        self.webrtc = None
        self.dc_channel = None

    def close(self):
        self._shutdown = True
        if self.ws_conn and self.aio_loop:
            asyncio.run_coroutine_threadsafe(self.ws_conn.close(), self.aio_loop)
        self._teardown()

    # -- Main loop ----------------------------------------------------------

    async def _connect_once(self):
        """Single connection attempt. Returns when disconnected."""
        self._reset_state()
        self.aio_loop = asyncio.get_event_loop()

        # GStreamer pipeline
        self.pipe = Gst.Pipeline.new('vdo2ts')
        self.webrtc = Gst.ElementFactory.make('webrtcbin', 'webrtc')
        self.webrtc.set_property('stun-server', 'stun://stun.l.google.com:19302')
        self.webrtc.set_property('bundle-policy', GstWebRTC.WebRTCBundlePolicy.MAX_BUNDLE)
        self.pipe.add(self.webrtc)

        self.webrtc.connect('pad-added', self._on_pad_added)
        self.webrtc.connect('on-ice-candidate', self._on_ice_candidate)
        self.webrtc.connect('notify::ice-gathering-state', self._on_ice_gathering_state)
        self.webrtc.connect('on-data-channel', self._on_data_channel)
        self.webrtc.connect('notify::connection-state', self._on_connection_state)
        self.pipe.set_state(Gst.State.PLAYING)

        threading.Thread(target=self.glib_loop.run, daemon=True).start()

        # WebSocket signaling
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE

        print(f'[WS] Connecting...', flush=True)
        self.ws_conn = await websockets.connect(self.wss_url, ssl=ssl_ctx, ping_interval=None)

        await self.ws_conn.send(json.dumps({
            'request': 'play',
            'streamID': self.full_stream_id,
            'from': self.our_id,
        }))
        print(f'[WS] Viewing: {self.stream_id}', flush=True)

        async for raw in self.ws_conn:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            # Peer identification
            sender = msg.get('from')
            if sender:
                if sender == self.our_id:
                    continue
                if not self.peer_uuid:
                    self.peer_uuid = sender
            elif 'UUID' in msg and not self.peer_uuid:
                self.peer_uuid = msg['UUID']

            if 'session' in msg and not self.session_id:
                self.session_id = msg['session']

            # SDP offer
            if 'description' in msg:
                desc = msg['description']
                if 'vector' in msg:
                    try:
                        desc = json.loads(decrypt_msg(desc, msg['vector'], self.passkey))
                    except Exception:
                        continue
                if isinstance(desc, str):
                    try:
                        desc = json.loads(desc)
                    except Exception:
                        continue
                if desc.get('type') == 'offer':
                    print(f'[WS] Offer received', flush=True)
                    GLib.idle_add(lambda s=desc['sdp']: self._process_offer(s) or False)

            # ICE candidates (plural or singular)
            elif 'candidates' in msg or 'candidate' in msg:
                key = 'candidates' if 'candidates' in msg else 'candidate'
                cands = msg[key]
                if 'vector' in msg:
                    try:
                        cands = json.loads(decrypt_msg(cands, msg['vector'], self.passkey))
                    except Exception:
                        continue
                if isinstance(cands, str):
                    try:
                        cands = json.loads(cands)
                    except Exception:
                        continue
                if not isinstance(cands, list):
                    cands = [cands]
                GLib.idle_add(lambda c=cands: self._add_ice(c) or False)

        # WS closed — clean up pipeline
        self._teardown()

    async def run(self):
        """Main loop with auto-reconnection."""
        attempt = 0
        while True:
            try:
                await self._connect_once()
            except (websockets.exceptions.ConnectionClosed,
                    websockets.exceptions.InvalidStatusCode,
                    ConnectionRefusedError, OSError) as e:
                print(f'[WS] Disconnected: {e}', flush=True)
                self._teardown()
            except Exception as e:
                print(f'[Error] {e}', flush=True)
                self._teardown()

            if not self.auto_retry or self._shutdown:
                break

            delay = self.RETRY_DELAYS[min(attempt, len(self.RETRY_DELAYS) - 1)]
            attempt += 1
            print(f'[Retry] Reconnecting in {delay}s (attempt {attempt})...', flush=True)
            await asyncio.sleep(delay)

            # Reset backoff on successful connection
            if self.got_video:
                attempt = 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='VDO.Ninja → MPEG-TS bridge',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='Examples:\n'
               '  %(prog)s abc123\n'
               '  %(prog)s abc123 udp://127.0.0.1:5000\n'
               '  %(prog)s abc123 /tmp/recording.ts --password mySecret\n',
    )
    parser.add_argument('stream_id', help='VDO.Ninja stream ID')
    parser.add_argument('output', nargs='?', default='/tmp/vdo_out.ts',
                        help='Output: file path or udp://host:port')
    parser.add_argument('--password', default='someEncryptionKey123',
                        help='Encryption password (default: VDO.Ninja default)')
    parser.add_argument('--server', default='wss://wss.vdo.ninja:443',
                        help='WSS signaling server')
    parser.add_argument('--no-retry', action='store_true',
                        help='Exit on disconnect instead of auto-reconnecting')
    args = parser.parse_args()

    bridge = VDOBridge(args.stream_id, args.output, args.password, args.server,
                       auto_retry=not args.no_retry)
    print(f'vdo2ts — {bridge.stream_id} → {bridge.output}')
    try:
        asyncio.run(bridge.run())
    except KeyboardInterrupt:
        print('\nStopped.')
    finally:
        bridge.close()


if __name__ == '__main__':
    main()
