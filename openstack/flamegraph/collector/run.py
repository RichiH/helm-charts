#!/usr/bin/env python
import glob
import os
import signal
import six
import sys
import logging
import time
from redis import StrictRedis
from datetime import datetime

if os.name == 'posix' and sys.version_info[0] < 3:
    import subprocess32 as subprocess
else:
    import subprocess

try:
    from lz4.block import compress
except ImportError:
    from lz4 import compress

logging.basicConfig()
LOG = logging.getLogger(__name__)
LOG.setLevel(logging.DEBUG)

LOG.info("Starting up")
db = StrictRedis(host=os.getenv('FLAMEGRAPH_REDIS_SERVICE_HOST'),
                    password=os.getenv('FLAMEGRAPH_REDIS_SERVICE_PASSWORD'),
                    port=os.getenv('FLAMEGRAPH_REDIS_SERVICE_PORT'),
                    socket_timeout=1.0,
                    socket_connect_timeout=1.0,
                    socket_keepalive=True
                    )
db.ping()

# PYFLAME_REDIS_SERVICE_HOST
# PYFLAME_REDIS_SERVICE_PORT_PUBLIC
pod_name = os.getenv('MY_POD_NAME', None)
node_name = os.getenv('MY_NODE_NAME', None)
DURATION=1
running = True
monitors = {}

EPOCH = datetime(1970, 1, 1)

def timestamp(now = None):
    now = now or datetime.utcnow()
    return (now - EPOCH).total_seconds()

def stop_handler(signum, frame):
    global running
    running = False
    for _, monitor in six.iteritems(monitors):
        proc = monitor['process']
        proc.send_signal(signum)

def start_monitor(pid, monitor=None):
    if not running:
        return
    monitor = monitor or monitors[pid]
    now = datetime.utcnow()
    proc = subprocess.Popen(["/usr/bin/pyflame", "-s", str(DURATION), "--threads", "-p", str(pid)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, bufsize=-1)
    monitor['process'] = proc
    monitor['timestamp'] = timestamp(now)
    # LOG.debug("Started %d for %d", proc.pid, pid)

def reaper(signum, _):
    for pid, monitor in six.iteritems(monitors):
        # LOG.debug("Checking %d", proc.pid)
        try:
            proc = monitor['process']
            proc.wait(timeout=0)
            # LOG.debug("Terminated %d", proc.pid)
            try:
                outs, _ = proc.communicate(timeout=1)
            except ValueError:
                outs = None

            if running:
                start_monitor(pid, monitor)
            if not outs:
                continue
            key = ':'.join([node_name, monitor['hostname'], monitor['name'], monitor['cgroup'], str(monitor['cgroup_pid'])])
            # LOG.debug("%s:%f: '%s'", key, monitor['timestamp'], outs)
            with db.pipeline() as pipe:
                pipe.zadd(key, monitor['timestamp'], compress(outs))
                # pipe.zadd()
                old = monitor['timestamp'] - 3600 * 24
                if old > 0:
                    pipe.zremrangebyscore(key, 0, old)
                pipe.execute()
        except subprocess.TimeoutExpired, KeyError:
            pass

signal.signal(signal.SIGTERM, stop_handler)
signal.signal(signal.SIGQUIT, stop_handler)
# signal.signal(signal.SIGCHLD, reaper)

if pod_name:
    own_ns_path = '/proc/{}/ns/net'.format(os.getpid())
    own_ns = os.stat(own_ns_path)

while running:
    reaper(signal.SIGCHLD, None)
    for ns_path in glob.glob('/proc/[1-9]*/ns/net'):
        try:
            pid_path = ns_path[:-6]
            if not 'python' in os.readlink(pid_path + 'exe') or \
                    pod_name and (ns_path == own_ns_path or os.stat(ns_path) != own_ns):
                continue
            _, _, pid, _ = pid_path.split('/', 3)
            pid = int(pid)
            cgroup_pid = pid
            if pid != os.getpid() and pid not in monitors:
                LOG.info("Discovered %d", pid)
                with open(pid_path + 'cgroup', 'r') as f:
                    cgroup = f.readline().split(':')[-1]
                with open(pid_path + 'status', 'r') as f:
                    for line in f:
                        if line.startswith('Name:\t'):
                            name = line[6:]
                        if line.startswith('NSpid:\t'):
                            for ppid in line[6:].split(None):
                                ppid = int(ppid)
                                if ppid != pid:
                                    cgroup_pid = ppid

                hostname='unknown'
                with open(pid_path + 'environ', 'r') as f:
                    for env in f.read().split('\0'):
                        if env.startswith('HOSTNAME='):
                            _, hostname = env.split("=", 2)
                            break
                monitor = { 'host_pid': pid,
                            'cgroup': cgroup.strip(),
                            'cgroup_pid': cgroup_pid,
                            'hostname': hostname.strip(),
                            'name': name.strip() }
                monitors[pid] = monitor
                start_monitor(pid, monitor)
        except OSError as e:
            pass

    sys.stdout.flush()
    time.sleep(0.01)
