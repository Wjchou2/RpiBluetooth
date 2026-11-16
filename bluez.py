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
        self.local_name = name

    @dbus_property(access=PropertyAccess.READ)
    def Type(self) -> "s":
        return "peripheral"

    @dbus_property(access=PropertyAccess.READ)
    def LocalName(self) -> "s":
        return self.local_name

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
    def __init__(self, mode, radio):
        super().__init__("org.bluez.GattCharacteristic1")
        self.mode = mode
        self.radio = radio
        self.notifying = False
        self.value = b""
        self.records = None
        self.read_at = 0
        self.sent = 0
        self.total = 0

    @dbus_property(access=PropertyAccess.READ)
    def UUID(self) -> "s":
        return {"read": "6E400004", "notify": "6E400003", "write": "6E400002"}[self.mode] + BASE

    @dbus_property(access=PropertyAccess.READ)
    def Service(self) -> "o":
        return SERVICE

    @dbus_property(access=PropertyAccess.READ)
    def Flags(self) -> "as":
        return [self.mode]

    @dbus_property(access=PropertyAccess.READ)
    def Value(self) -> "ay":
        return self.value

    @dbus_property(access=PropertyAccess.READ)
    def Notifying(self) -> "b":
        return self.notifying

    @method()
    def StartNotify(self):
        if self.mode != "notify":
            raise DBusError("org.bluez.Error.NotSupported", "Notifications unavailable.")
        self.notifying = True
        self.emit_properties_changed({"Notifying": True})

    @method()
    def StopNotify(self):
        self.notifying = False
        self.radio.audio_read.read_at = 0
        self.radio.notification_limit = 20
        self.emit_properties_changed({"Notifying": False})

    @method()
    def ReadValue(self, options: "a{sv}") -> "ay":
        if self.mode != "read":
            raise DBusError("org.bluez.Error.NotSupported", "Reading unavailable.")
        if "offset" in options and options["offset"].value:
            raise DBusError("org.bluez.Error.InvalidOffset", "Unsupported offset.")
        if not self.radio.tx.notifying:
            return b"\0I"
        self.radio.observe_mtu(options)
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

    @method()
    def WriteValue(self, value: "ay", options: "a{sv}"):
        if self.mode != "write" or not self.radio.tx.notifying:
            raise DBusError("org.bluez.Error.NotSupported", "Subscribe before writing.")
        if "offset" in options and options["offset"].value:
            raise DBusError("org.bluez.Error.InvalidOffset", "Unsupported offset.")
        if not 1 <= len(value) <= 20 or not all(32 <= byte <= 126 for byte in value):
            raise DBusError("org.bluez.Error.InvalidValueLength", "Enter 1–20 printable ASCII characters.")
        self.radio.observe_mtu(options)
        self.radio.received_message = bytes(value).decode("ascii")

    def notify(self, value):
        if not self.notifying:
            raise RuntimeError("Connect from BLE Chat or Bluetooth Test and wait for the Nano to subscribe.")
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
        self.tx = Characteristic("notify", self)
        self.rx = Characteristic("write", self)
        self.audio_read = Characteristic("read", self)
        self.received_message = ""
        self.notification_limit = 20
        self.scan_stop = None
        self.pair_task = None

    def observe_mtu(self, options):
        if "mtu" in options and options["mtu"].value >= 23:
            self.notification_limit = min(64, options["mtu"].value - 3)

    async def interface(self, path, name):
        info = await self.bus.introspect("org.bluez", path)
        return self.bus.get_proxy_object("org.bluez", path, info).get_interface(name)

    async def start(self):
        self.bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        self.objects = await self.interface("/", "org.freedesktop.DBus.ObjectManager")
        self.adapter = await self.interface(self.path, "org.bluez.Adapter1")
        if not await self.adapter.get_powered():
            try:
                await self.adapter.set_powered(True)
            except DBusError as error:
                raise RuntimeError(f"Could not power on {self.path.rsplit('/', 1)[-1]} "
                                   f"({error.type}: {error.text}). Check rfkill and bluetoothctl power on.") from error
        self.address = await self.adapter.get_address()
        self.agent_manager = await self.interface("/org/bluez", "org.bluez.AgentManager1")
        self.bus.export(ROOT + "/agent", Agent(self))
        await self.agent_manager.call_register_agent(ROOT + "/agent", "DisplayYesNo")

    async def advertise(self, uart):
        async with self.ad_lock:
            await self.set_advertisement(uart)

    async def set_advertisement(self, uart):
        name = "Nano Bluetooth Link" if uart else "NM-" + self.address.replace(":", "")
        if self.advertisement and self.advertisement.local_name == name:
            return
        ads = await self.interface(self.path, "org.bluez.LEAdvertisingManager1")
        if uart and not self.uart_registered:
            gatt = await self.interface(self.path, "org.bluez.GattManager1")
            self.bus.export(SERVICE, UARTService())
            self.bus.export(SERVICE + "/tx", self.tx)
            self.bus.export(SERVICE + "/read", self.audio_read)
            self.bus.export(SERVICE + "/rx", self.rx)
            try:
                await gatt.call_register_application(GATT_ROOT, {})
            except Exception:
                for suffix in ("", "/tx", "/read", "/rx"):
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

    async def scan(self, target=None):
        if self.scan_stop:
            raise RuntimeError("A Bluetooth search is already running.")
        self.scan_stop = stop = asyncio.Event()
        try:
            await self.adapter.call_set_discovery_filter({"Transport": Variant("s", "bredr")})
            await self.adapter.call_start_discovery()
            try:
                if target is None:
                    try:
                        await asyncio.wait_for(stop.wait(), 8)
                    except TimeoutError:
                        pass
                    return
                deadline = asyncio.get_running_loop().time() + 8
                while not stop.is_set() and asyncio.get_running_loop().time() < deadline:
                    if any(device["path"] == target for device in await self.devices()):
                        return
                    await asyncio.sleep(.2)
            finally:
                await self.adapter.call_stop_discovery()
        finally:
            self.scan_stop = None

    async def stop_scan(self):
        if self.scan_stop:
            self.scan_stop.set()

    async def stop_pair(self):
        if self.pair_task:
            self.pair_task.cancel()

    async def pair(self, address, *, replace_bond=False):
        if self.pair_task:
            raise RuntimeError("Pairing is already in progress.")
        path = self.path + "/dev_" + address.replace(":", "_")
        self.pair_task = asyncio.current_task()
        self.pairing_device = path
        try:
            existing = next((device for device in await self.devices() if device["path"] == path), None)
            if replace_bond and existing and existing["paired"]:
                await self.adapter.call_remove_device(path)
                existing = None
            if existing is None:
                await self.scan(path)
            try:
                device = await self.interface(path, "org.bluez.Device1")
            except (DBusError, InterfaceNotFoundError) as error:
                connect_device = getattr(self.adapter, "call_connect_device", None)
                if connect_device is None:
                    raise RuntimeError("The Pi could not discover the Nano. Keep Bluetooth settings open on "
                                       "the Nano, then try Find Devices again.") from error
                path = await asyncio.wait_for(connect_device({"Address": Variant("s", address)}), 12)
                self.pairing_device = path
                device = await self.interface(path, "org.bluez.Device1")
            if not await device.get_paired():
                try:
                    await asyncio.wait_for(device.call_pair(), 60)
                except (TimeoutError, asyncio.CancelledError) as error:
                    try:
                        await device.call_cancel_pairing()
                    except DBusError:
                        pass
                    if isinstance(error, TimeoutError):
                        raise RuntimeError("Pairing timed out.") from error
                    raise
        except asyncio.CancelledError as error:
            raise RuntimeError("Pairing cancelled.") from error
        finally:
            self.pairing_device = None
            self.pair_task = None

    async def confirm(self, value):
        if not self.confirmation or self.confirmation.done():
            raise RuntimeError("No pairing code is waiting for confirmation.")
        self.confirmation.set_result(value)

    async def ble_audio(self, frames, packed, hash_value, quality, autoplay, mode, cancelled, progress):
        if not self.tx.notifying:
            raise RuntimeError("Connect from BLE Chat or Bluetooth Test first.")
        fast = mode == "fast"
        start = b"\0" + bytes([70 if fast else 83 if autoplay else 65 if quality == 1 else 81])
        start += struct.pack("<II", frames, hash_value)
        if fast or autoplay or quality != 1:
            start += bytes([quality])
        if fast:
            start += bytes([autoplay])
        step = 15 if mode == "read" else self.notification_limit - (2 if fast else 6)
        audio_read = self.audio_read

        def records():
            yield start
            for i, offset in enumerate(range(0, len(packed), step)):
                prefix = bytes([0, 0x80 | (i & 127)]) if fast else b"\0D" + struct.pack("<I", offset)
                yield prefix + packed[offset:offset + step]

        try:
            if mode == "read":
                if not audio_read.read_at:
                    raise RuntimeError("Wait for the Nano to start GATT reads.")
                audio_read.records = records()
                audio_read.total = 1 + (len(packed) + step - 1) // step
                audio_read.sent = 0
                audio_read.read_at = asyncio.get_running_loop().time()
                while audio_read.records is not None:
                    cancelled()
                    if not self.tx.notifying or asyncio.get_running_loop().time() - audio_read.read_at > 15:
                        raise RuntimeError("The Nano stopped reading audio.")
                    progress(audio_read.sent * 100 // audio_read.total)
                    await asyncio.sleep(.05)
            else:
                total = 1 + (len(packed) + step - 1) // step
                for i, packet in enumerate(records(), 1):
                    cancelled()
                    self.tx.notify(packet)
                    progress(i * 100 // total)
                    await asyncio.sleep(.04)
        except InterruptedError:
            if mode == "read" and self.tx.notifying:
                audio_read.records = iter((b"\0C",))
                audio_read.total = audio_read.sent + 1
                deadline = asyncio.get_running_loop().time() + 1
                while audio_read.records and asyncio.get_running_loop().time() < deadline:
                    await asyncio.sleep(.05)
            elif self.tx.notifying:
                self.tx.notify(b"\0C")
            raise
        finally:
            audio_read.records = None
