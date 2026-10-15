"""Multi-client loopback transport tests; no physical aircraft connection."""
import socket
import threading
import time
import unittest

from mavlink_hub import Hub, Frames, Client


def frame(payload=b'abc', signed=False, v1=False):
    if v1:return bytes([0xfe,len(payload),0,200,1,0])+payload+b'\x00\x00'
    return bytes([0xfd,len(payload),int(signed),0,0,200,1,0,0,0])+payload+b'\x00\x00'+(b'x'*13 if signed else b'')


def exact(sock,count):
    data=b''
    while len(data)<count:
        block=sock.recv(count-len(data))
        if not block:raise AssertionError('Unexpected EOF')
        data+=block
    return data


class Tests(unittest.TestCase):
    def setUp(self):
        self.peer=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
        self.peer.bind(('127.0.0.1',0));self.peer.settimeout(2)
        self.hub=Hub(tcp=('127.0.0.1',0),udp=('127.0.0.1',0))
        self.thread=threading.Thread(target=self.hub.serve,daemon=True);self.thread.start()
        self.clients=[]
    def tearDown(self):
        self.hub.stopping.set();self.thread.join(3);self.hub.close();self.peer.close()
        for client in self.clients:client.close()
    def connect(self):
        client=socket.create_connection(self.hub.listener.getsockname(),timeout=2)
        client.settimeout(2);self.clients.append(client)
        for _ in range(100):
            if len(self.hub.clients)>=len(self.clients):break
            time.sleep(.01)
        return client
    def broadcast(self,data):self.peer.sendto(data,self.hub.udp.getsockname())
    def test_all_clients_receive_and_write_without_frame_interleaving(self):
        a,b=self.connect(),self.connect()
        heartbeat=frame(b'heartbeat');self.broadcast(heartbeat)
        self.assertEqual(exact(a,len(heartbeat)),heartbeat);self.assertEqual(exact(b,len(heartbeat)),heartbeat)
        command_a=frame(b'A',signed=True);command_b=frame(b'B',v1=True)
        a.sendall(command_a[:6]);b.sendall(command_b)
        self.assertEqual(self.peer.recvfrom(1024)[0],command_b)
        a.sendall(command_a[6:]);self.assertEqual(self.peer.recvfrom(1024)[0],command_a)
        a.close();self.broadcast(heartbeat);self.assertEqual(exact(b,len(heartbeat)),heartbeat)
    def test_no_command_is_queued_for_later_link_recovery(self):
        client=self.connect();client.sendall(frame(b'command'))
        self.assertEqual(client.recv(1024),b'')
        self.broadcast(frame(b'recovered'))
        self.peer.settimeout(.25)
        with self.assertRaises(socket.timeout):self.peer.recvfrom(1024)
    def test_partial_frames_remain_separate_and_preserve_signatures(self):
        parser=Frames();v1=frame(v1=True);signed=frame(signed=True)
        self.assertEqual(parser.feed(b'noise'+signed[:9]),[])
        self.assertEqual(parser.feed(signed[9:]+v1),[signed,v1])
        self.assertEqual(parser.buffer,b'')
    def test_slow_client_is_dropped_without_blocking_healthy_client(self):
        # Run the broadcast queue deterministically without the I/O thread draining it.
        self.hub.stopping.set();self.thread.join(3)
        slow,other=socket.socketpair();fast,reader=socket.socketpair()
        try:
            a,b=Client(slow),Client(fast)
            self.hub.clients[slow]=a;self.hub.clients[fast]=b
            self.hub.selector.register(slow,1,a);self.hub.selector.register(fast,1,b)
            a.pending.extend(b'x'*self.hub.max_pending)
            self.hub.broadcast(frame())
            self.assertNotIn(slow,self.hub.clients);self.assertIn(fast,self.hub.clients)
            self.assertEqual(b.pending,frame())
        finally:other.close();reader.close()


if __name__=='__main__':unittest.main()
