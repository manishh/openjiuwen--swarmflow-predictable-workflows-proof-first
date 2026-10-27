"""Minimal JiuwenSwarm web-channel client for driving SwarmFlow headlessly.

The web UI talks to `jiuwenswarm-start app` over ws://127.0.0.1:19000/ws with frames
{"type": "req", "id", "method", "params"}; replies are {"type": "res"} and pushes are
{"type": "event", "payload": {"event_type": ...}}. We use the same API.
"""
import asyncio, json, os, socket, subprocess, time, uuid
from pathlib import Path
import psutil, websockets

PORTS = (19000, 19001, 18092)  # web channel, gateway, agent server

def port_open(port):
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0

class Server:
    def __init__(self, home, venv_bin, cwd, log_path):
        self.home, self.venv_bin, self.cwd, self.log_path = Path(home), Path(venv_bin), Path(cwd), Path(log_path)
        self.proc = None
    def start(self, api_key, timeout=240):
        # jiuwenswarm-start rewrites these ports from <home>/.jiuwenswarm/config/.env on launch,
        # so they cannot be moved with env vars; wait for a previous instance to release them.
        t0 = time.time()
        while any(port_open(p) for p in PORTS):
            if time.time() - t0 > 90:
                busy = [p for p in PORTS if port_open(p)]
                raise RuntimeError(f"ports {busy} busy; stop other JiuwenSwarm instances first")
            time.sleep(2)
        env = dict(os.environ)
        env["JIUWENSWARM_HOME"] = str(self.home)
        env["API_KEY"] = api_key  # child process env only; never written to disk
        env.pop("OPENAI_API_KEY", None)
        env.pop("ANTHROPIC_API_KEY", None)
        self.logf = open(self.log_path, "w")
        self.proc = subprocess.Popen([str(self.venv_bin / "jiuwenswarm-start"), "app"], cwd=self.cwd, env=env,
                                     stdout=self.logf, stderr=subprocess.STDOUT, start_new_session=True)
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self.proc.poll() is not None:
                raise RuntimeError("jiuwenswarm-start exited early; see " + str(self.log_path))
            if port_open(19000) and port_open(18092):
                time.sleep(3)
                return
            time.sleep(1)
        raise RuntimeError("server did not open ports in time")
    def stop(self):
        if not self.proc:
            return
        try:
            parent = psutil.Process(self.proc.pid)
            procs = parent.children(recursive=True) + [parent]
        except psutil.NoSuchProcess:
            procs = []
        for p in procs:
            try: p.terminate()
            except psutil.NoSuchProcess: pass
        gone, alive = psutil.wait_procs(procs, timeout=15)
        for p in alive:
            try: p.kill()
            except psutil.NoSuchProcess: pass
        self.logf.close()

class Client:
    def __init__(self, events_path, url="ws://127.0.0.1:19000/ws"):
        self.url, self.events_path = url, Path(events_path)
        self.frames = []          # every frame received
        self.cond = asyncio.Condition()
    async def __aenter__(self):
        self.ws = await websockets.connect(self.url, max_size=None, ping_interval=None)
        self.evf = open(self.events_path, "a")
        self.reader = asyncio.create_task(self._read())
        return self
    async def __aexit__(self, *a):
        self.reader.cancel()
        await self.ws.close()
        self.evf.close()
    async def _read(self):
        async for raw in self.ws:
            try: fr = json.loads(raw)
            except Exception: continue
            fr["_t"] = time.time()
            self.evf.write(json.dumps(fr, ensure_ascii=False) + "\n"); self.evf.flush()
            async with self.cond:
                self.frames.append(fr)
                self.cond.notify_all()
    async def req(self, method, params):
        rid = f"{method}-{uuid.uuid4().hex[:8]}"
        frame = {"type": "req", "id": rid, "method": method, "params": params}
        if method == "chat.send":
            frame["is_stream"] = True
        await self.ws.send(json.dumps(frame))
        return rid
    async def wait_for(self, pred, timeout, start=0):
        """Wait until some frame at index >= start satisfies pred; return (index, frame)."""
        deadline = time.time() + timeout
        async with self.cond:
            while True:
                for i in range(start, len(self.frames)):
                    if pred(self.frames[i]):
                        return i, self.frames[i]
                rem = deadline - time.time()
                if rem <= 0:
                    raise TimeoutError("timed out waiting for event")
                try:
                    await asyncio.wait_for(self.cond.wait(), rem)
                except asyncio.TimeoutError:
                    pass

def payload(fr):
    return fr.get("payload") or {}
def etype(fr):
    p = payload(fr)
    return p.get("event_type") or fr.get("event")
