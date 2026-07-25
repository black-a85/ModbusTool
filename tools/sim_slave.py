"""A tiny Modbus TCP slave/server for testing ModbusTool without hardware.

Populates holding & input registers and coils with recognizable patterns, then
serves them on 127.0.0.1:5020 (unit id 1).

Run:  python tools/sim_slave.py
Then in ModbusTool connect TCP to 127.0.0.1 port 5020, Slave ID 1.
"""

import struct

from pymodbus.datastore import (
    ModbusSequentialDataBlock, ModbusDeviceContext, ModbusServerContext,
)
from pymodbus.server import StartTcpServer


def build_context() -> ModbusServerContext:
    # holding registers: index i -> value i*10, plus a float32 at 100..101
    hold = [i * 10 for i in range(200)]
    f = struct.unpack(">HH", struct.pack(">f", 3.14159))
    hold[100], hold[101] = f[0], f[1]           # big-endian float 3.14159
    # a 32-bit unsigned 0x00012345 at 110..111
    hold[110], hold[111] = 0x0001, 0x2345

    inputs = [1000 + i for i in range(200)]
    coils = [(i % 2 == 0) for i in range(200)]
    discrete = [(i % 3 == 0) for i in range(200)]

    # NOTE: in pymodbus 3.14 ModbusSequentialDataBlock stores at (address-1),
    # so pass 1 to make the datastore start at protocol address 0.
    store = ModbusDeviceContext(
        di=ModbusSequentialDataBlock(1, discrete),
        co=ModbusSequentialDataBlock(1, coils),
        hr=ModbusSequentialDataBlock(1, hold),
        ir=ModbusSequentialDataBlock(1, inputs),
    )
    return ModbusServerContext(devices={1: store}, single=False)


if __name__ == "__main__":
    print("Modbus TCP simulator on 127.0.0.1:5020  (unit id 1)")
    print("  HR[0..]   = 0,10,20,...   HR[100..101]=float 3.14159   HR[110..111]=u32 0x00012345")
    print("  IR[0..]   = 1000,1001,...")
    print("  Coils even=ON,  Discrete every-3rd=ON")
    print("Ctrl+C to stop.")
    StartTcpServer(context=build_context(), address=("127.0.0.1", 5020))
