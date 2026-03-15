"""Real-time streaming speech-to-text via ElevenLabs WebSocket API."""

import base64
import json
import logging
import queue
import ssl
import threading
from urllib.parse import urlencode

import certifi
import websocket

logger = logging.getLogger("scriber")

WS_BASE_URL = "wss://api.elevenlabs.io/v1/speech-to-text/realtime"


class StreamingTranscriber:
    """Manages a real-time STT WebSocket session with ElevenLabs.

    Usage:
        streamer = StreamingTranscriber(api_key, on_partial=..., on_committed=...)
        streamer.start()
        # Feed audio chunks from recorder callback:
        streamer.send_chunk(pcm_bytes)
        # When done:
        streamer.stop()
    """

    def __init__(
        self,
        api_key: str,
        language: str = "",
        on_partial=None,
        on_committed=None,
        on_error=None,
    ):
        self._api_key = api_key
        self._language = language
        self._on_partial = on_partial      # callable(str) — interim text
        self._on_committed = on_committed  # callable(str) — final segment
        self._on_error = on_error          # callable(str) — error message
        self._ws = None
        self._chunk_queue = queue.Queue()
        self._running = False
        self._send_thread = None
        self._recv_thread = None

    def start(self):
        """Connect to the WebSocket and start send/recv threads."""
        params = {
            "model_id": "scribe_v2_realtime",
            "audio_format": "pcm_16000",
            "commit_strategy": "vad",
            "vad_silence_threshold_secs": "1.0",
        }
        if self._language:
            params["language_code"] = self._language

        url = f"{WS_BASE_URL}?{urlencode(params)}"
        logger.info("Streaming: connecting to %s", WS_BASE_URL)

        self._ws = websocket.WebSocket(
            sslopt={
                "cert_reqs": ssl.CERT_REQUIRED,
                "ca_certs": certifi.where(),
            }
        )
        self._ws.connect(
            url,
            header={"xi-api-key": self._api_key},
            timeout=10,
        )
        self._running = True

        self._recv_thread = threading.Thread(target=self._recv_loop, daemon=True)
        self._recv_thread.start()

        self._send_thread = threading.Thread(target=self._send_loop, daemon=True)
        self._send_thread.start()

        logger.info("Streaming: connected and running")

    def send_chunk(self, pcm_bytes: bytes):
        """Queue a raw PCM audio chunk for sending. Called from audio callback."""
        if self._running:
            self._chunk_queue.put(pcm_bytes)

    def stop(self):
        """Close the WebSocket and stop threads gracefully.

        Sends a final commit to flush any buffered audio, waits briefly
        for the last committed_transcript, then shuts down.
        """
        logger.info("Streaming: stopping (flushing remaining audio)")

        # Stop the send loop from sending more audio
        self._running = False
        self._chunk_queue.put(None)  # Unblock send thread

        # Wait for send thread to finish so all queued chunks are sent
        if self._send_thread and self._send_thread.is_alive():
            self._send_thread.join(timeout=2.0)

        # Send a final commit message to flush buffered audio on the server
        try:
            if self._ws:
                self._ws.send(json.dumps({
                    "message_type": "input_audio_chunk",
                    "audio_base_64": "",
                    "commit": True,
                    "sample_rate": 16000,
                }))
                logger.info("Streaming: commit flush sent, waiting for final transcript")
        except Exception as e:
            logger.debug("Streaming: commit flush send failed: %s", e)

        # Give the recv thread time to receive the final committed_transcript
        if self._recv_thread and self._recv_thread.is_alive():
            self._recv_thread.join(timeout=3.0)

        # Close WebSocket
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass

        self._ws = None
        logger.info("Streaming: stopped")

    def _send_loop(self):
        """Read chunks from the queue and send over WebSocket."""
        chunks_sent = 0
        while self._running:
            try:
                chunk = self._chunk_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            if chunk is None:
                break  # Sentinel — time to exit

            if not self._running:
                break

            try:
                msg = json.dumps({
                    "message_type": "input_audio_chunk",
                    "audio_base_64": base64.b64encode(chunk).decode("ascii"),
                    "sample_rate": 16000,
                })
                self._ws.send(msg)
                chunks_sent += 1
                if chunks_sent == 1:
                    logger.info("Streaming: first audio chunk sent (%d bytes)", len(chunk))
                elif chunks_sent % 50 == 0:
                    logger.debug("Streaming: %d chunks sent", chunks_sent)
            except Exception as e:
                logger.error("Streaming send error: %s", e)
                self._running = False
                break
        logger.info("Streaming: send loop exited (%d chunks sent)", chunks_sent)

    def _recv_loop(self):
        """Receive messages from the WebSocket and dispatch callbacks."""
        while self._running:
            try:
                data = self._ws.recv()
                if not data:
                    break
            except websocket.WebSocketConnectionClosedException:
                logger.debug("Streaming: WebSocket closed")
                break
            except Exception as e:
                if self._running:
                    logger.error("Streaming recv error: %s", e)
                break

            try:
                msg = json.loads(data)
            except json.JSONDecodeError:
                continue

            msg_type = msg.get("message_type", "")
            logger.debug("Streaming recv: %s", msg_type)

            if msg_type == "session_started":
                session_id = msg.get("session_id", "")
                logger.info("Streaming session started: %s", session_id)

            elif msg_type == "partial_transcript":
                text = msg.get("text", "")
                if self._on_partial:
                    try:
                        self._on_partial(text)
                    except Exception as e:
                        logger.error("on_partial callback error: %s", e)

            elif msg_type == "committed_transcript":
                text = msg.get("text", "")
                logger.info("Streaming committed: %s", text[:80] if text else "(empty)")
                if text and self._on_committed:
                    try:
                        self._on_committed(text)
                    except Exception as e:
                        logger.error("on_committed callback error: %s", e)

            elif msg_type in (
                "error", "auth_error", "quota_exceeded", "rate_limited",
                "transcriber_error", "session_time_limit_exceeded",
            ):
                error_msg = msg.get("error", msg_type)
                logger.error("Streaming API error: %s", error_msg)
                if self._on_error:
                    try:
                        self._on_error(error_msg)
                    except Exception:
                        pass
                # Fatal errors — stop the session
                if msg_type in ("auth_error", "quota_exceeded", "session_time_limit_exceeded"):
                    self._running = False
                    break

        self._running = False
