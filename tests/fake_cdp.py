"""A fake Chrome DevTools endpoint: /json/version, /json/list and a browser websocket with scripted answers.

Runs in its own thread so sync code (httpx.get) and async code can both talk to it.

    with FakeCdp({"Storage.getCookies": {"cookies": [...]}}) as fake:
        fake.port, fake.calls            # calls: [(method, params, sessionId), ...]
        fake.emit("Network.requestWillBeSent", {...}, session="s1")
"""

from __future__ import annotations

import asyncio
import json
import threading

from websockets.asyncio.server import serve


class FakeCdp:
    def __init__(self, responses: dict | None = None, targets: list[dict] | None = None):
        # responses: method -> result dict, or callable(params, session) -> result dict
        self.responses = responses or {}
        self.targets = targets or []
        self.calls: list[tuple] = []
        self.port = 0
        self._conns: set = set()

    def _http(self, connection, request):
        if request.path == "/json/version":
            body = {
                "Browser": "FakeChrome/1",
                "webSocketDebuggerUrl": f"ws://127.0.0.1:{self.port}/devtools/browser/fake",
            }
        elif request.path in ("/json/list", "/json"):
            body = self.targets
        else:
            return None  # websocket upgrade
        resp = connection.respond(200, json.dumps(body))
        resp.headers["Content-Type"] = "application/json"
        return resp

    async def _handler(self, ws):
        self._conns.add(ws)
        try:
            async for raw in ws:
                msg = json.loads(raw)
                method, params, session = msg["method"], msg.get("params") or {}, msg.get("sessionId")
                self.calls.append((method, params, session))
                answer = self.responses.get(method, {})
                if callable(answer):
                    answer = answer(params, session)
                reply = {"id": msg["id"], "result": answer}
                if session:
                    reply["sessionId"] = session
                await ws.send(json.dumps(reply))
        finally:
            self._conns.discard(ws)

    def emit(self, method: str, params: dict | None = None, session: str | None = None) -> None:
        """Push an event to every connected client."""
        msg = {"method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        text = json.dumps(msg)
        fut = asyncio.run_coroutine_threadsafe(self._broadcast(text), self._loop)
        fut.result(5)

    async def _broadcast(self, text: str) -> None:
        for ws in list(self._conns):
            await ws.send(text)

    async def _main(self) -> None:
        async with serve(self._handler, "127.0.0.1", 0, process_request=self._http) as server:
            self.port = server.sockets[0].getsockname()[1]
            self._stop = asyncio.Event()
            self._ready.set()
            await self._stop.wait()

    def __enter__(self) -> FakeCdp:
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._loop.run_until_complete, args=(self._main(),), daemon=True)
        self._thread.start()
        if not self._ready.wait(5):
            raise RuntimeError("fake CDP server did not start")
        return self

    def __exit__(self, *exc) -> None:
        self._loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(5)
        self._loop.close()
