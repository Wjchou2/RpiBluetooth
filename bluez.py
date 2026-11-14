"""BlueZ discovery, numeric comparison, and Nano UART peripheral."""
import asyncio
import struct
from dbus_next import BusType, DBusError, Variant
from dbus_next.aio import MessageBus
from dbus_next.constants import PropertyAccess
from dbus_next.errors import InterfaceNotFoundError
from dbus_next.service import ServiceInterface, dbus_property, method

BASE = "-B5A3-F393-E0A9-E50E24DCCA9E"
ROOT = "/org/nanoapps/sender"
GATT_ROOT = ROOT + "/gatt"
SERVICE = GATT_ROOT + "/service"
UUID = "6E400001" + BASE


class Agent(ServiceInterface):
    def __init__(self, radio):
        super().__init__("org.bluez.Agent1")
        self.radio = radio

    @method()
    def Release(self):
        self.cancel_pending()

    @method()
    def Cancel(self):
        self.cancel_pending()

    def cancel_pending(self):
        if self.radio.confirmation and not self.radio.confirmation.done():
            self.radio.confirmation.set_result(False)

    @method()
    async def RequestConfirmation(self, device: "o", passkey: "u"):
        if device != self.radio.pairing_device or self.radio.confirmation:
            raise DBusError("org.bluez.Error.Rejected", "Start pairing from Nano Sender.")
        self.radio.prompt = f"{passkey:06d}"
        self.radio.confirmation = asyncio.get_running_loop().create_future()
        try:
            if not await asyncio.wait_for(self.radio.confirmation, 45):
                raise DBusError("org.bluez.Error.Rejected", "Pairing declined.")
        except TimeoutError:
            raise DBusError("org.bluez.Error.Rejected", "Confirmation expired.")
        finally:
            self.radio.prompt = None
            self.radio.confirmation = None


class Advertisement(ServiceInterface):
    def __init__(self, name):
        super().__init__("org.bluez.LEAdvertisement1")
        self.name = name

    @dbus_property(access=PropertyAccess.READ)
    def Type(self) -> "s":
        return "peripheral"

    @dbus_property(access=PropertyAccess.READ)
    def LocalName(self) -> "s":
        return self.name

    @method()
    def Release(self):
        pass


class UARTService(ServiceInterface):
    def __init__(self):
        super().__init__("org.bluez.GattService1")

    @dbus_property(access=PropertyAccess.READ)
    def UUID(self) -> "s":
        return UUID

    @dbus_property(access=PropertyAccess.READ)
    def Primary(self) -> "b":
        return True


class Characteristic(ServiceInterface):
    def __init__(self, read):
        super().__init__("org.bluez.GattCharacteristic1")
        self.read = read
        self.notifying = False
        self.value = b""
        self.records = None
        self.read_at = 0
        self.sent = 0
        self.total = 0

    @dbus_property(access=PropertyAccess.READ)
    def UUID(self) -> "s":
        return ("6E400004" if self.read else "6E400003") + BASE

    @dbus_property(access=PropertyAccess.READ)
    def Service(self) -> "o":
        return SERVICE

    @dbus_property(access=PropertyAccess.READ)
    def Flags(self) -> "as":
        return ["read"] if self.read else ["notify"]

    @dbus_property(access=PropertyAccess.READ)
    def Value(self) -> "ay":
        return self.value

    @dbus_property(access=PropertyAccess.READ)
    def Notifying(self) -> "b":
        return self.notifying

    @method()
    def StartNotify(self):
        self.notifying = True
        self.emit_properties_changed({"Notifying": True})

    @method()
    def StopNotify(self):
        self.notifying = False
        self.emit_properties_changed({"Notifying": False})

    @method()
    def ReadValue(self, options: "a{sv}") -> "ay":
        if not self.read:
            raise DBusError("org.bluez.Error.NotSupported", "Notifications only.")
        if "offset" in options and options["offset"].value:
            raise DBusError("org.bluez.Error.InvalidOffset", "Unsupported offset.")
        self.read_at = asyncio.get_running_loop().time()
        if self.records is None:
            return b"\0I"
        record = next(self.records, None)
        if record is None:
            self.records = None
            return b"\0I"
        self.sent += 1
        if self.sent >= self.total:
            self.records = None
        return record

    def notify(self, value):
        if not self.notifying:
            raise RuntimeError("Connect from Bluetooth Test and wait for the Nano to subscribe.")
        self.value = value
        self.emit_properties_changed({"Value": value})


