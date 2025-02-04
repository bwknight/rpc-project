import ast
import builtins
import json
import plistlib
import typing
from collections import namedtuple
from functools import lru_cache
from typing import Mapping

from cached_property import cached_property
from construct import Int64sl

from rpcclient.client import Client
from rpcclient.darwin import objective_c_class
from rpcclient.darwin.bluetooth import Bluetooth
from rpcclient.darwin.common import CfSerializable
from rpcclient.darwin.consts import kCFAllocatorDefault, \
    CFPropertyListFormat, CFPropertyListMutabilityOptions
from rpcclient.darwin.core_graphics import CoreGraphics
from rpcclient.darwin.darwin_lief import DarwinLief
from rpcclient.darwin.fs import DarwinFs
from rpcclient.darwin.hid import Hid
from rpcclient.darwin.ioregistry import IORegistry
from rpcclient.darwin.keychain import Keychain
from rpcclient.darwin.location import Location
from rpcclient.darwin.media import DarwinMedia
from rpcclient.darwin.objective_c_symbol import ObjectiveCSymbol
from rpcclient.darwin.preferences import Preferences
from rpcclient.darwin.processes import DarwinProcesses
from rpcclient.darwin.structs import utsname
from rpcclient.darwin.symbol import DarwinSymbol
from rpcclient.darwin.syslog import Syslog
from rpcclient.darwin.time import Time
from rpcclient.darwin.xpc import Xpc
from rpcclient.exceptions import RpcClientException, MissingLibraryError, GettingObjectiveCClassError
from rpcclient.protocol import arch_t, protocol_message_t, cmd_type_t
from rpcclient.structs.consts import RTLD_NOW
from rpcclient.symbol import Symbol
from rpcclient.symbols_jar import SymbolsJar

IsaMagic = namedtuple('IsaMagic', 'mask value')
ISA_MAGICS = [
    # ARM64
    IsaMagic(mask=0x000003f000000001, value=0x000001a000000001),
    # X86_64
    IsaMagic(mask=0x001f800000000001, value=0x001d800000000001),
]
# Mask for tagged pointer, from objc-internal.h
OBJC_TAG_MASK = (1 << 63)


