"""Read-only resource observations; disposable 30-second display cache (task-242)."""
import inspect
import json
import math
import os
import threading
import time

FIELDS = ("ramTotal", "ramAvailable", "swapUsed", "swapTotal", "diskTotal", "diskFree", "load1", "cores")


def empty(name, reachable=False):
    return dict(name=name, reachable=reachable, **dict.fromkeys(FIELDS))


def read_meminfo():
    values = {}
    try:
        with open("/proc/meminfo") as stream:
            for line in stream:
                key, value = line.split(":", 1)
                values[key] = int(value.split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return values


def read_local(path):
    row = dict.fromkeys(("ramTotal", "ramAvailable", "swapUsed", "swapTotal", "diskTotal", "diskFree", "load1", "cores"))
    mem = read_meminfo()
    row["ramTotal"] = mem.get("MemTotal")
    row["ramAvailable"] = mem.get("MemAvailable")
    if "SwapTotal" in mem and "SwapFree" in mem:
        row["swapUsed"] = max(0, mem["SwapTotal"] - mem["SwapFree"])
        row["swapTotal"] = mem["SwapTotal"]
    try:
        disk = os.statvfs(os.path.expanduser(path))
        row.update(diskTotal=disk.f_blocks * disk.f_frsize, diskFree=disk.f_bavail * disk.f_frsize)
    except (OSError, ValueError):
        pass
    try:
        row["load1"] = os.getloadavg()[0]
    except (OSError, AttributeError):
        pass
    row["cores"] = os.cpu_count()
    return row


def parse_worker(name, output):
    row = empty(name, True)
    try:
        data = json.loads(output)
        if not isinstance(data, dict):
            return row
    except (ValueError, TypeError):
        return row
    row["reachable"] = True
    for key in FIELDS:
        value = data.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
            if key in ("cores", "ramTotal", "diskTotal") and value == 0:
                continue
            row[key] = value
    return row


WORKER_SCRIPT = ("import os,json,sys\n" + inspect.getsource(read_meminfo) + "\n" +
                 inspect.getsource(read_local) + "\nprint(json.dumps(read_local(sys.argv[1])))")


def read_worker(config, machine):
    import machines
    # Reuse the portable reader, without importing Centrale on the worker.
    try:
        result = machines.run_on_machine(config, machine["name"],
            ["python3", "-c", WORKER_SCRIPT, machine["worktreeRoot"]], timeout=5)
        if result.status == machines.ANSWERED:
            # An answered command with no reader (e.g. missing python) is reachable,
            # but offers no figures. A lost/timed-out command is unreachable.
            if result.returncode != 0:
                return empty(machine["name"], True)
            return parse_worker(machine["name"], result.stdout)
    except (OSError, TimeoutError):
        pass
    return empty(machine["name"])


class Cache:
    def __init__(self):
        self.lock = threading.Lock()
        self.key = None
        self.rows = []
        self.updated = float("-inf")
        self.thread = None

    def get(self, config):
        paths = [p["path"] for p in config.get("projects", [])]
        path = paths[0] if paths else os.getcwd()
        workers = config.get("machines") or []
        key = json.dumps([path, workers], sort_keys=True)
        with self.lock:
            if key != self.key:
                self.key = key
                self.updated = float("-inf")
                self.rows = [dict(read_local(path), name="hub", reachable=True)] + [empty(m["name"], None) for m in workers]
            if time.monotonic() - self.updated >= 30 and not (self.thread and self.thread.is_alive()):
                self.rows[0] = dict(read_local(path), name="hub", reachable=True)
                self.updated = time.monotonic()
                if workers:
                    self.thread = threading.Thread(target=self.refresh, args=(config, workers, key), daemon=True)
                    self.thread.start()
            return {"machines": [dict(row) for row in self.rows]}

    def refresh(self, config, workers, key):
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(workers)) as pool:
            rows = list(pool.map(lambda m: read_worker(config, m), workers))
        with self.lock:
            if self.key == key:
                self.rows[1:] = rows


cache = Cache()
