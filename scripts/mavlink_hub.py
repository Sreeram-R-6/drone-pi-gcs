#!/usr/bin/env python3
"""Bidirectional, multi-client TCP fanout of the existing router's UDP 14552 output."""
import argparse
from dataclasses import dataclass, field
import logging
import selectors
import signal
import socket
import threading
import time

LOG = logging.getLogger('mavlink-hub')
MAX_PENDING = 256 * 1024


class Frames:
    """Reassemble each client's stream independently; preserve signed/v1/v2 bytes."""
    def __init__(self):
        self.buffer = bytearray()

    def feed(self, data):
        self.buffer.extend(data)
        result = []
        while self.buffer:
            if self.buffer[0] not in (0xfe, 0xfd):
                del self.buffer[0]
                continue
            if len(self.buffer) < 3:
                break
            length = self.buffer[1] + (8 if self.buffer[0] == 0xfe else 12)
            if self.buffer[0] == 0xfd and self.buffer[2] & 1:
                length += 13
            if len(self.buffer) < length:
                break
            result.append(bytes(self.buffer[:length]))
            del self.buffer[:length]
        return result


@dataclass
class Client:
    socket: socket.socket
    pending: bytearray = field(default_factory=bytearray)
    frames: Frames = field(default_factory=Frames)


class Hub:
    def __init__(self, tcp=('127.0.0.1', 5760), udp=('127.0.0.1', 14552), max_pending=MAX_PENDING):
        self.selector = selectors.DefaultSelector()
        self.clients = {}
        self.upstream = None
        self.upstream_seen = 0
        self.max_pending = max_pending
        self.stopping = threading.Event()
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self.udp.bind(udp)
            self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.listener.bind(tcp)
            self.listener.listen(64)
            for sock, role in ((self.udp, 'udp'), (self.listener, 'listener')):
                sock.setblocking(False)
                self.selector.register(sock, selectors.EVENT_READ, role)
        except BaseException:
            self.close()
            raise

    def drop(self, client):
        self.clients.pop(client.socket, None)
        try:self.selector.unregister(client.socket)
        except (KeyError, ValueError):pass
        client.socket.close()

    def broadcast(self, data):
        for client in list(self.clients.values()):
            if len(client.pending) + len(data) > self.max_pending:
                LOG.warning('Disconnecting slow client; other clients continue')
                self.drop(client)
                continue
            client.pending.extend(data)
            self.selector.modify(client.socket, selectors.EVENT_READ | selectors.EVENT_WRITE, client)

    def serve(self):
        while not self.stopping.is_set():
            for key, mask in self.selector.select(.2):
                role = key.data
                if role == 'listener':
                    try:sock, _ = self.listener.accept()
                    except BlockingIOError:continue
                    if len(self.clients) >= 64:
                        sock.close()
                        continue
                    sock.setblocking(False)
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    client = Client(sock)
                    self.clients[sock] = client
                    self.selector.register(sock, selectors.EVENT_READ, client)
                elif role == 'udp':
                    try:data, address = self.udp.recvfrom(65535)
                    except BlockingIOError:continue
                    if address[0] != '127.0.0.1':continue
                    self.upstream, self.upstream_seen = address, time.monotonic()
                    self.broadcast(data)
                else:
                    client = role
                    try:
                        if mask & selectors.EVENT_READ:
                            try:data = client.socket.recv(16384)
                            except BlockingIOError:data = None
                            if data == b'':
                                self.drop(client)
                                continue
                            for frame in client.frames.feed(data or b''):
                                # Do not queue control commands for later replay after a link outage.
                                if self.upstream is None or time.monotonic() - self.upstream_seen > 5:
                                    LOG.warning('Upstream unavailable; closing command client without replay')
                                    self.drop(client)
                                    break
                                self.udp.sendto(frame, self.upstream)
                        if client.socket in self.clients and mask & selectors.EVENT_WRITE:
                            try:count = client.socket.send(client.pending)
                            except BlockingIOError:continue
                            del client.pending[:count]
                            if not client.pending:
                                self.selector.modify(client.socket, selectors.EVENT_READ, client)
                    except OSError:
                        self.drop(client)

    def close(self):
        self.stopping.set()
        for client in list(self.clients.values()):self.drop(client)
        self.listener.close();self.udp.close();self.selector.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=5760)
    args = parser.parse_args()
    hub = Hub(tcp=('127.0.0.1', args.port))
    logging.basicConfig(level=logging.INFO)
    signal.signal(signal.SIGTERM, lambda *_: hub.stopping.set())
    signal.signal(signal.SIGINT, lambda *_: hub.stopping.set())
    LOG.info('TCP 127.0.0.1:%s ↔ existing UDP router 14552', args.port)
    try:hub.serve()
    finally:hub.close()


if __name__ == '__main__':main()
