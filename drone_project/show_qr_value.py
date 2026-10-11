from pymavlink import mavutil

master = mavutil.mavlink_connection('udp:127.0.0.1:14550')

while True:
    msg = master.recv_match(type='NAMED_VALUE_INT', blocking=True)

    if msg is None:
        continue

    name = msg.name
    if isinstance(name, bytes):
        name = name.decode(errors='replace')

    if name.rstrip('\x00') == 'QR':
        print(f"Current QR: {msg.value}")
        break
