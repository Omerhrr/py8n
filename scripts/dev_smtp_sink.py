"""The dev SMTP sink - a minimal, REAL SMTP server for rehearsals.

The escalation door (and its v89 digest) delivers over whatever channel
endpoint the operator's policy names - and email rides SMTP, a real
protocol over a real socket. Proving a digest "end to end" therefore
needs an inbox: this file IS the inbox. A ~120-line RFC 5321 subset that
answers on a real TCP port, advertises AUTH (accepting any credentials -
it is a sink, not a doorman), takes MAIL FROM / RCPT TO / DATA, and
stores every message it accepts (raw bytes + parsed subject/text).

It exists so tests and live smokes can bind a REAL ``email_inbound``
channel endpoint (smtp_host=127.0.0.1, smtp_port=<sink port>) and watch
a digest cross the actual wire: policy -> door -> bucket -> render ->
adapter -> RFC 5322 message -> SMTP conversation -> sink. No mocks of
py8n code - only the last-mile inbox is stood up locally.

Library use (tests, smokes):

    sink = SmtpDevSink(port=8250)
    sink.start()            # daemon thread + its own event loop
    ...
    sink.stop()
    sink.messages           # [{from, to, data, subject, text, at}]
    sink.count              # how many messages the sink accepted

CLI (a dev inbox you can point any endpoint at):

    python scripts/dev_smtp_sink.py --port 8250 --limit 3

The protocol subset (exactly what smtplib speaks on a submission port):

  220 greet -> EHLO/HELO (250 + AUTH PLAIN LOGIN advertised)
  AUTH PLAIN [initial] / AUTH LOGIN (334 steps) -> 235 (any creds)
  MAIL FROM:<...> -> 250 | RCPT TO:<...> -> 250
  DATA -> 354, dot-stuffed body until ".", stored -> 250
  RSET / NOOP / QUIT handled; unknown verbs -> 502
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import threading
from datetime import datetime, timezone


class SmtpDevSink:
    """A tiny SMTP inbox on a real socket. Thread-safe message store."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0) -> None:
        self.host = host
        self.port = port  # 0 = let the OS pick a free port
        self.messages: list[dict] = []
        self._lock = threading.Lock()
        self._server: asyncio.AbstractServer | None = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._started = threading.Event()

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> "SmtpDevSink":
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="dev-smtp-sink")
        self._thread.start()
        if not self._started.wait(timeout=10):
            raise RuntimeError("the dev SMTP sink never opened its port")
        return self

    def stop(self) -> None:
        if self._loop is not None and self._server is not None:
            self._loop.call_soon_threadsafe(self._server.close)
        if self._thread is not None:
            self._thread.join(timeout=5)

    @property
    def count(self) -> int:
        with self._lock:
            return len(self.messages)

    def last(self) -> dict | None:
        with self._lock:
            return self.messages[-1] if self.messages else None

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        async def _serve():
            self._server = await asyncio.start_server(
                self._handle, self.host, self.port)
            self.port = self._server.sockets[0].getsockname()[1]
            self._started.set()
            async with self._server:
                await self._server.serve_forever()

        try:
            self._loop.run_until_complete(_serve())
        except asyncio.CancelledError:
            pass
        finally:
            self._loop.close()

    # -- the conversation ---------------------------------------------------

    async def _handle(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        txn: dict = {"from": "", "to": []}
        try:
            await self._reply(writer, "220 dev-smtp-sink ready")
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=60)
                if not line:
                    break
                verb = line.decode("utf-8", "replace").rstrip("\r\n")
                upper = verb.upper()
                if upper.startswith("EHLO") or upper.startswith("HELO"):
                    await self._reply(writer, "250-dev-smtp-sink",
                                      "250-AUTH PLAIN LOGIN", "250 OK")
                elif upper.startswith("AUTH PLAIN"):
                    # optional initial response on the same line
                    parts = verb.split(None, 2)
                    if len(parts) < 3:
                        await self._reply(writer, "334 ")
                        await reader.readline()
                    await self._reply(writer, "235 2.7.0 Accepted")
                elif upper.startswith("AUTH LOGIN"):
                    await self._reply(writer, "334 VXNlcm5hbWU6")   # "Username:"
                    await reader.readline()
                    await self._reply(writer, "334 UGFzc3dvcmQ6")   # "Password:"
                    await reader.readline()
                    await self._reply(writer, "235 2.7.0 Accepted")
                elif upper.startswith("MAIL FROM:"):
                    txn = {"from": verb.split(":", 1)[1].strip(" <>"),
                           "to": []}
                    await self._reply(writer, "250 2.1.0 OK")
                elif upper.startswith("RCPT TO:"):
                    txn["to"].append(verb.split(":", 1)[1].strip(" <>"))
                    await self._reply(writer, "250 2.1.5 OK")
                elif upper == "DATA":
                    if not txn.get("to"):
                        await self._reply(writer, "503 5.5.1 no recipient")
                        continue
                    await self._reply(
                        writer, "354 End data with <CR><LF>.<CR><LF>")
                    raw = await self._read_data(reader)
                    self._store(txn, raw)
                    await self._reply(writer, "250 2.0.0 OK: stored")
                    txn = {"from": "", "to": []}
                elif upper == "RSET":
                    txn = {"from": "", "to": []}
                    await self._reply(writer, "250 2.0.0 OK")
                elif upper == "NOOP":
                    await self._reply(writer, "250 2.0.0 OK")
                elif upper == "QUIT":
                    await self._reply(writer, "221 2.0.0 bye")
                    break
                else:
                    await self._reply(writer, "502 5.5.2 not implemented")
        except (asyncio.TimeoutError, ConnectionError):
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # noqa: BLE001 - the peer may already be gone
                pass

    async def _read_data(self, reader: asyncio.StreamReader) -> bytes:
        """Read the DATA blob: lines until a lone '.', un-dot-stuffed."""
        chunks: list[bytes] = []
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=60)
            if not line:
                break
            if line in (b".\r\n", b".\n"):
                break
            if line.startswith(b"."):  # RFC 5321 dot-stuffing
                line = line[1:]
            chunks.append(line)
        return b"".join(chunks)

    def _store(self, txn: dict, raw: bytes) -> None:
        subject, text = "", ""
        try:
            import email as _email
            from email import policy as _policy

            msg = _email.message_from_bytes(raw, policy=_policy.default)
            subject = str(msg.get("Subject") or "")
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        text = part.get_content()
                        break
            else:
                text = msg.get_content()
        except Exception:  # noqa: BLE001 - the sink keeps the raw bytes anyway
            text = raw.decode("utf-8", "replace")
        entry = {"from": txn.get("from", ""), "to": list(txn.get("to", [])),
                 "data": raw, "subject": subject, "text": text,
                 "at": datetime.now(timezone.utc).isoformat()}
        with self._lock:
            self.messages.append(entry)

    @staticmethod
    async def _reply(writer: asyncio.StreamWriter, *lines: str) -> None:
        writer.write(("".join(f"{ln}\r\n" for ln in lines)).encode("utf-8"))
        await writer.drain()


def main() -> int:
    ap = argparse.ArgumentParser(description="py8n dev SMTP sink")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8250)
    ap.add_argument("--limit", type=int, default=0,
                    help="exit after N messages (0 = run forever)")
    args = ap.parse_args()

    sink = SmtpDevSink(host=args.host, port=args.port).start()
    print(f"dev-smtp-sink listening on {args.host}:{sink.port}", flush=True)
    import time

    try:
        while True:
            if args.limit and sink.count >= args.limit:
                break
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        sink.stop()
        for m in sink.messages:
            print(f"- from {m['from']} to {m['to']}: {m['subject']}")
        print(f"{sink.count} message(s) accepted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