class DarwinClient(Client):
    def __init__(self, sock, sysname: str, arch: arch_t, create_socket_cb: typing.Callable):
        super().__init__(sock, sysname, arch, create_socket_cb)
        self._dlsym_global_handle = -2  # RTLD_GLOBAL
        self._init_process_specific()

    def _init_process_specific(self):
        super(DarwinClient, self)._init_process_specific()

        if 0 == self.dlopen("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation", RTLD_NOW):
            raise MissingLibraryError('failed to load CoreFoundation')

        self.fs = DarwinFs(self)
        self.preferences = Preferences(self)
        self.processes = DarwinProcesses(self)
        self.media = DarwinMedia(self)
        self.ioregistry = IORegistry(self)
        self.location = Location(self)
        self.xpc = Xpc(self)
        self.syslog = Syslog(self)
        self.time = Time(self)
        self.hid = Hid(self)
        self.lief = DarwinLief(self)
        self.bluetooth = Bluetooth(self)
        self.core_graphics = CoreGraphics(self)
        self.keychain = Keychain(self)
        self._NSPropertyListSerialization = self.objc_get_class('NSPropertyListSerialization')

    @property
    def modules(self) -> typing.List[str]:
        m = []
        for i in range(self.symbols._dyld_image_count()):
            m.append(self.symbols._dyld_get_image_name(i).peek_str())
        return m

    @cached_property
    def uname(self):
        with self.safe_calloc(utsname.sizeof()) as uname:
            assert 0 == self.symbols.uname(uname)
            return utsname.parse_stream(uname)

    @cached_property
    def is_idevice(self):
        return self.uname.machine.startswith('i')

    @property
    def roots(self) -> typing.List[str]:
        """ get a list of all accessible darwin roots when used for lookup of files/preferences/... """
        return ['/', '/var/root']

    def showobject(self, object_address: Symbol) -> Mapping:
        message = protocol_message_t.build({
            'cmd_type': cmd_type_t.CMD_SHOWOBJECT,
            'data': {'address': object_address},
        })
        with self._protocol_lock:
            self._sock.sendall(message)
            response_len = Int64sl.parse(self._recvall(Int64sl.sizeof()))
            response = self._recvall(response_len)
        return json.loads(response)

    def showclass(self, class_address: Symbol) -> Mapping:
        message = protocol_message_t.build({
            'cmd_type': cmd_type_t.CMD_SHOWCLASS,
            'data': {'address': class_address},
        })
        with self._protocol_lock:
            self._sock.sendall(message)
            response_len = Int64sl.parse(self._recvall(Int64sl.sizeof()))
            response = self._recvall(response_len)
        return json.loads(response)

    def symbol(self, symbol: int):
        """ at a symbol object from a given address """
        return DarwinSymbol.create(symbol, self)

    def decode_cf(self, symbol: Symbol) -> CfSerializable:
        objc_data = self._NSPropertyListSerialization.dataWithPropertyList_format_options_error_(
            symbol, CFPropertyListFormat.kCFPropertyListBinaryFormat_v1_0, 0, 0)
        if objc_data == 0:
            return None
        count = self.symbols.CFDataGetLength(objc_data)
        result = plistlib.loads(self.symbols.CFDataGetBytePtr(objc_data).peek(count))
        objc_data.objc_call('release')
        return result

    def cf(self, o: CfSerializable) -> DarwinSymbol:
        """ construct a CFObject from a given python object """
        if o is None:
            return self.symbols.kCFNull[0]

        plist_bytes = plistlib.dumps(o, fmt=plistlib.FMT_BINARY)
        plist_objc_bytes = self.symbols.CFDataCreate(kCFAllocatorDefault, plist_bytes, len(plist_bytes))
        return self._NSPropertyListSerialization.propertyListWithData_options_format_error_(
            plist_objc_bytes, CFPropertyListMutabilityOptions.kCFPropertyListMutableContainersAndLeaves, 0, 0)

    def objc_symbol(self, address) -> ObjectiveCSymbol:
        """
        Get objc symbol wrapper for given address
        :param address:
        :return: ObjectiveC symbol object
        """
        return ObjectiveCSymbol.create(int(address), self)

    @lru_cache(maxsize=None)
    def objc_get_class(self, name: str):
        """
        Get ObjC class object
        :param name:
        :return:
        """
        return objective_c_class.Class.from_class_name(self, name)

    @staticmethod
    def is_objc_type(symbol: DarwinSymbol) -> bool:
        """
        Test if a given symbol represents an objc object
        :param symbol:
        :return:
        """
        # Tagged pointers are ObjC objects
        if symbol & OBJC_TAG_MASK == OBJC_TAG_MASK:
            return True

        # Class are not ObjC objects
        for mask, value in ISA_MAGICS:
            if symbol & mask == value:
                return False

        try:
            with symbol.change_item_size(8):
                isa = symbol[0]
        except RpcClientException:
            return False

        for mask, value in ISA_MAGICS:
            if isa & mask == value:
                return True

        return False

    def _ipython_run_cell_hook(self, info):
        """
        Enable lazy loading for symbols
        :param info: IPython's CellInf4o object
        """
        super()._ipython_run_cell_hook(info)

        if info.raw_cell.startswith('!') or info.raw_cell.endswith('?'):
            return

        for node in ast.walk(ast.parse(info.raw_cell)):
            if not isinstance(node, ast.Name):
                # we are only interested in names
                continue

            if node.id in locals() or node.id in globals() or node.id in dir(builtins):
                # That are undefined
                continue

            if not hasattr(SymbolsJar, node.id):
                # ignore SymbolsJar properties
                try:
                    symbol = self.objc_get_class(node.id)
                except GettingObjectiveCClassError:
                    pass
                else:
                    self._add_global(
                        node.id,
                        symbol
                    )