class Radio:
    def __init__(self, adapter):
        self.path = "/org/bluez/" + adapter
        self.address = None
        self.prompt = None
        self.confirmation = None
        self.pairing_device = None
        self.advertisement = None
        self.ad_lock = asyncio.Lock()
        self.uart_registered = False
        self.tx = Characteristic(False)
        self.rx = Characteristic(True)

    async def interface(self, path, name):
        info = await self.bus.introspect("org.bluez", path)
        return self.bus.get_proxy_object("org.bluez", path, info).get_interface(name)

    async def start(self):
        self.bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        self.objects = await self.interface("/", "org.freedesktop.DBus.ObjectManager")
        self.adapter = await self.interface(self.path, "org.bluez.Adapter1")
        await self.adapter.set_powered(True)
        self.address = await self.adapter.get_address()
        self.agent_manager = await self.interface("/org/bluez", "org.bluez.AgentManager1")
        self.bus.export(ROOT + "/agent", Agent(self))
        await self.agent_manager.call_register_agent(ROOT + "/agent", "DisplayYesNo")

    async def advertise(self, uart):
        async with self.ad_lock:
            await self.set_advertisement(uart)

    async def set_advertisement(self, uart):
        name = "Nano UART" if uart else "NM-" + self.address.replace(":", "")
        if self.advertisement and self.advertisement.name == name:
            return
        ads = await self.interface(self.path, "org.bluez.LEAdvertisingManager1")
        if uart and not self.uart_registered:
            gatt = await self.interface(self.path, "org.bluez.GattManager1")
            self.bus.export(SERVICE, UARTService())
            self.bus.export(SERVICE + "/tx", self.tx)
            self.bus.export(SERVICE + "/read", self.rx)
            try:
                await gatt.call_register_application(GATT_ROOT, {})
            except Exception:
                for suffix in ("", "/tx", "/read"):
                    self.bus.unexport(SERVICE + suffix)
                raise
            self.uart_registered = True
        path = ROOT + "/advertisement"
        if self.advertisement:
            await ads.call_unregister_advertisement(path)
            self.bus.unexport(path)
            self.advertisement = None
        ad = Advertisement(name)
        self.bus.export(path, ad)
        try:
            await ads.call_register_advertisement(path, {})
        except Exception:
            self.bus.unexport(path)
            raise
        self.advertisement = ad

    async def devices(self):
        objects = await self.objects.call_get_managed_objects()
        devices = []
        for path, interfaces in objects.items():
            if not path.startswith(self.path + "/"):
                continue
            props = interfaces.get("org.bluez.Device1")
            if props and props.get("AddressType", Variant("s", "public")).value == "public":
                address = props["Address"].value
                devices.append({"address": address, "name": props.get("Name", Variant("s", address)).value,
                                "paired": props["Paired"].value, "path": path})
        return sorted(devices, key=lambda d: (not d["paired"], d["name"].lower()))

    async def scan(self):
        await self.adapter.call_set_discovery_filter({"Transport": Variant("s", "bredr")})
        await self.adapter.call_start_discovery()
        try:
            await asyncio.sleep(8)
        finally:
            await self.adapter.call_stop_discovery()

    async def pair(self, address):
        if self.pairing_device:
            raise RuntimeError("Pairing is already in progress.")
        path = self.path + "/dev_" + address.replace(":", "_")
        self.pairing_device = path
        try:
            try:
                device = await self.interface(path, "org.bluez.Device1")
            except (DBusError, InterfaceNotFoundError) as error:
                connect_device = getattr(self.adapter, "call_connect_device", None)
                if connect_device is None:
                    raise RuntimeError("Use Find devices to discover the Nano before pairing. "
                                       "Keep Bluetooth open on the Nano if it isn't listed.") from error
                path = await asyncio.wait_for(connect_device({"Address": Variant("s", address)}), 15)
                device = await self.interface(path, "org.bluez.Device1")
            if not await device.get_paired():
                try:
                    await asyncio.wait_for(device.call_pair(), 60)
                except TimeoutError:
                    await device.call_cancel_pairing()
                    raise RuntimeError("Pairing timed out.") from None
        finally:
            self.pairing_device = None

    async def confirm(self, value):
        if not self.confirmation or self.confirmation.done():
            raise RuntimeError("No pairing code is waiting for confirmation.")
        self.confirmation.set_result(value)

    async def ble_audio(self, frames, packed, hash_value, quality, autoplay, mode, cancelled):
        if not self.tx.notifying:
            raise RuntimeError("Connect from Bluetooth Test first.")
        fast = mode == "fast"
        start = b"\0" + bytes([70 if fast else 83 if autoplay else 65 if quality == 1 else 81])
        start += struct.pack("<II", frames, hash_value)
        if fast or autoplay or quality != 1:
            start += bytes([quality])
        if fast:
            start += bytes([autoplay])
        step = 14 if mode == "read" else 18 if fast else 14

        def records():
            yield start
            for i, offset in enumerate(range(0, len(packed), step)):
                prefix = bytes([0, 0x80 | (i & 127)]) if fast else b"\0D" + struct.pack("<I", offset)
                yield prefix + packed[offset:offset + step]

        try:
            if mode == "read":
                self.rx.records = records()
                self.rx.total = 1 + (len(packed) + step - 1) // step
                self.rx.sent = 0
                self.rx.read_at = asyncio.get_running_loop().time()
                while self.rx.records is not None:
                    cancelled()
                    if not self.tx.notifying or asyncio.get_running_loop().time() - self.rx.read_at > 15:
                        raise RuntimeError("The Nano stopped reading audio.")
                    await asyncio.sleep(.05)
            else:
                for packet in records():
                    cancelled()
                    self.tx.notify(packet)
                    await asyncio.sleep(.04)
        except InterruptedError:
            if self.tx.notifying:
                self.tx.notify(b"\0C")
            raise
        finally:
            self.rx.records = None
