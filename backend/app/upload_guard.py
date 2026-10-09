"""Upload admission at the HTTP edge (pure ASGI, so it runs BEFORE Starlette receives and spools the multipart body).

1. A declared Content-Length above the limit is answered with 413 immediately: the body is never read.
2. The bytes that actually arrive are counted, so a missing, chunked or understated Content-Length cannot get a larger
   body through. Once the count passes the limit the app is told the client disconnected (it stops reading) and 413 is sent.
3. BEFORE the 413 is sent, the rest of the body is read and DISCARDED (never stored) for a bounded time and size
   ("lingering close"). Once a response has started, ASGI servers report a disconnect and a client that is still sending gets
   a TCP reset, so it would never see the 413. The cost of an oversized upload is therefore bounded bandwidth, not memory.
Only the document upload routes are guarded; other routes keep their normal behaviour."""
from __future__ import annotations

import asyncio
import json
import logging
import re

log = logging.getLogger("eduos.upload")
_UPLOAD = re.compile(r"^/v1/documents(/[^/]+/file)?$")
FRAMING_ALLOWANCE = 256 * 1024      # multipart boundaries, headers and the small form fields around the file
DRAIN_SECONDS = 15.0                # lingering close: at most this long ...
DRAIN_FLOOR = 128 * 1024 * 1024     # ... and at most max(2 x limit, this many bytes) is read and thrown away


def _is_upload(scope) -> bool:
    return scope["method"] in ("POST", "PUT") and bool(_UPLOAD.match(scope["path"]))


class UploadSizeGuard:
    def __init__(self, app, max_file_bytes: int):
        self.app, self.limit = app, max_file_bytes + FRAMING_ALLOWANCE
        self.max_file_bytes = max_file_bytes

    async def _drain(self, receive) -> None:
        loop, deadline, seen = asyncio.get_running_loop(), asyncio.get_running_loop().time() + DRAIN_SECONDS, 0
        while seen < max(2 * self.limit, DRAIN_FLOOR):
            try:
                msg = await asyncio.wait_for(receive(), timeout=max(0.01, deadline - loop.time()))
            except Exception:                  # timeout, disconnect or a closed channel: the client is gone or too slow
                return
            if msg["type"] != "http.request":
                return
            seen += len(msg.get("body", b""))
            if not msg.get("more_body"):
                return

    async def _reject(self, send, receive, trace_id: str | None) -> None:
        log.warning("upload rejected at the edge: too large", extra={"limit": self.max_file_bytes, "trace_id": trace_id})
        await self._drain(receive)
        body = json.dumps({"error": {"code": "FILE_TOO_LARGE", "trace_id": trace_id,
                                     "message": f"File exceeds the {self.max_file_bytes} byte upload limit",
                                     "details": {"max_bytes": self.max_file_bytes}}}).encode()
        await send({"type": "http.response.start", "status": 413, "headers": [
            (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
            (b"connection", b"close")]})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not _is_upload(scope):
            return await self.app(scope, receive, send)
        headers = dict(scope["headers"])
        trace = (headers.get(b"x-trace-id") or b"").decode("latin-1")[:64] or None
        declared = headers.get(b"content-length")
        if declared is not None:
            try:
                if int(declared) > self.limit:
                    return await self._reject(send, receive, trace)
            except ValueError:
                pass                            # malformed header: the real byte count below still applies
        seen, tripped = 0, False

        async def counted():
            nonlocal seen, tripped
            if tripped:
                return {"type": "http.disconnect"}
            msg = await receive()
            if msg["type"] == "http.request":
                seen += len(msg.get("body", b""))
                if seen > self.limit:
                    tripped = True
                    return {"type": "http.disconnect"}      # stop the app reading; we answer below
            return msg

        async def guarded_send(msg):
            if not tripped:                       # whatever the aborted request handler tries to say is discarded
                await send(msg)

        try:
            await self.app(scope, counted, guarded_send)
        except Exception:
            if not tripped:
                raise
        if tripped:
            await self._reject(send, receive, trace)
