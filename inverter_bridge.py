import socket, threading, struct, time, json, os, signal, sys, math
from datetime import datetime
from collections import deque
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs
import paho.mqtt.client as mqtt

try:
  import serial
except ImportError:
  serial = None

BRIDGE_VERSION = "42.0.0"
CONFIG_FILE = "/root/bridge_config.json"
EVENT_LOG_FILE = "/root/bridge_events.log"
PASSWORD_MASK = "********"

event_history = deque(maxlen=60)
event_lock = threading.Lock()
mqtt_op_lock = threading.Lock()

def log_event(source: str, msg: str):
  ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
  entry = f"[{ts}] [{source}] {msg}"
  print(entry)
  with event_lock:
    event_history.appendleft(entry)
    try:
      with open(EVENT_LOG_FILE, "a") as f:
        f.write(entry + "\n")
    except Exception:
      pass

def load_initial_events():
  if os.path.exists(EVENT_LOG_FILE):
    try:
      with open(EVENT_LOG_FILE, "r") as f:
        for line in [l.strip() for l in f.readlines() if l.strip()][-60:]:
          event_history.appendleft(line)
    except Exception:
      pass

load_initial_events()

DEFAULT_CONFIG = {
  "mqtt_host": "192.168.0.100",
  "mqtt_port": 1883,
  "mqtt_user": "",
  "mqtt_pass": "",
  "web_port": 8080,
  "max_battery_charge_amps": 120.0,
  "max_battery_discharge_amps": 120.0,
  "indicators": {
    "show_grid_bar": True,
    "show_solar_bar": True,
    "show_battery_bar": True,
    "show_load_bar": True
  },
  "devices": [
    {
      "id": "anenji_1",
      "name": "Anenji 6200W (Sumry Modbus RTU)",
      "short_name": "Anenji",
      "role": "main",
      "type": "anenji_tcp",
      "listen_port": 18899,
      "rated_va": 6200,
      "rated_watt": 6200,
      "installed_pv_watt": 5200,
      "max_grid_amps": 32.0,
      "max_pv_volt": 450.0,
      "max_bus_volt": 450.0,
      "poll_interval": 1.5,
      "energy_file": "/root/inverter_energy.json"
    },
    {
      "id": "easun_1",
      "name": "Easun Inverter (Voltronic PI30)",
      "short_name": "Easun",
      "role": "island",
      "type": "easun_serial",
      "serial_port": "/dev/easun-rs485",
      "baud_rate": 2400,
      "rated_va": 5500,
      "rated_watt": 5500,
      "installed_pv_watt": 5500,
      "max_pv_volt": 450.0,
      "max_bus_volt": 450.0,
      "poll_interval": 1.5,
      "energy_file": "/root/easun_energy.json"
    }
  ]
}

mqtt_status = {"connected": False, "rc_msg": "Connecting..."}
mqtt_client = None

RC_MAP = {
  0: "CONNECTED (OK)", 1: "ERROR: Incorrect protocol version", 2: "ERROR: Invalid client identifier",
  3: "ERROR: Server unavailable", 4: "AUTH FAILED: Bad username or password!", 5: "AUTH FAILED: Not authorized!",
  128: "ERROR: Unspecified error", 134: "AUTH FAILED: Bad username or password!", 135: "AUTH FAILED: Not authorized!"
}

def _migrate_pv_offset_into_energy_file(energy_path: str, offset_kwh: float):
  if abs(offset_kwh) < 1e-9 or not energy_path:
    return
  data = {"total_pv_kwh": 0.0, "total_grid_input_kwh": 0.0, "total_load_kwh": 0.0, "total_battery_charge_kwh": 0.0, "total_battery_discharge_kwh": 0.0}
  if os.path.exists(energy_path):
    try:
      with open(energy_path, "r") as f:
        data.update(json.load(f))
    except Exception as e:
      log_event("SYSTEM", f"Cannot read {energy_path} for offset migration: {e}")
      return
  data["total_pv_kwh"] = round(float(data.get("total_pv_kwh", 0.0)) + offset_kwh, 6)
  try:
    with open(energy_path + ".tmp", "w") as f:
      json.dump(data, f, indent=2)
    os.replace(energy_path + ".tmp", energy_path)
    log_event("SYSTEM", f"Migrated pv_kwh_offset ({offset_kwh} kWh) into {energy_path}")
  except Exception as e:
    log_event("SYSTEM", f"Failed writing migrated offset to {energy_path}: {e}")

def load_config():
  if os.path.exists(CONFIG_FILE):
    try:
      with open(CONFIG_FILE, "r") as f:
        cfg = json.load(f)
      legacy_hv = float(cfg.pop("high_volt_red_threshold", 450.0))
      legacy_ba = float(cfg.pop("max_battery_amps", 120.0))
      cfg.setdefault("max_battery_charge_amps", legacy_ba)
      cfg.setdefault("max_battery_discharge_amps", legacy_ba)
      ind_def = DEFAULT_CONFIG["indicators"]
      if not isinstance(cfg.get("indicators"), dict):
        cfg["indicators"] = dict(ind_def)
      else:
        cfg["indicators"].pop("solar_bar_style", None)
        for k, v in ind_def.items():
          cfg["indicators"].setdefault(k, v)
      existing_ids = [d.get("id") for d in cfg.get("devices", [])]
      if "easun_1" not in existing_ids:
        cfg.setdefault("devices", []).append(json.loads(json.dumps(DEFAULT_CONFIG["devices"][1])))
      for idx, d in enumerate(cfg.get("devices", [])):
        is_main = (idx == 0 or d.get("type") == "anenji_tcp")
        d.setdefault("role", "main" if is_main else "island")
        if "short_name" not in d:
          d["short_name"] = "Anenji" if "anenji" in d.get("id", "").lower() else ("Easun" if "easun" in d.get("id", "").lower() else d.get("id", "Inv"))
        def_p = 6200 if d["role"] == "main" else 5500
        def_pv = 5200 if d["role"] == "main" else 5500
        d.setdefault("rated_watt", def_p)
        d.setdefault("rated_va", d.get("rated_watt", def_p))
        if d.get("id") == "anenji_1" and d.get("installed_pv_watt") == 6200:
          d["installed_pv_watt"] = 5200
        d.setdefault("installed_pv_watt", def_pv)
        d.setdefault("max_pv_volt", legacy_hv)
        d.setdefault("max_bus_volt", legacy_hv)
        if d["role"] == "main":
          d.setdefault("max_grid_amps", 32.0)
        if "pv_kwh_offset" in d:
          off_val = float(d.pop("pv_kwh_offset") or 0.0)
          _migrate_pv_offset_into_energy_file(d.get("energy_file", ""), off_val)
      save_config(cfg)
      return cfg
    except Exception as e:
      log_event("SYSTEM", f"Config load error: {e}")
  save_config(DEFAULT_CONFIG)
  return json.loads(json.dumps(DEFAULT_CONFIG))

def save_config(cfg):
  with open(CONFIG_FILE + ".tmp", "w") as f:
    json.dump(cfg, f, indent=2)
  os.replace(CONFIG_FILE + ".tmp", CONFIG_FILE)

ANJ_STATUS_MAP = {0: "Power On", 1: "Standby", 2: "Line Mode (On-Grid)", 3: "Off-Grid (Battery)", 4: "Bypass", 5: "Charging Mode", 6: "Fault"}
ANJ_OUT_MODE_MAP = {0: "Utility First (UTI)", 1: "Solar First (SOL)", 2: "SBU (Solar-Batt-Util)", 3: "SUB (Solar-Util-Batt)", 4: "SUF (GRID Feedback)"}
ANJ_OUT_MODE_REV = {v: k for k, v in ANJ_OUT_MODE_MAP.items()}
ANJ_AC_RANGE_MAP = {0: "Appliances (APL)", 1: "UPS (UPS)", 2: "Generator (GEN)"}
ANJ_AC_RANGE_REV = {v: k for k, v in ANJ_AC_RANGE_MAP.items()}
ANJ_BUZZER_MAP = {0: "Mute (nd1)", 1: "Source/Warn/Fault (nd2)", 2: "Warn/Fault (nd3)", 3: "Fault Only (nd4)"}
ANJ_BUZZER_REV = {v: k for k, v in ANJ_BUZZER_MAP.items()}
ANJ_CHG_PRIO_MAP = {1: "Solar First (CSO)", 2: "Solar + Utility (SNU)", 3: "Solar Only (OSO)"}
ANJ_CHG_PRIO_REV = {v: k for k, v in ANJ_CHG_PRIO_MAP.items()}
ANJ_BATT_TYPE_MAP = {0: "AGN", 1: "FLD", 2: "USR", 4: "LI2", 6: "LI4", 8: "LIb"}
ANJ_BATT_TYPE_REV = {v: k for k, v in ANJ_BATT_TYPE_MAP.items()}
ANJ_OUT_VOLT_MAP = {2200: "220V", 2300: "230V", 2400: "240V"}
ANJ_OUT_VOLT_REV = {v: k for k, v in ANJ_OUT_VOLT_MAP.items()}

def modbus_crc(data: bytes) -> bytes:
  crc = 0xFFFF
  for pos in data:
    crc ^= pos
    for _ in range(8):
      crc = ((crc >> 1) ^ 0xA001) if (crc & 1) else (crc >> 1)
  return struct.pack('<H', crc)

def to_signed(val: int) -> int:
  return val - 65536 if val >= 32768 else val

def build_read_packet(start: int, count: int) -> bytes:
  payload = struct.pack('>BBHH', 1, 3, start, count)
  return payload + modbus_crc(payload)

def build_write_packet(reg: int, value: int) -> bytes:
  payload = struct.pack('>BBHHB', 1, 16, reg, 1, 2) + struct.pack('>H', value)
  return payload + modbus_crc(payload)

def recv_exact_modbus(conn: socket.socket, expected_regs: int, timeout: float = 2.0):
  expected_bytes = 5 + (expected_regs * 2)
  conn.settimeout(timeout)
  buf, deadline = b'', time.time() + timeout
  while len(buf) < expected_bytes and time.time() < deadline:
    try:
      chunk = conn.recv(expected_bytes - len(buf))
      if not chunk: return None
      buf += chunk
    except Exception:
      return None
  if len(buf) != expected_bytes or buf[0] != 1 or buf[1] != 3 or buf[2] != expected_regs * 2:
    return None
  if modbus_crc(buf[:-2]) != buf[-2:]:
    return None
  return [x[0] for x in struct.iter_unpack('>H', buf[3:-2])]

def flush_socket(conn: socket.socket):
  try:
    conn.settimeout(0.02)
    while conn.recv(1024): pass
  except Exception:
    pass

EASUN_MODE_MAP = {'P': "Power On", 'S': "Standby", 'L': "Line Mode (On-Grid)", 'B': "Off-Grid (Battery)", 'F': "Fault", 'H': "Power Saving", 'C': "Charging Mode", 'Y': "Bypass", 'D': "Shutdown"}
EASUN_OUT_MODE_MAP = {0: "Utility First (UTI)", 1: "Solar First (SOL)", 2: "SBU (Solar-Batt-Util)"}
EASUN_OUT_MODE_REV = {v: k for k, v in EASUN_OUT_MODE_MAP.items()}
EASUN_AC_RANGE_MAP = {0: "Appliances (APL)", 1: "UPS (UPS)"}
EASUN_AC_RANGE_REV = {v: k for k, v in EASUN_AC_RANGE_MAP.items()}
EASUN_CHG_PRIO_MAP = {0: "Utility First (UTI)", 1: "Solar First (CSO)", 2: "Solar + Utility (SNU)", 3: "Solar Only (OSO)"}
EASUN_CHG_PRIO_REV = {v: k for k, v in EASUN_CHG_PRIO_MAP.items()}
EASUN_BATT_TYPE_MAP = {0: "AGN", 1: "FLD", 2: "USR"}
EASUN_BATT_TYPE_REV = {v: k for k, v in EASUN_BATT_TYPE_MAP.items()}
EASUN_OUT_VOLT_MAP = {220: "220V", 230: "230V", 240: "240V"}
EASUN_OUT_VOLT_REV = {"220V": "220", "230V": "230", "240V": "240"}
_CRC_TABLE = [0x0000, 0x1021, 0x2042, 0x3063, 0x4084, 0x50a5, 0x60c6, 0x70e7, 0x8108, 0x9129, 0xa14a, 0xb16b, 0xc18c, 0xd1ad, 0xe1ce, 0xf1ef]

def axpert_crc(cmd: str) -> bytes:
  crc = 0
  for byte in cmd.encode('ascii'):
    da = (crc >> 8) >> 4
    crc = ((crc << 4) & 0xFFFF) ^ _CRC_TABLE[da ^ (byte >> 4)]
    da = (crc >> 8) >> 4
    crc = ((crc << 4) & 0xFFFF) ^ _CRC_TABLE[da ^ (byte & 0x0F)]
  hi, lo = (crc >> 8) & 0xFF, crc & 0xFF
  if hi in (0x28, 0x0D, 0x0A): hi += 1
  if lo in (0x28, 0x0D, 0x0A): lo += 1
  return bytes([hi, lo])

def build_axpert_packet(cmd: str) -> bytes:
  return cmd.encode('ascii') + axpert_crc(cmd) + b'\r'

class BaseInverterDevice:
  def __init__(self, dev_cfg, mqtt_c):
    self.cfg = dev_cfg
    self.id = dev_cfg["id"]
    self.name = dev_cfg["name"]
    self.role = dev_cfg.get("role", "island")
    self.short_name = dev_cfg.get("short_name", self.id)
    self.mqtt = mqtt_c
    self.energy_lock = threading.Lock()
    self.online = False
    self.settings_synced = False
    self.diag_msg = "Initializing..."
    self.state = {}
    self.energy = self.load_energy()
    self.update_energy_state_keys()

  def load_energy(self):
    default = {"total_pv_kwh": 0.0, "total_grid_input_kwh": 0.0, "total_load_kwh": 0.0, "total_battery_charge_kwh": 0.0, "total_battery_discharge_kwh": 0.0}
    path = self.cfg["energy_file"]
    if os.path.exists(path):
      try:
        with open(path, 'r') as f:
          data = json.load(f)
          for k in default: data.setdefault(k, default[k])
          return data
      except Exception as e:
        log_event(self.id, f"Error loading energy file {path}: {e}")
    return default.copy()

  def update_energy_state_keys(self):
    self.state.update({
      "total_pv_energy_kwh": round(self.energy["total_pv_kwh"], 4),
      "total_grid_input_kwh": round(self.energy["total_grid_input_kwh"], 4),
      "total_load_kwh": round(self.energy["total_load_kwh"], 4),
      "total_battery_charge_kwh": round(self.energy["total_battery_charge_kwh"], 4),
      "total_battery_discharge_kwh": round(self.energy["total_battery_discharge_kwh"], 4),
    })

  def save_energy(self):
    path = self.cfg["energy_file"]
    with self.energy_lock:
      try:
        with open(path + ".tmp", 'w') as f:
          json.dump(self.energy, f, indent=2)
        os.replace(path + ".tmp", path)
      except Exception as e:
        log_event(self.id, f"Energy save failed: {e}")

  def publish_availability(self, is_online: bool, reason: str = ""):
    was_online = self.online
    self.online = is_online
    if not is_online:
      self.settings_synced = False
      if was_online: log_event(self.id, f"DISCONNECTED ({reason})")
    elif not was_online:
      log_event(self.id, f"ONLINE ({reason})")
    self.mqtt.publish(f"solar/{self.id}/status", "online" if is_online else "offline", retain=True)
    self.mqtt.publish(f"solar/{self.id}/state", json.dumps(self.state), retain=True)

  def update_live_behavior_status(self):
    st = self.state
    grid_v = float(st.get("grid_volt") or 0.0)
    grid_f = float(st.get("grid_freq") or 0.0)
    pv_v = float(st.get("pv_input_volt") or 0.0)
    pv_w = float(st.get("pv_input_watt") or 0.0)
    bus_v = float(st.get("bus_volt") or 0.0)
    out_v = float(st.get("ac_out_volt") or 0.0)
    out_f = float(st.get("ac_out_freq") or 0.0)
    load_pct = float(st.get("ac_load_pct") or 0.0)
    batt_v = float(st.get("batt_volt") or 0.0)
    batt_soc = int(float(st.get("batt_soc") or 0))
    temp_inv = int(float(st.get("temp_inv") or 0))
    temp_dc = int(float(st.get("temp_dc") or 0))
    max_temp = max(temp_inv, temp_dc)

    max_pv_v = float(self.cfg.get("max_pv_volt", 450.0))
    max_bus_v = float(self.cfg.get("max_bus_volt", 450.0))
    cutoff_v = float(st.get("low_dc_cutoff_volt") or 44.0)
    bulk_v = float(st.get("bulk_charge_volt") or 56.4)

    faults, serious_warns, info_warns = [], [], []

    if st.get("device_status_msg") == "Fault":
      faults.append("Inverter Hardware Fault Mode")

    if grid_v < 50.0:
      info_warns.append("No Grid Voltage")
    elif grid_v < 170.0:
      serious_warns.append(f"Grid Under-Voltage ({grid_v:.0f}V)")
    elif grid_v > 270.0:
      serious_warns.append(f"Grid Over-Voltage ({grid_v:.0f}V)")
    if grid_v >= 100.0 and not (47.0 <= grid_f <= 53.0):
      serious_warns.append(f"Grid Freq Out of Range ({grid_f:.1f}Hz)")

    if pv_v < 30.0:
      info_warns.append("PV Disconnected (<30V)")
    elif pv_v < 120.0:
      info_warns.append(f"PV Below MPPT Range ({pv_v:.0f}V < 120V)")
    elif pv_v >= max_pv_v:
      faults.append(f"PV Over-Voltage ({pv_v:.0f}V >= {max_pv_v:.0f}V)")
    elif pv_v >= (max_pv_v - 15.0):
      serious_warns.append(f"PV Voltage High ({pv_v:.0f}V / {max_pv_v:.0f}V)")
    elif pv_w <= 12:
      info_warns.append(f"PV Idle ({pv_v:.0f}V, 0W)")

    if bus_v >= max_bus_v:
      faults.append(f"DC Bus Over-Voltage ({bus_v:.0f}V >= {max_bus_v:.0f}V)")
    elif bus_v >= (max_bus_v - 15.0):
      serious_warns.append(f"DC Bus Voltage High ({bus_v:.0f}V / {max_bus_v:.0f}V)")
    elif out_v >= 180.0 and 0 < bus_v < 300.0:
      serious_warns.append(f"DC Bus Under-Voltage ({bus_v:.0f}V < 300V)")

    if out_v < 180.0:
      info_warns.append("AC Output Off (<180V)")
    elif out_v > 260.0:
      faults.append(f"AC Output Over-Voltage ({out_v:.0f}V)")
    elif out_v < 195.0 or out_v > 253.0:
      serious_warns.append(f"AC Output Voltage Abnormal ({out_v:.0f}V)")
    if out_v >= 180.0 and not (47.5 <= out_f <= 52.5):
      serious_warns.append(f"AC Output Freq Abnormal ({out_f:.1f}Hz)")

    if load_pct >= 105.0:
      faults.append(f"AC Overload ({load_pct:.0f}%)")
    elif load_pct >= 90.0:
      serious_warns.append(f"High AC Load ({load_pct:.0f}%)")

    if 0 < batt_v < 20.0:
      faults.append("Battery Disconnected (<20V)")
    elif 20.0 <= batt_v <= (cutoff_v + 1.0):
      serious_warns.append(f"Battery Voltage Low ({batt_v:.1f}V)")
    elif batt_v >= (bulk_v + 2.5):
      faults.append(f"Battery Over-Voltage ({batt_v:.1f}V)")
    elif batt_v >= (bulk_v + 1.2):
      serious_warns.append(f"Battery Voltage High ({batt_v:.1f}V)")

    if "soc_cutoff" in st and 0 < batt_soc <= int(st.get("soc_cutoff") or 5) + 3:
      serious_warns.append(f"Battery SOC Low ({batt_soc}%)")

    if max_temp >= 80:
      faults.append(f"Over-Temperature ({max_temp}°C)")
    elif max_temp >= 68:
      serious_warns.append(f"High Temperature ({max_temp}°C)")

    all_warns = serious_warns + info_warns
    st["has_fault"] = len(faults) > 0
    st["serious_warn"] = len(serious_warns) > 0
    st["fault_msg"] = ", ".join(faults) if faults else "No Fault"
    st["warning_msg"] = ", ".join(all_warns) if all_warns else "No Warning"

  def clear_old_discovery_topics(self):
    old_topics = [
      ("sensor", "switch_status"), ("sensor", "load_status"), ("number", "reg_327_volt"), ("number", "reg_328_volt"),
      ("number", "recharge_volt"), ("number", "redischarge_volt"), ("number", "cutoff_volt"),
      ("number", "max_charge_amps"), ("number", "max_ac_amps"), ("select", "output_priority_str"),
      ("sensor", "grid_v"), ("sensor", "grid_f"), ("sensor", "ac_out_v"), ("sensor", "ac_out_f"),
      ("sensor", "ac_out_w"), ("sensor", "ac_out_va"), ("sensor", "load_pct"), ("sensor", "bus_v"),
      ("sensor", "batt_v"), ("sensor", "batt_c"), ("sensor", "batt_w"), ("sensor", "pv_v"),
      ("sensor", "pv_c"), ("sensor", "pv_w"), ("sensor", "total_pv_kwh"),
      ("sensor", "total_batt_charge_kwh"), ("sensor", "total_batt_discharge_kwh")
    ]
    for comp, key in old_topics:
      self.mqtt.publish(f"homeassistant/{comp}/{self.id}/{key}/config", "", retain=True)

class AnenjiDevice(BaseInverterDevice):
  def __init__(self, dev_cfg, mqtt_c):
    super().__init__(dev_cfg, mqtt_c)
    self.conn = None
    self.lock = threading.Lock()
    self.confirmed_regs = {}
    self.last_write_time = {}
    self.last_any_write = 0

  def publish_discovery(self):
    self.clear_old_discovery_topics()
    dev_info = {"identifiers": [f"solar_bridge_{self.id}"], "name": self.cfg["name"], "manufacturer": "Anenji (Sumry Protocol)", "model": "ANJ-6200W-48V (Sumry Modbus RTU)", "sw_version": f"v{BRIDGE_VERSION}"}
    avail_topic, state_topic = f"solar/{self.id}/status", f"solar/{self.id}/state"
    sensors = [
      ("device_status_msg", "Status", None, None, None, "mdi:information-outline"),
      ("fault_msg", "Fault Message", None, None, None, "mdi:alert-circle"),
      ("warning_msg", "Warning Message", None, None, None, "mdi:alert"),
      ("grid_volt", "Grid Voltage", "V", "voltage", "measurement", None),
      ("grid_freq", "Grid Frequency", "Hz", "frequency", "measurement", None),
      ("grid_power_watt", "Grid Input Power", "W", "power", "measurement", None),
      ("grid_current", "Grid Current", "A", "current", "measurement", None),
      ("ac_out_volt", "Output Voltage", "V", "voltage", "measurement", None),
      ("ac_out_freq", "Output Frequency", "Hz", "frequency", "measurement", None),
      ("ac_out_amp", "Output Current", "A", "current", "measurement", None),
      ("ac_load_real_watt", "House Load Power", "W", "power", "measurement", None),
      ("ac_load_va", "House Load Apparent Power", "VA", "apparent_power", "measurement", None),
      ("ac_load_pct", "Inverter Load Percentage", "%", None, "measurement", "mdi:percent"),
      ("bus_volt", "DC Bus Voltage", "V", "voltage", "measurement", None),
      ("batt_volt", "Battery Voltage (Inverter)", "V", "voltage", "measurement", None),
      ("batt_soc", "Battery Percentage", "%", "battery", "measurement", None),
      ("batt_current", "Battery Current", "A", "current", "measurement", None),
      ("batt_power_watt", "Battery Power Flow", "W", "power", "measurement", None),
      ("pv_input_volt", "PV Input Voltage", "V", "voltage", "measurement", None),
      ("pv_current", "PV Current", "A", "current", "measurement", None),
      ("pv_input_watt", "PV Input Power", "W", "power", "measurement", None),
      ("pv_charging_watt", "PV Charging Power", "W", "power", "measurement", None),
      ("temp_inv", "Inverter Temperature", "°C", "temperature", "measurement", None),
      ("temp_dc", "Inverter Temp (DC)", "°C", "temperature", "measurement", None),
      ("total_pv_energy_kwh", "Solar Energy", "kWh", "energy", "total_increasing", "mdi:solar-power"),
      ("total_grid_input_kwh", "Grid Import Energy", "kWh", "energy", "total_increasing", "mdi:transmission-tower"),
      ("total_load_kwh", "House Load Energy", "kWh", "energy", "total_increasing", "mdi:home-lightning-bolt"),
      ("total_battery_charge_kwh", "Battery Charge Energy", "kWh", "energy", "total_increasing", "mdi:battery-charging"),
      ("total_battery_discharge_kwh", "Battery Discharge Energy", "kWh", "energy", "total_increasing", "mdi:battery-minus"),
    ]
    for key, name, unit, d_class, s_class, icon in sensors:
      payload = {"name": name, "unique_id": f"{self.id}_{key}", "state_topic": state_topic, "value_template": f"{{{{ value_json.{key} }}}}", "device": dev_info}
      if not key.endswith("_kwh"): payload["availability_topic"] = avail_topic
      if unit: payload["unit_of_measurement"] = unit
      if d_class: payload["device_class"] = d_class
      if s_class: payload["state_class"] = s_class
      if icon: payload["icon"] = icon
      self.mqtt.publish(f"homeassistant/sensor/{self.id}/{key}/config", json.dumps(payload), retain=True)

    selects = [
      ("output_mode_str", "Output Source Priority", list(ANJ_OUT_MODE_MAP.values()), "mdi:source-branch"),
      ("charger_priority_str", "Charger Source Priority", list(ANJ_CHG_PRIO_MAP.values()), "mdi:battery-charging"),
      ("ac_input_range_str", "AC Input Range", list(ANJ_AC_RANGE_MAP.values()), "mdi:sine-wave"),
      ("battery_type_str", "Battery Type", list(ANJ_BATT_TYPE_MAP.values()), "mdi:battery-sync"),
      ("rated_out_volt_str", "Rated Output Voltage", list(ANJ_OUT_VOLT_MAP.values()), "mdi:sine-wave"),
      ("buzzer_mode_str", "Buzzer Mode", list(ANJ_BUZZER_MAP.values()), "mdi:volume-high"),
    ]
    for key, name, options, icon in selects:
      payload = {"name": name, "unique_id": f"{self.id}_{key}", "state_topic": state_topic, "value_template": f"{{{{ value_json.{key} }}}}", "command_topic": f"solar/{self.id}/set/{key}", "availability_topic": avail_topic, "options": options, "icon": icon, "device": dev_info}
      self.mqtt.publish(f"homeassistant/select/{self.id}/{key}/config", json.dumps(payload), retain=True)

    numbers = [
      ("bulk_charge_volt", "Bulk Charging Voltage", 48.0, 58.4, 0.1, "V", "mdi:battery-arrow-up"),
      ("float_charge_volt", "Float Charging Voltage", 48.0, 58.4, 0.1, "V", "mdi:battery-check"),
      ("back_to_grid_volt", "Back to Grid Voltage", 0.0, 58.0, 0.1, "V", "mdi:battery-arrow-down"),
      ("back_to_batt_volt", "Back to Battery Voltage", 0.0, 58.0, 0.1, "V", "mdi:battery-arrow-up"),
      ("low_dc_cutoff_volt", "Low DC Cut-off Voltage", 40.0, 54.0, 0.1, "V", "mdi:battery-alert"),
      ("max_total_amps", "Max Charging Current (Total)", 10, 120, 1, "A", "mdi:battery-charging-high"),
      ("max_ac_amps", "Max AC Charge Amps", 2, 80, 1, "A", "mdi:current-ac"),
      ("soc_back_to_grid", "Back to Grid SOC (Prg 43)", 4, 50, 1, "%", "mdi:battery-arrow-down"),
      ("soc_back_to_batt", "Back to Battery SOC (Prg 44)", 60, 100, 1, "%", "mdi:battery-arrow-up"),
      ("soc_cutoff", "Cut-off SOC (Prg 45)", 3, 30, 1, "%", "mdi:battery-alert"),
    ]
    for key, name, mn, mx, step, unit, icon in numbers:
      payload = {"name": name, "unique_id": f"{self.id}_{key}", "state_topic": state_topic, "value_template": f"{{{{ value_json.{key} }}}}", "command_topic": f"solar/{self.id}/set/{key}", "availability_topic": avail_topic, "min": mn, "max": mx, "step": step, "unit_of_measurement": unit, "mode": "box", "icon": icon, "device": dev_info}
      self.mqtt.publish(f"homeassistant/number/{self.id}/{key}/config", json.dumps(payload), retain=True)

    switches = [("backlight_status", "LCD Backlight", "mdi:monitor-shimmer"), ("return_to_default", "Return to Default Screen", "mdi:arrow-u-left-top"), ("grid_charging", "Grid Charging", "mdi:flash")]
    for key, name, icon in switches:
      vt = "{{ 'ON' if value_json.charger_priority_str == 'Solar + Utility (SNU)' else 'OFF' }}" if key == "grid_charging" else f"{{{{ 'ON' if value_json.{key} == 1 else 'OFF' }}}}"
      payload = {"name": name, "unique_id": f"{self.id}_{key}", "state_topic": state_topic, "value_template": vt, "command_topic": f"solar/{self.id}/set/{key}", "availability_topic": avail_topic, "payload_on": "ON", "payload_off": "OFF", "icon": icon, "device": dev_info}
      self.mqtt.publish(f"homeassistant/switch/{self.id}/{key}/config", json.dumps(payload), retain=True)

  def write_register_safe(self, reg: int, raw_val: int):
    if not self.online or not self.conn or not self.settings_synced:
      log_event(self.id, f"Blocked write Reg {reg}={raw_val} (Offline/Unsynced)")
      return False
    if self.confirmed_regs.get(reg) == raw_val: return False
    now = time.time()
    if (now - self.last_write_time.get(reg, 0)) < 5.0:
      log_event(self.id, f"Rate-limited write Reg {reg}={raw_val}")
      return False
    with self.lock:
      try:
        flush_socket(self.conn)
        self.conn.send(build_write_packet(reg, raw_val))
        time.sleep(0.25)
        ack = self.conn.recv(32)
        if ack and len(ack) >= 8 and ack[1] == 16:
          self.confirmed_regs[reg] = raw_val
          self.last_write_time[reg] = now
          self.last_any_write = now
          log_event(self.id, f"WRITE OK: Reg {reg} -> {raw_val}")
          return True
        log_event(self.id, f"WRITE NAK/TIMEOUT: Reg {reg} -> {raw_val}")
        return False
      except Exception as e:
        log_event(self.id, f"WRITE ERROR: Reg {reg} -> {e}")
        return False

  def handle_mqtt_command(self, prop: str, payload: str):
    try:
      if prop == "grid_charging":
        target_val = 2 if payload == "ON" else 3
        target_str = "Solar + Utility (SNU)" if payload == "ON" else "Solar Only (OSO)"
        if self.write_register_safe(331, target_val): self.state["charger_priority_str"] = target_str
      elif prop == "output_mode_str" and payload in ANJ_OUT_MODE_REV:
        if self.write_register_safe(301, ANJ_OUT_MODE_REV[payload]): self.state[prop] = payload
      elif prop == "ac_input_range_str" and payload in ANJ_AC_RANGE_REV:
        if self.write_register_safe(302, ANJ_AC_RANGE_REV[payload]): self.state[prop] = payload
      elif prop == "buzzer_mode_str" and payload in ANJ_BUZZER_REV:
        if self.write_register_safe(303, ANJ_BUZZER_REV[payload]): self.state[prop] = payload
      elif prop == "charger_priority_str" and payload in ANJ_CHG_PRIO_REV:
        if self.write_register_safe(331, ANJ_CHG_PRIO_REV[payload]): self.state[prop] = payload
      elif prop == "battery_type_str" and payload in ANJ_BATT_TYPE_REV:
        if self.write_register_safe(322, ANJ_BATT_TYPE_REV[payload]): self.state[prop] = payload
      elif prop == "rated_out_volt_str" and payload in ANJ_OUT_VOLT_REV:
        if self.write_register_safe(320, ANJ_OUT_VOLT_REV[payload]): self.state[prop] = payload
      elif prop == "backlight_status":
        v = 1 if payload == "ON" else 0
        if self.write_register_safe(305, v): self.state[prop] = v
      elif prop == "return_to_default":
        v = 1 if payload == "ON" else 0
        if self.write_register_safe(306, v): self.state[prop] = v
      elif prop == "bulk_charge_volt":
        v = round(float(payload), 1)
        if self.write_register_safe(324, int(round(v * 10))): self.state[prop] = v
      elif prop == "float_charge_volt":
        v = round(float(payload), 1)
        if self.write_register_safe(325, int(round(v * 10))): self.state[prop] = v
      elif prop == "back_to_batt_volt":
        v = round(float(payload), 1)
        if self.write_register_safe(326, int(round(v * 10))): self.state[prop] = v
      elif prop == "back_to_grid_volt":
        v = round(float(payload), 1)
        if self.write_register_safe(327, int(round(v * 10))): self.state[prop] = v
      elif prop == "low_dc_cutoff_volt":
        v = round(float(payload), 1)
        if self.write_register_safe(329, int(round(v * 10))): self.state[prop] = v
      elif prop == "max_total_amps":
        v = int(float(payload))
        if self.write_register_safe(332, v * 10): self.state[prop] = v
      elif prop == "max_ac_amps":
        v = int(float(payload))
        if self.write_register_safe(333, v * 10): self.state[prop] = v
      elif prop == "soc_back_to_grid":
        v = int(float(payload))
        if self.write_register_safe(341, v): self.state[prop] = v
      elif prop == "soc_back_to_batt":
        v = int(float(payload))
        if self.write_register_safe(342, v): self.state[prop] = v
      elif prop == "soc_cutoff":
        v = int(float(payload))
        if self.write_register_safe(343, v): self.state[prop] = v
      self.mqtt.publish(f"solar/{self.id}/state", json.dumps(self.state), retain=True)
    except Exception as e:
      log_event(self.id, f"Command error ({prop}={payload}): {e}")

  def run_worker(self):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    srv.bind(('0.0.0.0', self.cfg["listen_port"]))
    srv.listen(2)
    last_valid_sample_time, last_save_time = None, time.time()

    while True:
      disconnect_reason = "Socket closed"
      try:
        self.diag_msg = f"Listening on TCP port {self.cfg['listen_port']}..."
        conn, addr = srv.accept()
        conn.settimeout(5.0)
        conn.send(b'AT+DTUPN?\r\n')
        reply = conn.recv(1024).decode(errors='ignore').strip()
        if not reply:
          log_event(self.id, f"Ignored empty handshake from {addr[0]}:{addr[1]} (secondary port)")
          conn.close()
          continue
        self.diag_msg = f"Connected from {addr[0]}:{addr[1]} (Dongle: {reply})"
        time.sleep(0.4)
        self.conn, failures, loop_cnt = conn, 0, 0

        while True:
          with self.lock:
            flush_socket(conn)
            conn.send(build_read_packet(200, 35))
            vals = recv_exact_modbus(conn, 35, timeout=2.0)
            if vals is None:
              failures += 1
              if failures >= 6:
                disconnect_reason = "6 consecutive Modbus read timeouts (Reg 200)"
                break
            else:
              failures = 0
              now = time.time()
              dt = (now - last_valid_sample_time) if last_valid_sample_time is not None else 0.0
              last_valid_sample_time = now
              self.diag_msg = f"Connected from {addr[0]}:{addr[1]} (Dongle: {reply}, Poll dt={dt:.1f}s)"
              v_grid, p_grid, p_ac_chg = vals[2] / 10.0, to_signed(vals[4]), to_signed(vals[9])
              p_load, va_load, v_batt = to_signed(vals[13]), vals[14], vals[15] / 10.0
              v_pv, i_pv, p_pv, p_pv_btt = vals[19] / 10.0, vals[20] / 10.0, vals[23], to_signed(vals[24])
              batt_current = to_signed(vals[32]) / 10.0
              batt_p = int(batt_current * v_batt)

              if 0.0 < dt <= 60.0:
                with self.energy_lock:
                  if p_pv > 0: self.energy["total_pv_kwh"] += (p_pv * dt) / 3600000.0
                  if p_grid > 0: self.energy["total_grid_input_kwh"] += (p_grid * dt) / 3600000.0
                  if p_load > 0: self.energy["total_load_kwh"] += (p_load * dt) / 3600000.0
                  if batt_p > 0: self.energy["total_battery_charge_kwh"] += (batt_p * dt) / 3600000.0
                  elif batt_p < 0: self.energy["total_battery_discharge_kwh"] += (abs(batt_p) * dt) / 3600000.0
              if (now - last_save_time) > 300:
                self.save_energy()
                last_save_time = now

              rated_va_cfg = float(self.cfg.get("rated_va", self.cfg.get("rated_watt", 6200)))
              self.update_energy_state_keys()
              self.state.update({
                "device_status_code": vals[1], "device_status_msg": ANJ_STATUS_MAP.get(vals[1], f"Mode {vals[1]}"),
                "grid_volt": v_grid, "grid_freq": vals[3] / 100.0, "grid_power_watt": p_grid, "ac_charging_watt": p_ac_chg,
                "grid_current": round(p_grid / v_grid, 1) if v_grid > 0 else 0.0,
                "ac_out_volt": vals[10] / 10.0 if vals[10] > 0 else vals[5] / 10.0,
                "ac_out_amp": vals[11] / 10.0,
                "ac_out_freq": vals[12] / 100.0 if vals[12] > 0 else vals[7] / 100.0,
                "ac_load_real_watt": p_load, "ac_load_va": va_load,
                "ac_load_pct": vals[25] if vals[25] > 0 else (round((max(va_load, p_load) / rated_va_cfg) * 100, 1) if rated_va_cfg > 0 else 0),
                "bus_volt": vals[18] / 10.0, "batt_volt": v_batt, "batt_soc": vals[29],
                "batt_current": batt_current, "batt_power_watt": batt_p,
                "pv_input_volt": v_pv, "pv_current": i_pv if i_pv > 0 else (round(p_pv / v_pv, 2) if v_pv > 0 else 0.0),
                "pv_input_watt": p_pv, "pv_charging_watt": p_pv_btt,
                "temp_dc": to_signed(vals[26]), "temp_inv": to_signed(vals[27]),
              })

              if (not self.settings_synced) or (loop_cnt % 5 == 0 and (time.time() - self.last_any_write) > 8.0):
                time.sleep(0.2)
                flush_socket(conn)
                conn.send(build_read_packet(300, 44))
                vs = recv_exact_modbus(conn, 44, timeout=2.0)
                if vs:
                  for idx, r_val in enumerate(vs): self.confirmed_regs[300 + idx] = r_val
                  chg_prio = ANJ_CHG_PRIO_MAP.get(vs[31], "Solar Only (OSO)")
                  self.state.update({
                    "output_mode_str": ANJ_OUT_MODE_MAP.get(vs[1], "SUB (Solar-Util-Batt)"),
                    "ac_input_range_str": ANJ_AC_RANGE_MAP.get(vs[2], "UPS (UPS)"),
                    "buzzer_mode_str": ANJ_BUZZER_MAP.get(vs[3], "Mute (nd1)"),
                    "backlight_status": vs[5], "return_to_default": vs[6],
                    "rated_out_volt_str": ANJ_OUT_VOLT_MAP.get(vs[20], "230V"),
                    "battery_type_str": ANJ_BATT_TYPE_MAP.get(vs[22], "LIb"),
                    "bulk_charge_volt": vs[24] / 10.0, "float_charge_volt": vs[25] / 10.0,
                    "back_to_batt_volt": vs[26] / 10.0, "back_to_grid_volt": vs[27] / 10.0,
                    "low_dc_cutoff_volt": vs[29] / 10.0,
                    "charger_priority_str": chg_prio,
                    "grid_charging": "ON" if chg_prio == "Solar + Utility (SNU)" else "OFF",
                    "max_total_amps": int(vs[32] / 10), "max_ac_amps": int(vs[33] / 10),
                    "soc_back_to_grid": vs[41], "soc_back_to_batt": vs[42], "soc_cutoff": vs[43],
                  })
                  self.settings_synced = True

              self.update_live_behavior_status()
              if self.settings_synced and not self.online:
                self.publish_availability(True, f"Dongle {reply}")
              if self.online:
                self.mqtt.publish(f"solar/{self.id}/state", json.dumps(self.state), retain=True)
          loop_cnt += 1
          time.sleep(self.cfg["poll_interval"])
      except Exception as e:
        disconnect_reason = f"Exception: {e}"
        self.diag_msg = f"Disconnected: {e}"
      finally:
        last_valid_sample_time = None
        self.publish_availability(False, disconnect_reason)
        if self.conn:
          try: self.conn.close()
          except Exception: pass
          self.conn = None
        time.sleep(1)

class EasunDevice(BaseInverterDevice):
  def __init__(self, dev_cfg, mqtt_c):
    super().__init__(dev_cfg, mqtt_c)
    self.ser = None
    self.lock = threading.Lock()
    self.last_write_time = {}

  def publish_discovery(self):
    self.clear_old_discovery_topics()
    dev_info = {"identifiers": [f"solar_bridge_{self.id}"], "name": self.cfg["name"], "manufacturer": "Easun (Voltronic Protocol)", "model": "ISolar SML II 5.5KW (Axpert PI30)", "sw_version": f"v{BRIDGE_VERSION}"}
    avail_topic, state_topic = f"solar/{self.id}/status", f"solar/{self.id}/state"
    sensors = [
      ("device_status_msg", "Status", None, None, None, "mdi:information-outline"),
      ("fault_msg", "Fault Message", None, None, None, "mdi:alert-circle"),
      ("warning_msg", "Warning Message", None, None, None, "mdi:alert"),
      ("charging_status", "Charging Status", None, None, None, "mdi:battery-charging"),
      ("grid_volt", "Grid Voltage", "V", "voltage", "measurement", None),
      ("grid_freq", "Grid Frequency", "Hz", "frequency", "measurement", None),
      ("ac_out_volt", "Output Voltage", "V", "voltage", "measurement", None),
      ("ac_out_freq", "Output Frequency", "Hz", "frequency", "measurement", None),
      ("ac_out_amp", "Output Current", "A", "current", "measurement", None),
      ("ac_load_real_watt", "House Load Power", "W", "power", "measurement", None),
      ("ac_load_va", "House Load Apparent Power", "VA", "apparent_power", "measurement", None),
      ("ac_load_pct", "Inverter Load Percentage", "%", None, "measurement", "mdi:percent"),
      ("bus_volt", "DC Bus Voltage", "V", "voltage", "measurement", None),
      ("batt_volt", "Battery Voltage (Inverter)", "V", "voltage", "measurement", None),
      ("batt_soc", "Battery Percentage", "%", "battery", "measurement", None),
      ("batt_current", "Battery Current", "A", "current", "measurement", None),
      ("batt_power_watt", "Battery Power Flow", "W", "power", "measurement", None),
      ("pv_input_volt", "PV Input Voltage", "V", "voltage", "measurement", None),
      ("pv_current", "PV Current", "A", "current", "measurement", None),
      ("pv_input_watt", "PV Input Power", "W", "power", "measurement", None),
      ("temp_inv", "Inverter Temperature", "°C", "temperature", "measurement", None),
      ("total_pv_energy_kwh", "Solar Energy", "kWh", "energy", "total_increasing", "mdi:solar-power"),
      ("total_load_kwh", "House Load Energy", "kWh", "energy", "total_increasing", "mdi:home-lightning-bolt"),
      ("total_battery_charge_kwh", "Battery Charge Energy", "kWh", "energy", "total_increasing", "mdi:battery-charging"),
      ("total_battery_discharge_kwh", "Battery Discharge Energy", "kWh", "energy", "total_increasing", "mdi:battery-minus"),
    ]
    for key, name, unit, d_class, s_class, icon in sensors:
      payload = {"name": name, "unique_id": f"{self.id}_{key}", "state_topic": state_topic, "value_template": f"{{{{ value_json.{key} }}}}", "device": dev_info}
      if not key.endswith("_kwh"): payload["availability_topic"] = avail_topic
      if unit: payload["unit_of_measurement"] = unit
      if d_class: payload["device_class"] = d_class
      if s_class: payload["state_class"] = s_class
      if icon: payload["icon"] = icon
      self.mqtt.publish(f"homeassistant/sensor/{self.id}/{key}/config", json.dumps(payload), retain=True)

    selects = [
      ("output_mode_str", "Output Source Priority", list(EASUN_OUT_MODE_MAP.values()), "mdi:source-branch"),
      ("charger_priority_str", "Charger Source Priority", list(EASUN_CHG_PRIO_MAP.values()), "mdi:battery-charging"),
      ("ac_input_range_str", "AC Input Range", list(EASUN_AC_RANGE_MAP.values()), "mdi:sine-wave"),
      ("battery_type_str", "Battery Type", list(EASUN_BATT_TYPE_MAP.values()), "mdi:battery-sync"),
      ("rated_out_volt_str", "Rated Output Voltage", list(EASUN_OUT_VOLT_MAP.values()), "mdi:sine-wave"),
    ]
    for key, name, options, icon in selects:
      payload = {"name": name, "unique_id": f"{self.id}_{key}", "state_topic": state_topic, "value_template": f"{{{{ value_json.{key} }}}}", "command_topic": f"solar/{self.id}/set/{key}", "availability_topic": avail_topic, "options": options, "icon": icon, "device": dev_info}
      self.mqtt.publish(f"homeassistant/select/{self.id}/{key}/config", json.dumps(payload), retain=True)

    numbers = [
      ("bulk_charge_volt", "Bulk Charging Voltage", 48.0, 58.4, 0.1, "V", "mdi:battery-arrow-up"),
      ("float_charge_volt", "Float Charging Voltage", 48.0, 58.4, 0.1, "V", "mdi:battery-check"),
      ("back_to_grid_volt", "Back to Grid Voltage", 44.0, 51.0, 1.0, "V", "mdi:battery-arrow-down"),
      ("back_to_batt_volt", "Back to Battery Voltage", 0.0, 58.0, 1.0, "V", "mdi:battery-arrow-up"),
      ("low_dc_cutoff_volt", "Low DC Cut-off Voltage", 40.0, 48.0, 0.1, "V", "mdi:battery-alert"),
      ("max_total_amps", "Max Charging Current (Total)", 10, 100, 10, "A", "mdi:battery-charging-high"),
      ("max_ac_amps", "Max AC Charge Amps", 2, 80, 1, "A", "mdi:current-ac"),
    ]
    for key, name, mn, mx, step, unit, icon in numbers:
      payload = {"name": name, "unique_id": f"{self.id}_{key}", "state_topic": state_topic, "value_template": f"{{{{ value_json.{key} }}}}", "command_topic": f"solar/{self.id}/set/{key}", "availability_topic": avail_topic, "min": mn, "max": mx, "step": step, "unit_of_measurement": unit, "mode": "box", "icon": icon, "device": dev_info}
      self.mqtt.publish(f"homeassistant/number/{self.id}/{key}/config", json.dumps(payload), retain=True)

    switches = [("backlight_status", "LCD Backlight", "mdi:monitor-shimmer"), ("return_to_default", "Return to Default Screen", "mdi:arrow-u-left-top"), ("buzzer_alarm", "Buzzer Alarm", "mdi:volume-high"), ("overload_bypass", "Overload Bypass", "mdi:transfer"), ("grid_charging", "Grid Charging", "mdi:flash")]
    for key, name, icon in switches:
      vt = "{{ 'ON' if value_json.charger_priority_str == 'Solar + Utility (SNU)' else 'OFF' }}" if key == "grid_charging" else f"{{{{ 'ON' if value_json.{key} == 1 else 'OFF' }}}}"
      payload = {"name": name, "unique_id": f"{self.id}_{key}", "state_topic": state_topic, "value_template": vt, "command_topic": f"solar/{self.id}/set/{key}", "availability_topic": avail_topic, "payload_on": "ON", "payload_off": "OFF", "icon": icon, "device": dev_info}
      self.mqtt.publish(f"homeassistant/switch/{self.id}/{key}/config", json.dumps(payload), retain=True)

  def _read_serial_line_locked(self, cmd: str, wait_sec: float = 2.0) -> bytes:
    self.ser.reset_input_buffer()
    self.ser.write(build_axpert_packet(cmd))
    deadline, buf = time.time() + wait_sec, b''
    while time.time() < deadline:
      chunk = self.ser.read(256)
      if chunk:
        buf += chunk
        if b'\r' in buf: break
      time.sleep(0.05)
    return buf

  def _send_serial_cmd_locked(self, cmd: str, timeout: float = 3.0) -> str:
    resp = self._read_serial_line_locked(cmd, timeout).decode('ascii', errors='ignore').strip()
    return 'ACK' if 'ACK' in resp else ('NAK' if 'NAK' in resp else 'TIMEOUT')

  def _refresh_qflag_locked(self):
    time.sleep(0.15)
    raw_f = self._read_serial_line_locked('QFLAG', 1.8)
    if raw_f and b'(' in raw_f:
      line = raw_f[raw_f.find(b'(') + 1:].split(b'\r')[0]
      if len(line) > 2: line = line[:-2]
      f_str = line.decode('ascii', errors='ignore').strip().lower()
      if 'd' in f_str:
        en_part = f_str.split('d')[0]
        self.state.update({'buzzer_alarm': 1 if 'a' in en_part else 0, 'overload_bypass': 1 if 'b' in en_part else 0, 'return_to_default': 1 if 'k' in en_part else 0, 'backlight_status': 1 if 'x' in en_part else 0})

  def _refresh_qpiri_and_flags_locked(self) -> bool:
    time.sleep(0.15)
    raw = self._read_serial_line_locked('QPIRI', 2.2)
    if not raw or b'(' not in raw: return False
    fields = raw[raw.find(b'(') + 1:].decode('ascii', errors='ignore').strip().split()
    if len(fields) < 23: return False
    try:
      chg_prio = EASUN_CHG_PRIO_MAP.get(int(fields[17]), "Solar Only (OSO)")
      self.state.update({
        'rated_out_volt_str': EASUN_OUT_VOLT_MAP.get(int(float(fields[2])), "230V"),
        'hw_rated_va': int(float(fields[5])), 'hw_rated_watt': int(float(fields[6])),
        'back_to_grid_volt': float(fields[8]), 'low_dc_cutoff_volt': float(fields[9]),
        'bulk_charge_volt': float(fields[10]), 'float_charge_volt': float(fields[11]),
        'battery_type_str': EASUN_BATT_TYPE_MAP.get(int(fields[12]), "USR"),
        'max_ac_amps': int(fields[13]), 'max_total_amps': int(fields[14]),
        'ac_input_range_str': EASUN_AC_RANGE_MAP.get(int(fields[15]), "Appliances (APL)"),
        'output_mode_str': EASUN_OUT_MODE_MAP.get(int(fields[16]), "SBU (Solar-Batt-Util)"),
        'charger_priority_str': chg_prio,
        'grid_charging': "ON" if chg_prio == "Solar + Utility (SNU)" else "OFF",
        'back_to_batt_volt': float(fields[22]),
      })
      self.settings_synced = True
    except Exception as e:
      self.diag_msg = f"QPIRI parse error: {e}"
      return False
    self._refresh_qflag_locked()
    return True

  def execute_easun_write(self, prop: str, new_val, axpert_cmd: str, fallback_cmd: str = None) -> bool:
    if not self.online or not self.settings_synced:
      log_event(self.id, f"Blocked {axpert_cmd} (Offline/Unsynced)")
      return False
    if self.state.get(prop) == new_val: return False
    now = time.time()
    if (now - self.last_write_time.get(prop, 0)) < 3.0:
      log_event(self.id, f"Rate-limited {axpert_cmd}")
      return False
    with self.lock:
      res = self._send_serial_cmd_locked(axpert_cmd)
      if res != "ACK" and fallback_cmd:
        time.sleep(0.2)
        res = self._send_serial_cmd_locked(fallback_cmd)
        log_event(self.id, f"WRITE {axpert_cmd} NAK -> Fallback {fallback_cmd}: {res}")
      else:
        log_event(self.id, f"WRITE {axpert_cmd} -> {res}")
      if res == "ACK":
        self.last_write_time[prop] = now
        if axpert_cmd.startswith(("PE", "PD")): self._refresh_qflag_locked()
        return True
      return False

  def handle_mqtt_command(self, prop: str, payload: str):
    try:
      ok = False
      if prop == "grid_charging":
        code = 2 if payload == "ON" else 3
        target_str = "Solar + Utility (SNU)" if payload == "ON" else "Solar Only (OSO)"
        ok = self.execute_easun_write("charger_priority_str", target_str, f"PCP{code:02d}")
        if ok:
          self.state["charger_priority_str"] = target_str
          self.state["grid_charging"] = payload
      elif prop == "output_mode_str" and payload in EASUN_OUT_MODE_REV:
        ok = self.execute_easun_write(prop, payload, f"POP{EASUN_OUT_MODE_REV[payload]:02d}")
      elif prop == "charger_priority_str" and payload in EASUN_CHG_PRIO_REV:
        ok = self.execute_easun_write(prop, payload, f"PCP{EASUN_CHG_PRIO_REV[payload]:02d}")
      elif prop == "ac_input_range_str" and payload in EASUN_AC_RANGE_REV:
        ok = self.execute_easun_write(prop, payload, f"PGR{EASUN_AC_RANGE_REV[payload]:02d}")
      elif prop == "battery_type_str" and payload in EASUN_BATT_TYPE_REV:
        ok = self.execute_easun_write(prop, payload, f"PBT{EASUN_BATT_TYPE_REV[payload]:02d}")
      elif prop == "rated_out_volt_str" and payload in EASUN_OUT_VOLT_REV:
        ok = self.execute_easun_write(prop, payload, f"V{EASUN_OUT_VOLT_REV[payload]}")
      elif prop == "bulk_charge_volt":
        v = round(float(payload), 1); ok = self.execute_easun_write(prop, v, f"PCVV{v:.1f}"); payload = v
      elif prop == "float_charge_volt":
        v = round(float(payload), 1); ok = self.execute_easun_write(prop, v, f"PBFT{v:.1f}"); payload = v
      elif prop == "low_dc_cutoff_volt":
        v = round(float(payload), 1); ok = self.execute_easun_write(prop, v, f"PSDV{v:.1f}"); payload = v
      elif prop == "back_to_grid_volt":
        v = round(float(payload), 1); ok = self.execute_easun_write(prop, v, f"PBCV{v:.1f}"); payload = v
      elif prop == "back_to_batt_volt":
        v = round(float(payload), 1); ok = self.execute_easun_write(prop, v, f"PBDV{v:04.1f}"); payload = v
      elif prop == "max_total_amps":
        v = int(float(payload)); ok = self.execute_easun_write(prop, v, f"MCHGC{v:03d}", f"MNCHGC0{v:03d}"); payload = v
      elif prop == "max_ac_amps":
        v = int(float(payload)); ok = self.execute_easun_write(prop, v, f"MUCHGC{v:03d}"); payload = v
      elif prop in ("backlight_status", "return_to_default", "buzzer_alarm", "overload_bypass"):
        fl = {"buzzer_alarm": "a", "overload_bypass": "b", "return_to_default": "k", "backlight_status": "x"}[prop]
        v = 1 if payload == "ON" else 0
        ok = self.execute_easun_write(prop, v, f"PE{fl}" if v == 1 else f"PD{fl}", f"PE{fl.upper()}" if v == 1 else f"PD{fl.upper()}")
        payload = v
      if ok: self.state[prop] = payload
      self.mqtt.publish(f"solar/{self.id}/state", json.dumps(self.state), retain=True)
    except Exception as e:
      log_event(self.id, f"Command error ({prop}={payload}): {e}")

  def run_worker(self):
    last_valid_sample_time, last_save_time, last_qpiri_time = None, time.time(), 0.0
    failures, loop_cnt = 0, 0
    while True:
      if serial is None:
        self.diag_msg = "ERROR: python3-serial not installed!"
        time.sleep(5); continue
      port_path = self.cfg.get("serial_port", "/dev/easun-rs485")
      baud = int(self.cfg.get("baud_rate", 2400))
      if not os.path.exists(port_path):
        self.diag_msg = f"ERROR: {port_path} does not exist!"
        last_valid_sample_time = None
        self.publish_availability(False, f"Missing {port_path}")
        time.sleep(5); continue
      try:
        if self.ser is None or not self.ser.is_open:
          self.diag_msg = f"Opening {port_path} at {baud} baud..."
          self.ser = serial.Serial(port=port_path, baudrate=baud, timeout=2.0)
          failures = 0
        with self.lock:
          raw = self._read_serial_line_locked('QPIGS', 2.2)
          if not raw or b'(' not in raw:
            failures += 1
            self.diag_msg = f"Timeout waiting for QPIGS on {port_path} ({failures}/5)"
            if failures >= 5:
              last_valid_sample_time = None
              self.publish_availability(False, "5 consecutive QPIGS timeouts")
              self.ser.close(); time.sleep(2); continue
          else:
            failures = 0
            fields = raw[raw.find(b'(') + 1:].decode('ascii', errors='ignore').strip().split(' ')
            if len(fields) >= 20:
              now = time.time()
              dt = (now - last_valid_sample_time) if last_valid_sample_time is not None else 0.0
              last_valid_sample_time = now
              self.diag_msg = f"Connected on {port_path} ({baud} baud, Poll dt={dt:.1f}s)"
              grid_v, grid_f, ac_out_v, ac_out_f = float(fields[0]), float(fields[1]), float(fields[2]), float(fields[3])
              ac_out_va, ac_out_w, load_pct, bus_v = int(fields[4]), int(fields[5]), int(fields[6]), int(fields[7])
              batt_v, batt_chg_c, batt_soc, temp_inv = float(fields[8]), int(fields[9]), int(fields[10]), int(fields[11])
              pv_c, pv_v, batt_dis_c, status_1, pv_w = float(fields[12]), float(fields[13]), int(fields[15]), fields[16], int(fields[19])

              chg_status = "Not Charging"
              if len(status_1) >= 8:
                cb = status_1[5:8]
                if cb == "110": chg_status = "SCC Charging (Solar)"
                elif cb == "101": chg_status = "AC Charging (Grid)"
                elif cb == "111": chg_status = "SCC + AC Charging"

              net_batt_c = float(batt_chg_c - batt_dis_c)
              batt_p = int(net_batt_c * batt_v)
              if pv_w > 15 and batt_dis_c == 0:
                est_chg_w = int(pv_w * 0.96) if ac_out_v < 180.0 else max(0, int((pv_w * 0.95) - ac_out_w - 38))
                if batt_p < (est_chg_w * 0.85):
                  batt_p = est_chg_w
                  net_batt_c = round(batt_p / batt_v, 1) if batt_v > 10.0 else 0.0

              ac_out_amp = round(ac_out_va / ac_out_v, 1) if ac_out_v > 0 else 0.0
              if 0.0 < dt <= 60.0:
                with self.energy_lock:
                  if pv_w > 0: self.energy["total_pv_kwh"] += (pv_w * dt) / 3600000.0
                  if ac_out_w > 0: self.energy["total_load_kwh"] += (ac_out_w * dt) / 3600000.0
                  if batt_p > 0: self.energy["total_battery_charge_kwh"] += (batt_p * dt) / 3600000.0
                  elif batt_p < 0: self.energy["total_battery_discharge_kwh"] += (abs(batt_p) * dt) / 3600000.0
              if (now - last_save_time) > 300:
                self.save_energy(); last_save_time = now

              self.update_energy_state_keys()
              self.state.update({
                "charging_status": chg_status, "grid_volt": grid_v, "grid_freq": grid_f,
                "ac_out_volt": ac_out_v, "ac_out_freq": ac_out_f, "ac_out_amp": ac_out_amp,
                "ac_load_real_watt": ac_out_w, "ac_load_va": ac_out_va, "ac_load_pct": load_pct,
                "bus_volt": float(bus_v), "batt_volt": batt_v, "batt_soc": batt_soc,
                "batt_current": net_batt_c, "batt_power_watt": batt_p,
                "pv_input_watt": pv_w, "pv_input_volt": pv_v, "pv_current": pv_c, "temp_inv": temp_inv,
              })

              if loop_cnt % 4 == 2 or "device_status_msg" not in self.state:
                time.sleep(0.1)
                raw_m = self._read_serial_line_locked('QMOD', 1.2)
                if raw_m and b'(' in raw_m:
                  m_char = raw_m[raw_m.find(b'(')+1 : raw_m.find(b'(')+2].decode('ascii', errors='ignore')
                  self.state["device_status_msg"] = EASUN_MODE_MAP.get(m_char, f"Mode {m_char}")

              if not self.settings_synced or (now - last_qpiri_time) > 60:
                self._refresh_qpiri_and_flags_locked()
                last_qpiri_time = time.time()
              self.update_live_behavior_status()
              if self.settings_synced and not self.online:
                self.publish_availability(True, f"Serial {port_path}")
              if self.online:
                self.mqtt.publish(f"solar/{self.id}/state", json.dumps(self.state), retain=True)
        loop_cnt += 1
        time.sleep(self.cfg.get("poll_interval", 1.5))
      except Exception as e:
        last_valid_sample_time = None
        self.diag_msg = f"Serial Exception: {e}"
        self.publish_availability(False, f"Serial error: {e}")
        if self.ser and self.ser.is_open:
          try: self.ser.close()
          except Exception: pass
        time.sleep(2)

devices_map = {}
config = {}

ENTITY_DISPLAY_SPEC = [
  ("device_status_msg", "Status", ""),
  ("charging_status", "Charging Status", ""),
  ("fault_msg", "Fault Message", ""),
  ("warning_msg", "Warning Message", ""),
  ("grid_volt", "Grid Voltage", "V"),
  ("grid_freq", "Grid Frequency", "Hz"),
  ("grid_power_watt", "Grid Input Power", "W"),
  ("grid_current", "Grid Current", "A"),
  ("ac_charging_watt", "AC Charging Power", "W"),
  ("ac_out_volt", "Output Voltage", "V"),
  ("ac_out_freq", "Output Frequency", "Hz"),
  ("ac_out_amp", "Output Current", "A"),
  ("ac_load_real_watt", "House Load Power", "W"),
  ("ac_load_va", "House Load Apparent", "VA"),
  ("ac_load_pct", "Inverter Load %", "%"),
  ("bus_volt", "DC Bus Voltage", "V"),
  ("batt_volt", "Battery Voltage", "V"),
  ("batt_soc", "Battery SOC", "%"),
  ("batt_current", "Battery Current", "A"),
  ("batt_power_watt", "Battery Power Flow", "W"),
  ("pv_input_volt", "PV Input Voltage", "V"),
  ("pv_current", "PV Current", "A"),
  ("pv_input_watt", "PV Input Power", "W"),
  ("pv_charging_watt", "PV Charging Power", "W"),
  ("temp_inv", "Inverter Temp", "°C"),
  ("temp_dc", "DC-DC Temp", "°C"),
  ("output_mode_str", "Output Priority", ""),
  ("charger_priority_str", "Charger Priority", ""),
  ("grid_charging", "Grid Charging Switch", ""),
  ("ac_input_range_str", "AC Input Range", ""),
  ("battery_type_str", "Battery Type", ""),
  ("rated_out_volt_str", "Rated Output Volt", ""),
  ("buzzer_mode_str", "Buzzer Mode", ""),
  ("buzzer_alarm", "Buzzer Alarm", "bool"),
  ("overload_bypass", "Overload Bypass", "bool"),
  ("backlight_status", "LCD Backlight", "bool"),
  ("return_to_default", "Return Default Screen", "bool"),
  ("bulk_charge_volt", "Bulk Charge Volt", "V"),
  ("float_charge_volt", "Float Charge Volt", "V"),
  ("back_to_grid_volt", "Back to Grid Volt", "V"),
  ("back_to_batt_volt", "Back to Batt Volt", "V"),
  ("low_dc_cutoff_volt", "Low DC Cut-off Volt", "V"),
  ("max_total_amps", "Max Total Charge", "A"),
  ("max_ac_amps", "Max AC Charge", "A"),
  ("soc_back_to_grid", "Back to Grid SOC", "%"),
  ("soc_back_to_batt", "Back to Batt SOC", "%"),
  ("soc_cutoff", "Cut-off SOC", "%"),
  ("total_pv_energy_kwh", "Total Solar Energy", "kWh"),
  ("total_grid_input_kwh", "Total Grid Import", "kWh"),
  ("total_load_kwh", "Total House Load", "kWh"),
  ("total_battery_charge_kwh", "Total Batt Charge", "kWh"),
  ("total_battery_discharge_kwh", "Total Batt Discharge", "kWh"),
]

def render_device_card_html(d_id: str, dev: BaseInverterDevice) -> str:
  badge = "<span style='color:#4caf50;font-weight:bold;'>ONLINE</span>" if dev.online else "<span style='color:#f44336;font-weight:bold;'>OFFLINE</span>"
  role_tag = "MAIN (GRID)" if dev.role == "main" else "ISLAND"
  st = dev.state
  hw_va = st.get("hw_rated_va") or dev.cfg.get("rated_va", "-")
  hw_w = st.get("hw_rated_watt") or dev.cfg.get("rated_watt", "-")
  pv_cap = int(min(float(dev.cfg.get("installed_pv_watt", 0)), float(hw_w if isinstance(hw_w, (int, float)) else 99999)))
  cells = []
  for key, label, unit in ENTITY_DISPLAY_SPEC:
    if key not in st:
      continue
    val = st[key]
    if unit == "bool":
      v_str = "ON" if val == 1 else "OFF"
    elif key == "back_to_batt_volt" and val == 0.0:
      v_str = "0.0 V (FUL/BMS)"
    elif isinstance(val, float) and unit == "kWh":
      v_str = f"{val:.3f} kWh"
    else:
      v_str = f"{val} {unit}".strip()
    if key == "fault_msg" and val not in ("No Fault", ""):
      val_col = "#ff5252"
    elif key == "warning_msg" and val not in ("No Warning", ""):
      val_col = "#ff5252" if st.get("serious_warn", False) else "#ffb74d"
    else:
      val_col = "#e0f7fa"
    cells.append(
      f"<div class='ent-cell'><span class='ent-lbl'>{label} <small>({key})</small></span>"
      f"<span class='ent-val' style='color:{val_col};'>{v_str}</span></div>"
    )
  return f"""
  <div style='background:#1a1d24;padding:12px 14px;border-radius:8px;margin-bottom:12px;border:1px solid #333;'>
    <div style='display:flex;justify-content:space-between;align-items:baseline;flex-wrap:wrap;gap:8px;margin-bottom:6px;'>
      <h3 style='margin:0;font-size:15px;'>[{role_tag}] {dev.cfg['name']} ({d_id}) — {badge}</h3>
      <span style='color:#90caf9;font-size:12px;'><b>Diag:</b> {dev.diag_msg}</span>
    </div>
    <div style='font-size:11.5px;color:#aaa;margin-bottom:8px;border-bottom:1px solid #2c313c;padding-bottom:5px;'>
      <b>Config & Limits:</b> Rated={hw_w}W / {hw_va}VA | Installed PV={pv_cap}W | Max PV/Bus={dev.cfg.get('max_pv_volt','-')}/{dev.cfg.get('max_bus_volt','-')}V | Energy File=<code>{dev.cfg['energy_file']}</code>
    </div>
    <div class='ent-grid'>{"".join(cells)}</div>
  </div>"""

def get_masked_config_json() -> str:
  ui_cfg = json.loads(json.dumps(config))
  if ui_cfg.get("mqtt_pass"): ui_cfg["mqtt_pass"] = PASSWORD_MASK
  return json.dumps(ui_cfg, indent=2)

def _svg_dc_cutout(x: float, y: float) -> str:
  bg = "#082268"
  return f'<rect x="{x:.1f}" y="{y:.1f}" width="3.6" height="2.2" rx="0.8" fill="{bg}"/><rect x="{x+5.4:.1f}" y="{y:.1f}" width="3.6" height="2.2" rx="0.8" fill="{bg}"/><rect x="{x+10.8:.1f}" y="{y:.1f}" width="3.6" height="2.2" rx="0.8" fill="{bg}"/><rect x="{x:.1f}" y="{y+4.2:.1f}" width="14.4" height="2.2" rx="0.8" fill="{bg}"/>'

def _svg_ac_wave(x: float, y: float, color: str = "#e0f7fa") -> str:
  return f'<path d="M {x:.1f} {y+2.5:.1f} Q {x+3.6:.1f} {y-4.0:.1f}, {x+7.2:.1f} {y+2.5:.1f} T {x+14.4:.1f} {y+2.5:.1f}" fill="none" stroke="{color}" stroke-width="2.1" stroke-linecap="round"/>'

def _svg_converter_box(box_id: str, x: int, y: int, left_type: str, right_type: str) -> str:
  w, h, col = 54, 32, "#e0f7fa"
  ul_path = f"M {x} {y+h-3.5} L {x} {y+4.5} Q {x} {y} {x+4.5} {y} L {x+w-5.5} {y} Z"
  lr_path = f"M {x+5.5} {y+h} L {x+w-4.5} {y+h} Q {x+w} {y+h} {x+w} {y+h-4.5} L {x+w} {y+3.5} Z"
  ul_svg = (f'<path d="{ul_path}" fill="{col}" stroke="{col}" stroke-width="1.4" stroke-linejoin="round"/>' + _svg_dc_cutout(x + 5.5, y + 5.2)) if left_type == "dc" else (f'<path d="{ul_path}" fill="rgba(10,38,115,0.18)" stroke="{col}" stroke-width="1.6" stroke-linejoin="round"/>' + _svg_ac_wave(x + 5.5, y + 6.5, col))
  lr_svg = (f'<path d="{lr_path}" fill="{col}" stroke="{col}" stroke-width="1.4" stroke-linejoin="round"/>' + _svg_dc_cutout(x + 33.5, y + 19.2)) if right_type == "dc" else (f'<path d="{lr_path}" fill="rgba(10,38,115,0.18)" stroke="{col}" stroke-width="1.6" stroke-linejoin="round"/>' + _svg_ac_wave(x + 33.5, y + 20.5, col))
  return f'<g id="{box_id}"><g class="dpi-box">{ul_svg}{lr_svg}</g></g>'

def _svg_hvdc_bus_box(x: int, y: int, short_name: str, val_id: str) -> str:
  w, h, col, dc_col = 130, 56, "#e0f7fa", "rgba(224,247,250,0.68)"
  return (
    f'<g class="dpi-box"><rect x="{x+2}" y="{y+3}" width="{w-4}" height="{h-6}" rx="4" fill="rgba(16,46,124,0.58)"/>'
    f'<line x1="{x+20}" y1="{y}" x2="{x+46}" y2="{y}" stroke="{dc_col}" stroke-width="2.0" stroke-linecap="round"/>'
    f'<line x1="{x+52}" y1="{y}" x2="{x+78}" y2="{y}" stroke="{dc_col}" stroke-width="2.0" stroke-linecap="round"/>'
    f'<line x1="{x+84}" y1="{y}" x2="{x+w-20}" y2="{y}" stroke="{dc_col}" stroke-width="2.0" stroke-linecap="round"/>'
    f'<path d="M {x+13} {y} L {x+5} {y} Q {x} {y} {x} {y+5} L {x} {y+h-5} Q {x} {y+h} {x+5} {y+h} L {x+w-5} {y+h} Q {x+w} {y+h} {x+w} {y+h-5} L {x+w} {y+5} Q {x+w} {y} {x+w-5} {y} L {x+w-13} {y}" fill="none" stroke="{col}" stroke-width="2.0" stroke-linecap="round" stroke-linejoin="round"/>'
    f'<text x="{x+w/2}" y="{y+23}" text-anchor="middle" class="svg-val">{short_name.upper()}</text>'
    f'<text id="{val_id}" x="{x+w/2}" y="{y+46}" text-anchor="middle" class="svg-val">0V</text></g>'
  )

def _svg_voltronic_pv_icon(x_tl: float, y_top: float) -> str:
  col = "#e0f7fa"
  return (
    f'<g transform="translate({x_tl:.1f}, {y_top:.1f}) matrix(1 0 -0.16 1 0 0)">'
    f'<path d="M 13 0 L 1.8 0 Q 0 0 0 1.8 L 0 42.2 Q 0 44 1.8 44 L 13 44" fill="none" stroke="{col}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>'
    f'<path d="M 19 0 L 30.2 0 Q 32 0 32 1.8 L 32 42.2 Q 32 44 30.2 44 L 19 44" fill="none" stroke="{col}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>'
    f'<rect x="3.5" y="3.6" width="11.5" height="11" rx="2.3" fill="{col}"/><rect x="17.0" y="3.6" width="11.5" height="11" rx="2.3" fill="{col}"/>'
    f'<rect x="3.5" y="16.5" width="11.5" height="11" rx="2.3" fill="{col}"/><rect x="17.0" y="16.5" width="11.5" height="11" rx="2.3" fill="{col}"/>'
    f'<rect x="3.5" y="29.4" width="11.5" height="11" rx="2.3" fill="{col}"/><rect x="17.0" y="29.4" width="11.5" height="11" rx="2.3" fill="{col}"/>'
    f'<line x1="9.25" y1="3.1" x2="9.25" y2="40.9" stroke="#082268" stroke-width="1.15"/><line x1="22.75" y1="3.1" x2="22.75" y2="40.9" stroke="#082268" stroke-width="1.15"/>'
    f'<line x1="3.0" y1="9.1" x2="29.0" y2="9.1" stroke="#082268" stroke-width="1.15"/><line x1="3.0" y1="22.0" x2="29.0" y2="22.0" stroke="#082268" stroke-width="1.15"/>'
    f'<line x1="3.0" y1="34.9" x2="29.0" y2="34.9" stroke="#082268" stroke-width="1.15"/></g>'
  )

def _svg_bypass_emblem() -> str:
  return '<defs><path id="bypass_text_arc_top" d="M 362 64 A 83 24 0 0 1 528 64" fill="none"/></defs><text class="svg-bypass-font"><textPath href="#bypass_text_arc_top" startOffset="50%" text-anchor="middle">BYPASS</textPath></text>'

def _svg_grid_tower_icon() -> str:
  col = "#e0f7fa"
  return (
    '<g id="icon_grid_tower">'
    f'<line x1="50" y1="49" x2="62" y2="49" stroke="{col}" stroke-width="1.7" stroke-linecap="round"/>'
    f'<path d="M 53 52 L 46 94 L 42.5 94" fill="none" stroke="{col}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>'
    f'<path d="M 59 52 L 66 94 L 69.5 94" fill="none" stroke="{col}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>'
    f'<line x1="39" y1="60" x2="73" y2="60" stroke="{col}" stroke-width="1.7" stroke-linecap="round"/><line x1="42" y1="72" x2="70" y2="72" stroke="{col}" stroke-width="1.7" stroke-linecap="round"/>'
    f'<line x1="39" y1="60" x2="39" y2="65.5" stroke="{col}" stroke-width="1.5" stroke-linecap="round"/><line x1="73" y1="60" x2="73" y2="65.5" stroke="{col}" stroke-width="1.5" stroke-linecap="round"/>'
    f'<line x1="42" y1="72" x2="42" y2="77.5" stroke="{col}" stroke-width="1.5" stroke-linecap="round"/><line x1="70" y1="72" x2="70" y2="77.5" stroke="{col}" stroke-width="1.5" stroke-linecap="round"/>'
    f'<path d="M 51.5 60 L 62.5 72 M 60.5 60 L 49.5 72 M 49.5 72 L 64.5 88 M 62.5 72 L 47.5 88" fill="none" stroke="{col}" stroke-width="1.1" stroke-linecap="round"/></g>'
  )

def _svg_generator_icon() -> str:
  col = "#e0f7fa"
  return f'<g id="icon_gas_gen" style="display:none;"><rect x="39" y="64" width="34" height="26" rx="3" fill="rgba(224,247,250,0.12)" stroke="{col}" stroke-width="1.8"/><rect x="45" y="58" width="12" height="6" rx="1" fill="none" stroke="{col}" stroke-width="1.5"/><path d="M 66 64 L 66 54 L 72 54" fill="none" stroke="{col}" stroke-width="2" stroke-linecap="round"/><circle cx="56" cy="77" r="8" fill="none" stroke="{col}" stroke-width="1.4"/><path d="M 51 77 Q 53.5 72, 56 77 T 61 77" fill="none" stroke="{col}" stroke-width="1.6"/><line x1="42" y1="94" x2="48" y2="94" stroke="{col}" stroke-width="2"/><line x1="64" y1="94" x2="70" y2="94" stroke="{col}" stroke-width="2"/></g>'

def get_main_and_island_devices():
  main_dev, islands = None, []
  for d_cfg in config.get("devices", []):
    dev = devices_map.get(d_cfg["id"])
    if not dev: continue
    if d_cfg.get("role") == "main" and main_dev is None: main_dev = dev
    else: islands.append(dev)
  if main_dev is None and islands: main_dev = islands.pop(0)
  return main_dev, islands

def build_api_data() -> dict:
  main_dev, island_devs = get_main_and_island_devices()
  sa = main_dev.state if (main_dev and main_dev.online) else {}
  a_on = bool(main_dev and main_dev.online)

  ind_cfg = config.get("indicators", {})
  indicators_payload = {
    "show_grid_bar": bool(ind_cfg.get("show_grid_bar", True)),
    "show_solar_bar": bool(ind_cfg.get("show_solar_bar", True)),
    "show_battery_bar": bool(ind_cfg.get("show_battery_bar", True)),
    "show_load_bar": bool(ind_cfg.get("show_load_bar", True))
  }

  a_mode = sa.get("device_status_msg", "OFFLINE") if a_on else "OFFLINE"
  a_grid_v = int(round(float(sa.get("grid_volt") or 0)))
  a_grid_f = round(float(sa.get("grid_freq") or 0.0), 1)
  a_grid_w = int(round(float(sa.get("grid_power_watt") or 0)))
  a_grid_a = round(float(sa.get("grid_current") or 0.0), 1)
  a_ac_chg_w = int(round(float(sa.get("ac_charging_watt") or 0)))
  a_pv_v = int(round(float(sa.get("pv_input_volt") or 0)))
  a_pv_w = int(round(float(sa.get("pv_input_watt") or 0)))
  a_pv_a = int(round(float(sa.get("pv_current") or 0)))
  a_bus_v = int(round(float(sa.get("bus_volt") or 0)))
  a_out_v = int(round(float(sa.get("ac_out_volt") or 0)))
  a_out_f = round(float(sa.get("ac_out_freq") or 0.0), 1)
  a_out_a = int(round(float(sa.get("ac_out_amp") or 0)))
  a_load_w = int(round(float(sa.get("ac_load_real_watt") or 0)))
  a_load_va = int(round(float(sa.get("ac_load_va") or 0)))
  a_batt_v = round(float(sa.get("batt_volt") or 0.0), 1)
  a_batt_a_raw = float(sa.get("batt_current") or 0.0)
  a_batt_a = int(round(a_batt_a_raw))
  a_batt_w = int(round(float(sa.get("batt_power_watt") or 0)))
  a_soc = int(round(float(sa.get("batt_soc") or 0)))
  a_btype = str(sa.get("battery_type_str", "LIb")).strip()
  is_gen_mode = ("GEN" in sa.get("ac_input_range_str", "UPS (UPS)").upper())
  a_max_temp = max(int(round(float(sa.get("temp_dc") or 0))), int(round(float(sa.get("temp_inv") or 0))))

  batt_chem_tag = "Li" if a_btype.upper().startswith("LI") else a_btype.upper()
  is_bypass = ("Line" in a_mode or "Bypass" in a_mode)

  w_a_grid_chg = a_ac_chg_w if a_ac_chg_w > 15 else 0
  gchg_lbl = f"{a_ac_chg_w}W" if w_a_grid_chg > 0 else ""

  if is_bypass:
    grid_to_load = max(0, a_grid_w - w_a_grid_chg)
    if a_grid_w > 12:
      if a_pv_w > 30 or a_batt_w < -30:
        w_a_bypass = min(a_load_w, grid_to_load)
        w_a_inv_out = max(0, a_load_w - w_a_bypass)
      else:
        w_a_bypass = grid_to_load if grid_to_load > 12 else a_load_w
        w_a_inv_out = 0
    else:
      if a_pv_w > 30 or a_batt_w < -30 or a_load_w <= 12:
        w_a_bypass = 0
        w_a_inv_out = a_load_w if a_out_v >= 180 else 0
      else:
        w_a_bypass = a_load_w
        w_a_inv_out = 0
  else:
    w_a_bypass = 0
    w_a_inv_out = a_load_w if a_out_v >= 180 else 0

  m_cfg = main_dev.cfg if main_dev else {}
  m_max_pv_v = float(m_cfg.get("max_pv_volt", 450.0))
  m_max_bus_v = float(m_cfg.get("max_bus_volt", 450.0))
  eff_m_va = float(sa.get("hw_rated_va") or m_cfg.get("rated_va") or m_cfg.get("rated_watt") or 6200)
  eff_m_max_w = float(sa.get("hw_rated_watt") or m_cfg.get("rated_watt") or eff_m_va)
  eff_m_pv_max = min(float(m_cfg.get("installed_pv_watt", eff_m_max_w)), eff_m_max_w) if eff_m_max_w > 0 else float(m_cfg.get("installed_pv_watt", 5200))
  a_pv_pct = min(100.0, round((a_pv_w / eff_m_pv_max) * 100.0, 1)) if (eff_m_pv_max > 0 and a_pv_w > 0) else 0.0

  out_active = (a_out_v >= 180)
  a_load_pct = min(100.0, round((max(a_load_va, a_load_w) / eff_m_va) * 100.0, 1)) if (eff_m_va > 0 and out_active) else 0.0

  eff_grid_w = max(a_grid_w, w_a_bypass + w_a_grid_chg)
  eff_grid_a = round(eff_grid_w / a_grid_v, 1) if (a_grid_v > 50 and eff_grid_w > 12) else (a_grid_a if eff_grid_w > 12 else 0.0)
  max_grid_amps = float(m_cfg.get("max_grid_amps", 32.0))
  a_grid_pct = min(100.0, round((eff_grid_a / max_grid_amps) * 100.0, 1)) if (max_grid_amps > 0 and eff_grid_a > 0) else 0.0

  inv_out_a = a_out_a if (w_a_bypass == 0 and w_a_inv_out > 12) else (int(round(w_a_inv_out / a_out_v)) if (a_out_v > 50 and w_a_inv_out > 12) else 0)

  pwr_48v_txt = f"{abs(a_batt_w)}W | {abs(a_batt_a)}A" if abs(a_batt_w) > 12 else ""
  a_has_fault = bool(sa.get("has_fault", False)) or (sa.get("fault_msg", "No Fault") != "No Fault")
  a_real_warn = bool(sa.get("serious_warn", False))
  a_led_flt = "#ff1744" if a_has_fault else ("#ffd600" if a_real_warn else "#333")
  grid_active = (a_grid_v >= 100 or eff_grid_w > 12)
  pv1_active = (a_pv_w > 12)

  islands_payload = []
  online_islands_count = 0
  tot_island_pv_w = tot_island_load_w = tot_island_batt_w = 0
  tot_island_batt_a_raw = 0.0
  avail_cap_w = eff_m_max_w if (a_on and out_active) else 0.0
  temp_parts = [f"{a_max_temp}°C"] if a_on else []
  batt_v_samples = [a_batt_v] if (a_on and a_batt_v > 10.0) else []

  for idev in island_devs:
    si = idev.state if idev.online else {}
    i_on = bool(idev.online)
    if i_on: online_islands_count += 1
    i_pv_v = int(round(float(si.get("pv_input_volt") or 0)))
    i_pv_w = int(round(float(si.get("pv_input_watt") or 0)))
    i_pv_a = int(round(float(si.get("pv_current") or 0)))
    i_bus_v = int(round(float(si.get("bus_volt") or 0)))
    i_out_v = int(round(float(si.get("ac_out_volt") or 0)))
    i_out_f = round(float(si.get("ac_out_freq") or 0.0), 1)
    i_out_a = int(round(float(si.get("ac_out_amp") or 0)))
    i_load_w = int(round(float(si.get("ac_load_real_watt") or 0)))
    i_load_va = int(round(float(si.get("ac_load_va") or 0)))
    i_batt_v = round(float(si.get("batt_volt") or 0.0), 1)
    i_batt_a_raw = float(si.get("batt_current") or 0.0)
    i_batt_a = int(round(i_batt_a_raw))
    i_batt_w = int(round(float(si.get("batt_power_watt") or 0)))
    i_temp_inv = int(round(float(si.get("temp_inv") or 0)))

    island_active = (i_on and i_out_v >= 219)
    if i_on:
      tot_island_pv_w += i_pv_w; tot_island_load_w += i_load_w; tot_island_batt_w += i_batt_w; tot_island_batt_a_raw += i_batt_a_raw
      temp_parts.append(f"{i_temp_inv}°C")
      if i_batt_v > 10.0: batt_v_samples.append(i_batt_v)

    i_max_pv_v = float(idev.cfg.get("max_pv_volt", 450.0))
    i_max_bus_v = float(idev.cfg.get("max_bus_volt", 450.0))
    eff_i_va = float(si.get("hw_rated_va") or idev.cfg.get("rated_va") or idev.cfg.get("rated_watt") or 5500)
    eff_i_max_w = float(si.get("hw_rated_watt") or idev.cfg.get("rated_watt") or eff_i_va)
    if island_active:
      avail_cap_w += eff_i_max_w
    eff_i_pv_max = min(float(idev.cfg.get("installed_pv_watt", eff_i_max_w)), eff_i_max_w) if eff_i_max_w > 0 else float(idev.cfg.get("installed_pv_watt", 5500))
    i_pv_pct = min(100.0, round((i_pv_w / eff_i_pv_max) * 100.0, 1)) if (eff_i_pv_max > 0 and i_pv_w > 0) else 0.0
    i_load_pct = min(100.0, round((max(i_load_va, i_load_w) / eff_i_va) * 100.0, 1)) if (eff_i_va > 0 and island_active) else 0.0

    i_has_fault = (si.get("fault_msg", "No Fault") != "No Fault")
    i_led_flt = "#ff1744" if i_has_fault else ("#ffd600" if si.get("serious_warn", False) else "#333")
    pvi_active = (i_pv_w > 12)

    islands_payload.append({
      "id": idev.id, "short_name": idev.short_name, "online": i_on, "island_active": island_active,
      "pv_active": pvi_active, "pv_sun_on": (i_pv_v > 120), "pv_pct": i_pv_pct,
      "pv_top_txt": f"{i_pv_w}W | {i_pv_a}A" if pvi_active else "", "pv_bot_txt": f"{i_pv_v}V",
      "pv_high": (i_pv_v >= i_max_pv_v), "bus_v": f"{i_bus_v}V", "bus_high": (i_bus_v >= i_max_bus_v),
      "out_top_l": f"{i_load_w}W" if (island_active and i_load_w > 12) else "",
      "out_top_r": f"{i_out_a}A" if (island_active and i_load_w > 12) else "",
      "out_bot_l": f"{i_out_v}V" if island_active else "",
      "out_bot_r": f"{i_out_f:.1f}Hz" if island_active else "",
      "load_pct": i_load_pct, "w_pv": i_pv_w, "w_hv_inv": i_load_w if island_active else 0,
      "w_48v": abs(i_batt_w), "rev_48v": (i_batt_w < 0),
      "pwr_48v_txt": f"{abs(i_batt_w)}W | {abs(i_batt_a)}A" if abs(i_batt_w) > 12 else "",
      "led_inv_w": i_load_w if island_active else 0, "led_inv_on": island_active,
      "led_chg_w": max(0, i_batt_w), "led_flt": i_led_flt
    })

  avg_batt_v = round(sum(batt_v_samples) / len(batt_v_samples), 1) if batt_v_samples else 0.0
  batt_w = a_batt_w + tot_island_batt_w
  batt_a_raw = a_batt_a_raw + tot_island_batt_a_raw
  batt_a = int(round(batt_a_raw))

  max_chg_amps = float(config.get("max_battery_charge_amps", 120.0))
  max_dis_amps = float(config.get("max_battery_discharge_amps", 120.0))
  if abs(batt_w) <= 12 or abs(batt_a_raw) < 0.4:
    batt_signed_pct = 0.0
  elif batt_a_raw > 0:
    batt_signed_pct = min(100.0, round((batt_a_raw / max_chg_amps) * 100.0, 1)) if max_chg_amps > 0 else 0.0
  else:
    batt_signed_pct = -min(100.0, round((abs(batt_a_raw) / max_dis_amps) * 100.0, 1)) if max_dis_amps > 0 else 0.0

  show_soc_num = (a_btype in ("LI2", "LI4"))
  if show_soc_num:
    batt_bars = 4 if a_soc >= 85 else (3 if a_soc >= 60 else (2 if a_soc >= 35 else (1 if a_soc >= 10 else 0)))
  else:
    batt_bars = 4 if avg_batt_v >= 54.4 else (3 if avg_batt_v >= 53.6 else (2 if avg_batt_v >= 51.6 else (1 if avg_batt_v >= 50.0 else 0)))

  batt_sign_top = "+" if batt_w > 12 else ("-" if batt_w < -12 else "")
  if online_islands_count > 0 and a_batt_w < -15 and tot_island_batt_w > 15:
    xfer_txt = f"▲ {min(-a_batt_w, tot_island_batt_w)}W"
  elif online_islands_count > 0 and tot_island_batt_w < -15 and a_batt_w > 15:
    xfer_txt = f"▼ {min(a_batt_w, -tot_island_batt_w)}W"
  else:
    xfer_txt = ""

  avail_cap_kw = f"{avail_cap_w / 1000.0:.1f}".rstrip("0").rstrip(".") if avail_cap_w > 0 else "0"
  cards_html = "".join(render_device_card_html(d_id, dev) for d_id, dev in devices_map.items())

  with event_lock:
    log_lines = "\n".join(event_history) if event_history else "No events recorded yet."

  return {
    "mqtt": {
      "connected": mqtt_status["connected"], "msg": mqtt_status["rc_msg"],
      "host": f"{config.get('mqtt_host')}:{config.get('mqtt_port')}",
      "user": f"user '{config.get('mqtt_user')}'" if config.get("mqtt_user") else "anonymous"
    },
    "indicators": indicators_payload,
    "top": {
      "tot_pv_w": a_pv_w + tot_island_pv_w, "temp_lbl": "TEMPS" if len(temp_parts) > 1 else "TEMP",
      "temps_str": " / ".join(temp_parts) if temp_parts else "0°C",
      "batt_w": abs(batt_w), "batt_sign": batt_sign_top, "tot_load_w": a_load_w + tot_island_load_w,
      "cap_kw": avail_cap_kw
    },
    "main": {
      "short_name": main_dev.short_name if main_dev else "Main", "online": a_on, "is_gen": is_gen_mode,
      "grid_active": grid_active, "grid_pct": a_grid_pct,
      "grid_top_l": f"{eff_grid_w}W" if eff_grid_w > 12 else "",
      "grid_top_r": f"{eff_grid_a:.1f}A" if eff_grid_w > 12 else "",
      "grid_bot_l": f"{a_grid_v}V" if grid_active else "",
      "grid_bot_r": f"{a_grid_f:.1f}Hz" if grid_active else "",
      "grid_bot_idle": "" if grid_active else f"{a_grid_v}V",
      "pv_active": pv1_active, "pv_sun_on": (a_pv_v > 120), "pv_pct": a_pv_pct,
      "pv_top_txt": f"{a_pv_w}W | {a_pv_a}A" if pv1_active else "", "pv_bot_txt": f"{a_pv_v}V",
      "pv_high": (a_pv_v >= m_max_pv_v), "bus_v": f"{a_bus_v}V", "bus_high": (a_bus_v >= m_max_bus_v),
      "out_active": out_active,
      "out_top_l": f"{w_a_inv_out}W" if w_a_inv_out > 12 else "",
      "out_top_r": f"{inv_out_a}A" if w_a_inv_out > 12 else "",
      "out_bot_l": f"{a_out_v}V" if out_active else "",
      "out_bot_r": f"{a_out_f:.1f}Hz" if out_active else "",
      "out_bot_idle": "" if out_active else f"{a_out_v}V",
      "load_pct": a_load_pct, "w_bypass": w_a_bypass, "w_grid_chg": w_a_grid_chg, "gchg_lbl": gchg_lbl,
      "w_pv": a_pv_w, "w_hv_inv": w_a_inv_out,
      "w_48v": abs(a_batt_w), "rev_48v": (a_batt_w < 0), "pwr_48v_txt": pwr_48v_txt,
      "led_inv_w": a_load_w if out_active else 0, "led_inv_on": out_active,
      "led_chg_w": max(0, a_batt_w), "led_flt": a_led_flt
    },
    "batt": {
      "chem": batt_chem_tag, "soc": f"{a_soc}%" if show_soc_num else "",
      "avg_v_txt": f"{avg_batt_v:.1f}V" if avg_batt_v > 0 else "0.0V",
      "bars": batt_bars, "signed_pct": batt_signed_pct,
      "w": abs(batt_w), "a": abs(batt_a), "rev": (batt_w < 0),
      "xfer_txt": xfer_txt, "online_islands": online_islands_count
    },
    "islands": islands_payload, "cards_html": cards_html, "log_lines": log_lines
  }

def render_main_page_html() -> str:
  main_dev, island_devs = get_main_and_island_devices()
  main_short = main_dev.short_name if main_dev else "Anenji"

  bypass_emblem_svg = _svg_bypass_emblem()
  conv_m_acdc = _svg_converter_box("conv_m_acdc", 308, 78, "ac", "dc")
  conv_m_mppt = _svg_converter_box("conv_m_mppt", 308, 118, "dc", "dc")
  conv_m_dcac = _svg_converter_box("conv_m_dcac", 528, 96, "dc", "ac")
  conv_m_48v_corner = _svg_converter_box("conv_m_48v", 418, 172, "dc", "dc")
  hvdc_m_box = _svg_hvdc_bus_box(380, 84, main_short, "main_bus_v")

  grid_tower_svg = _svg_grid_tower_icon()
  gas_gen_svg = _svg_generator_icon()
  pv1_panel_svg = _svg_voltronic_pv_icon(45.0, 112.0)

  island_wings_svg = ""
  island_leds_html = ""
  for idx, idev in enumerate(island_devs):
    yb = 244 + (idx * 162)
    conv_i_48v_corner = _svg_converter_box(f"conv_i{idx}_48v", 418, yb - 20, "dc", "dc")
    conv_i_mppt = _svg_converter_box(f"conv_i{idx}_mppt", 308, yb + 56, "dc", "dc")
    conv_i_dcac = _svg_converter_box(f"conv_i{idx}_dcac", 528, yb + 56, "dc", "ac")
    hvdc_i_box = _svg_hvdc_bus_box(380, yb + 44, idev.short_name, f"isl{idx}_bus_v")
    pvi_panel_svg = _svg_voltronic_pv_icon(45.0, yb + 50.0)

    island_wings_svg += f"""
    <g id="wing_isl_{idx}" style="display:none;">
      <path id="path_i{idx}_48v_bot" d="M 445 {yb+44} L 445 {yb+12}" class="lcd-dot-idle" fill="none"/>
      {conv_i_48v_corner}
      <path id="path_i{idx}_48v_top" d="M 418 {yb-4} L 285 {yb-4} L 285 214" class="lcd-dot-idle" fill="none"/>
      <text id="lbl_i{idx}_48v_pwr" x="352" y="{yb+13}" text-anchor="middle" class="svg-flow-val" style="fill:#ffe082;"></text>
      <g id="isl{idx}_pv_bars" class="dpi-bar-l"></g>
      <g id="box_i{idx}_pv" class="dpi-ico-l">{pvi_panel_svg}</g>
      <path id="path_i{idx}_pv1" d="M 82 {yb+72} L 308 {yb+72}" class="lcd-dot-idle" fill="none"/>
      {conv_i_mppt}
      <path id="path_i{idx}_pv2" d="M 362 {yb+72} L 380 {yb+72}" class="lcd-dot-idle" fill="none"/>
      <text id="lbl_i{idx}_pv_top" x="195" y="{yb+63}" text-anchor="middle" class="svg-flow-val" style="fill:#ffe082;"></text>
      <text id="lbl_i{idx}_pv_bot" x="195" y="{yb+88}" text-anchor="middle" class="svg-line-sub"></text>
      {hvdc_i_box}
      <g id="isl{idx}_ac_grp" style="display:none;">
        <path id="path_i{idx}_inv1" d="M 510 {yb+72} L 528 {yb+72}" class="lcd-dot-idle" fill="none"/>
        {conv_i_dcac}
        <path id="path_i{idx}_inv2" d="M 582 {yb+72} L 880 {yb+72}" class="lcd-dot-idle" fill="none"/>
        <text id="lbl_i{idx}_out_top_l" x="723" y="{yb+63}" text-anchor="end" class="svg-flow-val" style="fill:#ffffff;"></text>
        <text id="lbl_i{idx}_out_top_p" x="731" y="{yb+63}" text-anchor="middle" class="svg-flow-val" style="fill:#ffffff;"></text>
        <text id="lbl_i{idx}_out_top_r" x="739" y="{yb+63}" text-anchor="start" class="svg-flow-val" style="fill:#ffffff;"></text>
        <text id="lbl_i{idx}_out_bot_l" x="723" y="{yb+88}" text-anchor="end" class="svg-line-sub"></text>
        <text id="lbl_i{idx}_out_bot_p" x="731" y="{yb+88}" text-anchor="middle" class="svg-line-sub"></text>
        <text id="lbl_i{idx}_out_bot_r" x="739" y="{yb+88}" text-anchor="start" class="svg-line-sub"></text>
        <g id="isl{idx}_load_bulb" class="dpi-ico-r"></g>
        <g id="isl{idx}_load_bars" class="dpi-bar-r"></g>
      </g>
    </g>"""

    island_leds_html += f"""
    <div id="led_grp_isl_{idx}" class="led-group" style="display:none;">
      <span class="led-title">{idev.short_name.upper()}:</span>
      <span id="led_i{idx}_inv" class="led-dot"></span> AC/INV
      <span id="led_i{idx}_chg" class="led-dot"></span> CHG
      <span id="led_i{idx}_flt" class="led-dot"></span> FAULT
    </div>"""

  return f"""<!DOCTYPE html><html><head><title>Solar Bridge v{BRIDGE_VERSION}</title>
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <style>
  body{{background:#121212;color:#e0e0e0;font-family:'Segoe UI',sans-serif;max-width:980px;margin:18px auto;padding:0 15px;}}
  textarea{{background:#1e1e1e;color:#80cbc4;border:1px solid #444;padding:10px;width:100%;border-radius:6px;font-family:monospace;font-size:13px;box-sizing:border-box;}}
  button{{background:#0288d1;color:#fff;border:none;padding:10px 18px;border-radius:4px;cursor:pointer;font-weight:bold;}}
  body.kiosk-mode{{max-width:none !important;width:100vw !important;height:100dvh !important;margin:0 !important;padding:0 !important;background:#0b1948 !important;overflow:hidden !important;}}
  body.kiosk-mode .non-kiosk{{display:none !important;}}
  body.kiosk-mode .lcd-bezel{{width:100vw !important;height:100dvh !important;max-width:none !important;margin:0 !important;padding:4px 8px !important;border-width:2px !important;border-radius:0 !important;box-sizing:border-box !important;display:flex !important;flex-direction:column !important;}}
  body.kiosk-mode .lcd-screen{{flex:1 !important;min-height:0 !important;display:flex !important;flex-direction:column !important;padding:6px 10px !important;box-sizing:border-box !important;}}
  body.kiosk-mode .lcd-svg{{flex:1 !important;min-height:0 !important;width:100% !important;height:100% !important;}}
  body.kiosk-mode .lcd-leds-bar{{padding-top:4px !important;flex-shrink:0 !important;}}
  .lcd-bezel{{background:#1b1b1b;border:4px solid #3a3a3a;border-radius:14px;padding:12px;margin-bottom:16px;box-shadow:0 8px 24px rgba(0,0,0,0.65);}}
  .lcd-screen{{--dpi-txt:1;--dpi-ico:1;--dpi-box:1;--dpi-lin:1;background:linear-gradient(180deg, #0a2578 0%, #103ca8 50%, #0b2982 100%);border:3px solid #7799cc;border-radius:8px;padding:8px 10px;box-shadow:inset 0 0 28px rgba(0,180,255,0.35);color:#fff;font-family:'Courier New',monospace;}}
  .lcd-topbar{{display:flex;justify-content:space-around;align-items:baseline;flex-wrap:nowrap;white-space:nowrap;border-bottom:1.5px solid rgba(160,216,255,0.35);padding-bottom:6px;margin-bottom:4px;gap:8px;overflow:hidden;}}
  .lcd-tag{{font-size:calc(10.5px * var(--dpi-txt,1));color:#99d5ff;font-weight:bold;letter-spacing:0.4px;}}
  .lcd-big{{font-size:calc(17px * var(--dpi-txt,1));font-weight:bold;color:#ffffff;text-shadow:0 0 8px rgba(160,225,255,0.9);margin:0 2px;}}
  .lcd-unit{{font-size:calc(11px * var(--dpi-txt,1));color:#b3e5fc;font-weight:bold;}}
  .lcd-svg{{width:100%;height:auto;display:block;}}
  .dpi-bar-l{{transform-box:fill-box;transform-origin:right center;transform:scale(var(--dpi-ico,1));}}
  .dpi-ico-l{{transform-box:fill-box;transform-origin:left center;transform:scale(var(--dpi-ico,1));}}
  .dpi-ico-r{{transform-box:fill-box;transform-origin:right center;transform:scale(var(--dpi-ico,1));}}
  .dpi-bar-r{{transform-box:fill-box;transform-origin:left center;transform:scale(var(--dpi-ico,1));}}
  .dpi-box{{transform-box:fill-box;transform-origin:center;transform:scale(var(--dpi-box,1));}}
  .kiosk-btn{{background:rgba(255,255,255,0.08);color:#99d5ff;border:1px solid rgba(153,213,255,0.4);padding:3px 9px;border-radius:4px;font-size:11px;font-family:'Courier New',monospace;cursor:pointer;font-weight:bold;}}
  .kiosk-btn:hover{{background:rgba(255,255,255,0.2);color:#fff;}}
  .dpi-panel{{display:none;flex-wrap:wrap;justify-content:center;align-items:center;gap:14px;background:#131c2e;border:1px solid #355280;border-radius:6px;padding:6px 12px;margin-top:6px;font-size:11.5px;color:#b3e5fc;font-family:'Courier New',monospace;}}
  .dpi-panel label{{display:flex;align-items:center;gap:5px;font-weight:bold;}}
  .dpi-panel input[type=range]{{width:78px;accent-color:#4fc3f7;cursor:pointer;}}
  .svg-idle-box{{opacity:0.28;filter:grayscale(75%);transition:opacity 0.25s ease;}}
  .svg-val{{fill:#ffffff;font-size:calc(17px * var(--dpi-txt,1));font-weight:bold;filter:drop-shadow(0px 0px 4px rgba(140,215,255,0.8));}}
  .svg-sub{{fill:#cceeff;font-size:calc(11px * var(--dpi-txt,1));font-weight:bold;}}
  .svg-flow-val{{fill:#e0f7fa;font-size:calc(13px * var(--dpi-txt,1));font-weight:bold;filter:drop-shadow(0 0 3px rgba(0,0,0,0.8));}}
  .svg-line-sub{{fill:#cceeff;font-size:calc(12.5px * var(--dpi-txt,1));font-weight:bold;filter:drop-shadow(0 0 3px rgba(0,0,0,0.8));}}
  .svg-bypass-font{{fill:rgba(255,255,255,0.34);font-size:calc(10.5px * var(--dpi-txt,1));font-weight:bold;letter-spacing:3.2px;}}
  .lcd-dot-idle{{stroke:rgba(255,255,255,0.14);stroke-width:calc(2px * var(--dpi-lin,1));stroke-dasharray:2 9;stroke-linecap:round;}}
  .lcd-dot-fwd{{stroke-dasharray:1 13;stroke-linecap:round;animation:dot-flow-fwd 1s linear infinite;filter:drop-shadow(0 0 4px rgba(255,255,255,0.6));}}
  .lcd-dot-rev{{stroke-dasharray:1 13;stroke-linecap:round;animation:dot-flow-rev 1s linear infinite;filter:drop-shadow(0 0 4px rgba(255,213,79,0.6));}}
  @keyframes dot-flow-fwd{{from{{stroke-dashoffset:28;}}to{{stroke-dashoffset:0;}}}}
  @keyframes dot-flow-rev{{from{{stroke-dashoffset:0;}}to{{stroke-dashoffset:28;}}}}
  .lcd-leds-bar{{display:flex;justify-content:space-around;align-items:center;padding-top:8px;font-size:11.5px;color:#bbb;font-weight:bold;letter-spacing:0.5px;}}
  .led-dot{{display:inline-block;width:10px;height:10px;border-radius:50%;margin:0 4px 0 10px;vertical-align:middle;border:1px solid #555;background:#333;transition:background 0.04s linear, box-shadow 0.04s linear;}}
  .led-title{{color:#8ec5ff;}}
  .ent-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(215px,1fr));gap:6px;}}
  .ent-cell{{background:#12151b;border:1px solid #282c34;border-radius:5px;padding:5px 8px;display:flex;flex-direction:column;}}
  .ent-lbl{{font-size:10.5px;color:#90a4ae;}}
  .ent-lbl small{{color:#607d8b;font-size:9.5px;}}
  .ent-val{{font-size:13px;font-weight:bold;font-family:monospace;margin-top:1px;}}
  </style>
  </head>
  <body>
  <h2 class="non-kiosk" style="margin-top:0;">⚡ Local Solar MQTT Bridge <span style='font-size:14px;color:#888;'>v{BRIDGE_VERSION}</span></h2>
  <div id="mqtt_banner" class="non-kiosk" style="background:#18222c;padding:10px 15px;border-radius:8px;margin-bottom:15px;border:1px solid #4caf50;">
    <b id="mqtt_title">📡 MQTT Broker:</b> <span id="mqtt_msg" style="font-weight:bold;">Connecting...</span>
  </div>

  <div class="lcd-bezel" ondblclick="if(document.body.classList.contains('kiosk-mode')) toggleKioskMode();">
    <div id="lcd_screen_box" class="lcd-screen">
      <div class="lcd-topbar">
        <div><span class="lcd-tag">TOTAL PV</span> <span id="top_pv" class="lcd-big">0</span><span class="lcd-unit">W</span></div>
        <div><span class="lcd-tag">BATT</span> <span id="top_batt_sign" class="lcd-big" style="margin-right:0;"></span><span id="top_batt_w" class="lcd-big" style="margin-left:0;">0</span><span class="lcd-unit">W</span></div>
        <div><span class="lcd-tag">TOTAL LOAD</span> <span id="top_load" class="lcd-big">0</span><span class="lcd-unit">W</span></div>
        <div><span class="lcd-tag">AC CAPACITY</span> <span id="top_cap" class="lcd-big">0</span><span class="lcd-unit">kW</span></div>
        <div><span id="top_temp_lbl" class="lcd-tag">TEMP</span> <span id="top_temps" class="lcd-big">0°C</span></div>
      </div>

      <svg id="lcd_svg" viewBox="0 0 960 264" preserveAspectRatio="xMidYMid meet" class="lcd-svg">
        <g id="grid_load_bars" class="dpi-bar-l"></g>
        <g id="box_m_grid" class="dpi-ico-l">{grid_tower_svg}{gas_gen_svg}</g>

        <g id="main_pv_bars" class="dpi-bar-l"></g>
        <g id="box_m_pv" class="dpi-ico-l">{pv1_panel_svg}</g>

        <path id="path_m_ac_in" d="M 82 70 L 285 70" class="lcd-dot-idle" fill="none"/>
        <text id="lbl_m_grid_top_l" x="179" y="61" text-anchor="end" class="svg-flow-val" style="fill:#ffffff;"></text>
        <text id="lbl_m_grid_top_p" x="187" y="61" text-anchor="middle" class="svg-flow-val" style="fill:#ffffff;"></text>
        <text id="lbl_m_grid_top_r" x="195" y="61" text-anchor="start" class="svg-flow-val" style="fill:#ffffff;"></text>
        <text id="lbl_m_grid_bot_l" x="179" y="86" text-anchor="end" class="svg-line-sub"></text>
        <text id="lbl_m_grid_bot_p" x="187" y="86" text-anchor="middle" class="svg-line-sub"></text>
        <text id="lbl_m_grid_bot_r" x="195" y="86" text-anchor="start" class="svg-line-sub"></text>
        <text id="lbl_m_grid_bot_idle" x="187" y="86" text-anchor="middle" class="svg-line-sub" style="fill:rgba(224,247,250,0.32);"></text>

        <path id="path_m_gchg1" d="M 285 70 L 285 94 L 308 94" class="lcd-dot-idle" fill="none"/>
        {conv_m_acdc}
        <path id="path_m_gchg2" d="M 362 94 L 380 94" class="lcd-dot-idle" fill="none"/>
        <text id="lbl_m_gchg" x="279" y="99" text-anchor="end" class="svg-line-sub" style="fill:#ffe082;font-size:calc(11px * var(--dpi-txt,1));"></text>

        {bypass_emblem_svg}
        <path id="path_m_bypass" d="M 285 70 L 362 70 A 83 24 0 0 1 528 70 L 880 70" class="lcd-dot-idle" fill="none"/>
        <circle id="ac_split_node" cx="285" cy="70" r="5.5" fill="#ffffff" stroke="#b3e5fc" stroke-width="1.3" class="dpi-box"/>

        <path id="path_m_pv1" d="M 82 134 L 308 134" class="lcd-dot-idle" fill="none"/>
        {conv_m_mppt}
        <path id="path_m_pv2" d="M 362 134 L 380 134" class="lcd-dot-idle" fill="none"/>
        <text id="lbl_m_pv_top" x="195" y="125" text-anchor="middle" class="svg-flow-val" style="fill:#ffe082;"></text>
        <text id="lbl_m_pv_bot" x="195" y="150" text-anchor="middle" class="svg-line-sub"></text>

        {hvdc_m_box}

        <path id="path_m_inv1" d="M 510 112 L 528 112" class="lcd-dot-idle" fill="none"/>
        {conv_m_dcac}
        <path id="path_m_inv2" d="M 582 112 L 880 112" class="lcd-dot-idle" fill="none"/>
        <text id="lbl_m_out_top_l" x="723" y="103" text-anchor="end" class="svg-flow-val" style="fill:#ffffff;"></text>
        <text id="lbl_m_out_top_p" x="731" y="103" text-anchor="middle" class="svg-flow-val" style="fill:#ffffff;"></text>
        <text id="lbl_m_out_top_r" x="739" y="103" text-anchor="start" class="svg-flow-val" style="fill:#ffffff;"></text>
        <text id="lbl_m_out_bot_l" x="723" y="128" text-anchor="end" class="svg-line-sub"></text>
        <text id="lbl_m_out_bot_p" x="731" y="128" text-anchor="middle" class="svg-line-sub"></text>
        <text id="lbl_m_out_bot_r" x="739" y="128" text-anchor="start" class="svg-line-sub"></text>
        <text id="lbl_m_out_bot_idle" x="731" y="128" text-anchor="middle" class="svg-line-sub" style="fill:rgba(224,247,250,0.32);"></text>

        <g id="main_load_bulb" class="dpi-ico-r"></g>
        <g id="main_load_bars" class="dpi-bar-r"></g>

        <path id="path_m_48v_top" d="M 445 140 L 445 198" class="lcd-dot-idle" fill="none"/>
        {conv_m_48v_corner}
        <path id="path_m_48v_bot" d="M 418 214 L 94 214" class="lcd-dot-idle" fill="none"/>
        <text id="lbl_m_48v_pwr" x="256" y="205" text-anchor="middle" class="svg-flow-val" style="fill:#ffe082;"></text>
        <text id="lbl_batt_avg_v" x="190" y="230" text-anchor="middle" class="svg-line-sub" style="fill:#ffe082;"></text>

        <g id="batt_load_bars" class="dpi-bar-l"></g>
        <g id="batt_group" class="dpi-ico-l">
          <rect x="47" y="186.5" width="32" height="4" rx="1.2" fill="#e0f7fa"/>
          <path d="M 35 210 L 35 196 Q 35 192.5 39 192.5 L 87 192.5 Q 91 192.5 91 196 L 91 210" fill="rgba(6,28,88,0.45)" stroke="#e0f7fa" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round"/>
          <path d="M 35 218 L 35 234.5 Q 35 238 39 238 L 87 238 Q 91 238 91 234.5 L 91 218" fill="rgba(6,28,88,0.45)" stroke="#e0f7fa" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round"/>
          <g id="batt_bars_grp"></g>
          <rect x="96" y="227" width="26" height="13" rx="2" fill="#e0f7fa"/>
          <text id="batt_chem_svg" x="109" y="237" text-anchor="middle" style="fill:#082268;font-size:calc(10px * var(--dpi-txt,1));font-weight:bold;letter-spacing:0.4px;">Li</text>
          <text id="batt_soc_svg" x="128" y="237" class="svg-sub" style="fill:#e0f7fa;"></text>
        </g>

        <g id="multi_node_grp" style="display:none;">
          <path id="path_net_batt" d="M 285 214 L 94 214" class="lcd-dot-idle" fill="none"/>
          <circle cx="285" cy="214" r="5.5" fill="#ffd54f" stroke="#fff" stroke-width="1.3" class="dpi-box"/>
          <text id="lbl_net_batt" x="190" y="205" text-anchor="middle" class="svg-flow-val" style="fill:#ffe082;"></text>
          <text id="lbl_xfer_chev" x="296" y="218" text-anchor="start" class="svg-flow-val" style="fill:#ffd54f;"></text>
        </g>

        {island_wings_svg}
      </svg>
    </div>

    <div id="lcd_dpi_panel" class="dpi-panel">
      <label>Text <input type="range" id="dpi_txt" min="0.70" max="1.40" step="0.05" value="1" oninput="updateLcdDpi()"><span id="dpi_txt_v">100%</span></label>
      <label>Icons/Bars <input type="range" id="dpi_ico" min="0.70" max="1.40" step="0.05" value="1" oninput="updateLcdDpi()"><span id="dpi_ico_v">100%</span></label>
      <label>Boxes/Nodes <input type="range" id="dpi_box" min="0.70" max="1.40" step="0.05" value="1" oninput="updateLcdDpi()"><span id="dpi_box_v">100%</span></label>
      <label>Lines <input type="range" id="dpi_lin" min="0.70" max="1.40" step="0.05" value="1" oninput="updateLcdDpi()"><span id="dpi_lin_v">100%</span></label>
      <button class="kiosk-btn" onclick="resetLcdDpi()">↺ Reset</button>
    </div>

    <div class="lcd-leds-bar">
      <div class="led-group">
        <span class="led-title">{main_short.upper()}:</span>
        <span id="led_m_inv" class="led-dot"></span> AC/INV
        <span id="led_m_chg" class="led-dot"></span> CHG
        <span id="led_m_flt" class="led-dot"></span> FAULT
      </div>
      {island_leds_html}
      <div style="display:flex;gap:6px;">
        <button class="kiosk-btn" onclick="toggleDpiPanel()">⚙ DPI</button>
        <button id="kiosk_toggle_btn" class="kiosk-btn" onclick="toggleKioskMode()">⛶ SCREEN ONLY</button>
      </div>
    </div>
  </div>

  <div id="device_cards" class="non-kiosk"></div>
  <div class="non-kiosk" style='background:#161616;padding:15px;border-radius:8px;margin-bottom:15px;border:1px solid #333;'>
    <h3 style='margin-top:0;font-size:15px;'>📋 Recent Disconnects, Writes & Events Log ({EVENT_LOG_FILE})</h3>
    <pre id="event_log_pre" style='background:#0d0d0d;color:#ffb74d;padding:10px;border-radius:6px;max-height:200px;overflow-y:auto;font-size:12px;margin:0;'>Loading...</pre>
  </div>
  <div class="non-kiosk">
    <h3>Bridge Configuration ({CONFIG_FILE})</h3>
    <form method="POST" action="/save">
      <textarea name="cfg" rows="28">{get_masked_config_json()}</textarea><br><br>
      <button type="submit">Save Configuration & Reconnect MQTT</button>
    </form>
  </div>

  <script>
  const DPI_KEY = 'solar_lcd_dpi_v40';
  function applyLcdDpi(cfg) {{
    const scr = document.getElementById('lcd_screen_box');
    if (!scr) return;
    for (const k of ['txt', 'ico', 'box', 'lin']) {{
      const v = parseFloat(cfg[k] || 1);
      scr.style.setProperty(`--dpi-${{k}}`, v);
      const inp = document.getElementById(`dpi_${{k}}`);
      const lbl = document.getElementById(`dpi_${{k}}_v`);
      if (inp) inp.value = v;
      if (lbl) lbl.textContent = Math.round(v * 100) + '%';
    }}
  }}
  function updateLcdDpi() {{
    const cfg = {{
      txt: parseFloat(document.getElementById('dpi_txt').value || 1),
      ico: parseFloat(document.getElementById('dpi_ico').value || 1),
      box: parseFloat(document.getElementById('dpi_box').value || 1),
      lin: parseFloat(document.getElementById('dpi_lin').value || 1)
    }};
    applyLcdDpi(cfg);
    try {{ localStorage.setItem(DPI_KEY, JSON.stringify(cfg)); }} catch (e) {{}}
  }}
  function resetLcdDpi() {{
    const cfg = {{ txt: 1, ico: 1, box: 1, lin: 1 }};
    applyLcdDpi(cfg);
    try {{ localStorage.removeItem(DPI_KEY); }} catch (e) {{}}
  }}
  function toggleDpiPanel() {{
    const p = document.getElementById('lcd_dpi_panel');
    if (p) p.style.display = (p.style.display === 'flex') ? 'none' : 'flex';
  }}
  try {{
    const savedDpi = JSON.parse(localStorage.getItem(DPI_KEY) || 'null');
    if (savedDpi) applyLcdDpi(savedDpi);
  }} catch (e) {{}}

  function applyKioskState(isKiosk) {{
    const btn = document.getElementById('kiosk_toggle_btn');
    document.body.classList.toggle('kiosk-mode', isKiosk);
    if (btn) btn.textContent = isKiosk ? '✕ EXIT' : '⛶ SCREEN ONLY';
  }}
  function toggleKioskMode() {{
    const nextState = !document.body.classList.contains('kiosk-mode');
    applyKioskState(nextState);
    try {{
      if (nextState && document.documentElement.requestFullscreen) document.documentElement.requestFullscreen().catch(() => {{}});
      else if (!nextState && document.fullscreenElement && document.exitFullscreen) document.exitFullscreen().catch(() => {{}});
    }} catch (e) {{}}
  }}
  document.addEventListener('fullscreenchange', () => {{
    if (!document.fullscreenElement && document.body.classList.contains('kiosk-mode')) applyKioskState(false);
  }});
  if (new URLSearchParams(window.location.search).get('kiosk') === '1') applyKioskState(true);

  function setText(id, val) {{
    const el = document.getElementById(id);
    if (el && el.textContent !== String(val)) el.textContent = String(val);
  }}
  function setBoxActive(id, isActive) {{
    const el = document.getElementById(id);
    if (el) el.classList.toggle('svg-idle-box', !isActive);
  }}

  function updateFlowLine(id, watts, reverse, color) {{
    const el = document.getElementById(id);
    if (!el) return;
    const w = Math.abs(watts || 0);
    if (w < 12) {{
      if (el.getAttribute('class') !== 'lcd-dot-idle') {{
        el.setAttribute('class', 'lcd-dot-idle');
        el.removeAttribute('style');
        delete el.dataset.dur;
      }}
      return;
    }}
    const targetClass = reverse ? 'lcd-dot-rev' : 'lcd-dot-fwd';
    if (el.getAttribute('class') !== targetClass) el.setAttribute('class', targetClass);
    const dur = Math.max(0.20, Math.min(2.20, 32.0 / (Math.sqrt(w) + 8.0)));
    const sw = Math.min(5.0, 2.6 + (w / 2500.0));
    const prevDur = parseFloat(el.dataset.dur || '0');
    if (Math.abs(dur - prevDur) > 0.15) {{
      el.style.animationDuration = dur.toFixed(2) + 's';
      el.dataset.dur = dur.toFixed(2);
    }}
    el.style.stroke = color;
    el.style.strokeWidth = `calc(${{sw.toFixed(1)}}px * var(--dpi-lin, 1))`;
  }}

  function buildWedgeInnerSvg(hTotal, pct) {{
    const p = Math.max(0, Math.min(100, pct || 0));
    const col = '#e0f7fa', wTop = 15.5, wBot = 4.0, pad = 2.0;
    const yInnerBot = hTotal - pad;
    const hFill = (p / 100.0) * (hTotal - pad * 2);
    const yFillTop = (yInnerBot - hFill).toFixed(1);
    const xFillL = '2.0';
    const xFillBotR = (wBot - 1.5).toFixed(1);
    const xFillTopR = (parseFloat(xFillBotR) + (p / 100.0) * ((wTop - 1.8) - parseFloat(xFillBotR))).toFixed(1);
    let s = `<path d="M 5 0 L 0 0 L 0 ${{hTotal}} L 2.0 ${{hTotal}}" fill="none" stroke="${{col}}" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>`;
    s += `<path d="M ${{wTop-4.5}} 0 L ${{wTop}} 0 L ${{wBot}} ${{hTotal}} L ${{wBot-1.5}} ${{hTotal}}" fill="none" stroke="${{col}}" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>`;
    if (p > 0) {{
      const fillCol = p >= 85 ? '#ff5252' : col;
      s += `<polygon points="${{xFillL}},${{yFillTop}} ${{xFillTopR}},${{yFillTop}} ${{xFillBotR}},${{yInnerBot}} ${{xFillL}},${{yInnerBot}}" fill="${{fillCol}}"/>`;
    }}
    return s;
  }}

  function renderGridWedge(groupId, xSpine, yTop, hTotal, pct, visible) {{
    const g = document.getElementById(groupId);
    if (!g) return;
    if (!visible) {{ if (g.innerHTML !== '') g.innerHTML = ''; return; }}
    const p = Math.max(0, Math.min(100, pct || 0));
    const html = `<g opacity="${{p > 0 ? 1.0 : 0.35}}" transform="translate(${{xSpine}}, ${{yTop}}) scale(-1, 1)">${{buildWedgeInnerSvg(hTotal, p)}}</g>`;
    if (g.innerHTML !== html) g.innerHTML = html;
  }}

  function renderLoadWedge(groupId, xSpine, yTop, hTotal, pct, visible) {{
    const g = document.getElementById(groupId);
    if (!g) return;
    if (!visible) {{ if (g.innerHTML !== '') g.innerHTML = ''; return; }}
    const p = Math.max(0, Math.min(100, pct || 0));
    const html = `<g opacity="${{p > 0 ? 1.0 : 0.35}}" transform="translate(${{xSpine}}, ${{yTop}})">${{buildWedgeInnerSvg(hTotal, p)}}</g>`;
    if (g.innerHTML !== html) g.innerHTML = html;
  }}

  function renderSolarRays(groupId, cy, pct, sunOn, visible) {{
    const g = document.getElementById(groupId);
    if (!g) return;
    if (!visible) {{ if (g.innerHTML !== '') g.innerHTML = ''; return; }}
    const p = Math.max(0, Math.min(100, pct || 0));
    const col = '#e0f7fa', dim = 'rgba(224,247,250,0.16)';
    const thresholds = [1, 20, 40, 60, 80];
    const angles = [56, 28, 0, -28, -56];
    let rays = '';
    for (let i = 0; i < 5; i++) {{
      const rad = angles[i] * Math.PI / 180.0;
      const x1 = (12 + Math.cos(rad) * 8.5).toFixed(1), y1 = (cy + Math.sin(rad) * 8.5).toFixed(1);
      const x2 = (12 + Math.cos(rad) * 16.5).toFixed(1), y2 = (cy + Math.sin(rad) * 16.5).toFixed(1);
      const c = p >= thresholds[i] ? col : dim;
      rays += `<line x1="${{x1}}" y1="${{y1}}" x2="${{x2}}" y2="${{y2}}" stroke="${{c}}" stroke-width="2.1" stroke-linecap="round"/>`;
    }}
    const sunFill = sunOn ? col : 'rgba(224,247,250,0.12)';
    const sunStroke = sunOn ? col : 'rgba(224,247,250,0.28)';
    const html = `<circle cx="12" cy="${{cy}}" r="5.3" fill="${{sunFill}}" stroke="${{sunStroke}}" stroke-width="1.4"/>${{rays}}`;
    if (g.innerHTML !== html) g.innerHTML = html;
  }}

  function renderVoltronicLoadBulb(bulbGroupId, xRight, yCenter, isTwisted, isActive) {{
    const g = document.getElementById(bulbGroupId);
    if (!g) return;
    const yOrigin = (yCenter - 29.0 * 1.15).toFixed(1);
    const col = '#e0f7fa', bx = -22, by = 1;
    let tubesSvg = !isTwisted ? `
      <path d="M ${{bx+7.5}} ${{by+8}} A 4.8 4.2 0 0 1 ${{bx+17}} ${{by+8}} L ${{bx+17}} ${{by+28}} A 2.2 1.4 0 0 1 ${{bx+12.6}} ${{by+28}} L ${{bx+12.6}} ${{by+10}}" fill="rgba(224,247,250,0.18)" stroke="${{col}}" stroke-width="1.5" stroke-linejoin="round"/>
      <path d="M ${{bx+3.5}} ${{by+28.5}} L ${{bx+3.5}} ${{by+10.5}} A 4.5 4.2 0 0 1 ${{bx+12.5}} ${{by+10.5}} L ${{bx+12.5}} ${{by+29.5}} A 2.2 1.4 0 0 1 ${{bx+8.1}} ${{by+29.5}} L ${{bx+8.1}} ${{by+11.5}} A 0.8 0.8 0 0 0 ${{bx+6.5}} ${{by+11.5}} L ${{bx+6.5}} ${{by+28.5}} A 1.5 1.1 0 0 1 ${{bx+3.5}} ${{by+28.5}} Z" fill="rgba(224,247,250,0.32)" stroke="${{col}}" stroke-width="1.5" stroke-linejoin="round"/>
    ` : `
      <path d="M ${{bx+4.8}} ${{by+28}} L ${{bx+4.2}} ${{by+22.5}} L ${{bx+7.5}} ${{by+21.5}} L ${{bx+8.0}} ${{by+28}}" fill="rgba(224,247,250,0.28)" stroke="${{col}}" stroke-width="1.4" stroke-linejoin="round"/>
      <path d="M ${{bx+12.5}} ${{by+28}} L ${{bx+13.2}} ${{by+20.5}} L ${{bx+16.5}} ${{by+19.5}} L ${{bx+15.6}} ${{by+28}}" fill="rgba(224,247,250,0.28)" stroke="${{col}}" stroke-width="1.4" stroke-linejoin="round"/>
      <path d="M ${{bx+15.5}} ${{by+20.5}} A 2.2 2.2 0 0 0 ${{bx+15.5}} ${{by+16.2}}" fill="none" stroke="${{col}}" stroke-width="1.4"/>
      <path d="M ${{bx+15.5}} ${{by+16.0}} A 2.2 2.2 0 0 0 ${{bx+15.5}} ${{by+11.7}}" fill="none" stroke="${{col}}" stroke-width="1.4"/>
      <path d="M ${{bx+15.2}} ${{by+11.5}} A 2.2 2.2 0 0 0 ${{bx+14.8}} ${{by+7.2}}" fill="none" stroke="${{col}}" stroke-width="1.4"/>
      <g transform="translate(${{bx+10.2}}, ${{by+19.5}}) rotate(-15)"><rect x="-7.5" y="-2.1" width="15" height="4.2" rx="2.1" fill="rgba(224,247,250,0.34)" stroke="${{col}}" stroke-width="1.45"/></g>
      <g transform="translate(${{bx+10.2}}, ${{by+15.0}}) rotate(-15)"><rect x="-7.5" y="-2.1" width="15" height="4.2" rx="2.1" fill="rgba(224,247,250,0.34)" stroke="${{col}}" stroke-width="1.45"/></g>
      <g transform="translate(${{bx+10.2}}, ${{by+10.5}}) rotate(-15)"><rect x="-7.2" y="-2.1" width="14.4" height="4.2" rx="2.1" fill="rgba(224,247,250,0.34)" stroke="${{col}}" stroke-width="1.45"/></g>
      <g transform="translate(${{bx+10.2}}, ${{by+6.2}}) rotate(-13)"><rect x="-5.5" y="-2.0" width="11.0" height="4.0" rx="2.0" fill="rgba(224,247,250,0.34)" stroke="${{col}}" stroke-width="1.45"/></g>
    `;
    const html = `
      <g transform="translate(${{xRight}}, ${{yOrigin}}) scale(1.15)" opacity="${{isActive ? 1.0 : 0.35}}">
        ${{tubesSvg}}
        <path d="M ${{bx+3.5}} ${{by+25.5}} A 8.5 3.6 0 1 0 ${{bx+17}} ${{by+25.5}}" fill="none" stroke="${{col}}" stroke-width="1.5"/>
        <path d="M ${{bx+1.8}} ${{by+27}} L ${{bx+1.8}} ${{by+36}} C ${{bx+1.8}} ${{by+42.5}}, ${{bx+18.7}} ${{by+42.5}}, ${{bx+18.7}} ${{by+36}} L ${{bx+18.7}} ${{by+27}}" fill="rgba(224,247,250,0.16)" stroke="${{col}}" stroke-width="1.6" stroke-linejoin="round"/>
        <path d="M ${{bx+1.8}} ${{by+27}} C ${{bx+1.8}} ${{by+31.8}}, ${{bx+18.7}} ${{by+31.8}}, ${{bx+18.7}} ${{by+27}}" fill="none" stroke="${{col}}" stroke-width="1.5"/>
        <rect x="${{bx+5.8}}" y="${{by+41}}" width="9" height="2.6" rx="1.2" fill="rgba(224,247,250,0.25)" stroke="${{col}}" stroke-width="1.3"/>
        <rect x="${{bx+6.2}}" y="${{by+43.6}}" width="8.2" height="2.5" rx="1.2" fill="rgba(224,247,250,0.25)" stroke="${{col}}" stroke-width="1.3"/>
        <rect x="${{bx+6.8}}" y="${{by+46.1}}" width="7" height="2.4" rx="1.1" fill="rgba(224,247,250,0.25)" stroke="${{col}}" stroke-width="1.3"/>
        <path d="M ${{bx+7.8}} ${{by+48.5}} C ${{bx+7.8}} ${{by+51}}, ${{bx+12.8}} ${{by+51}}, ${{bx+12.8}} ${{by+48.5}} Z" fill="${{col}}" stroke="${{col}}" stroke-width="1.1"/>
      </g>`;
    if (g.innerHTML !== html) g.innerHTML = html;
  }}

  function renderBattBars(barsCount) {{
    const gBars = document.getElementById('batt_bars_grp');
    if (!gBars) return;
    let bHtml = '';
    const yCoords = [226.6, 216.5, 206.4, 196.3];
    for (let i = 0; i < 4; i++) {{
      const fill = i < barsCount ? '#e0f7fa' : 'rgba(224,247,250,0.15)';
      bHtml += `<rect x="40" y="${{yCoords[i]}}" width="46" height="7.6" rx="1.1" fill="${{fill}}"/>`;
    }}
    if (gBars.innerHTML !== bHtml) gBars.innerHTML = bHtml;
  }}

  const battGauge = {{ visible: true, targetPct: 0, currPct: 0, targetShape: -1, currShape: -1, lastHtml: '' }};
  function stepBattGauge(dt) {{
    const g = document.getElementById('batt_load_bars');
    if (!g) return;
    if (!battGauge.visible) {{
      if (battGauge.lastHtml !== '') {{ g.innerHTML = ''; battGauge.lastHtml = ''; }}
      return;
    }}
    const k = 1.0 - Math.exp(-dt * 7.5);
    if (Math.abs(battGauge.currPct - battGauge.targetPct) > 0.05) battGauge.currPct += (battGauge.targetPct - battGauge.currPct) * k;
    else battGauge.currPct = battGauge.targetPct;
    if (Math.abs(battGauge.currShape - battGauge.targetShape) > 0.004) battGauge.currShape += (battGauge.targetShape - battGauge.currShape) * k;
    else battGauge.currShape = battGauge.targetShape;

    const hTotal = 48.0, s = battGauge.currShape, col = '#e0f7fa';
    const wTop = 9.75 - 5.75 * s;
    const wBot = 9.75 + 5.75 * s;
    const lTS = (wTop * 0.30).toFixed(1), lTO = (wTop * 0.28).toFixed(1);
    const lBS = (wBot * 0.30).toFixed(1), lBO = (wBot * 0.28).toFixed(1);

    const absP = Math.min(100, Math.abs(battGauge.currPct));
    const op = absP > 0.3 ? '1.0' : '0.35';
    let poly = '';
    if (absP > 0.3) {{
      const yMin = 2.0, yMax = hTotal - 2.0;
      let y1, y2;
      if (battGauge.currPct > 0) {{
        y2 = yMax;
        y1 = yMax - (absP / 100.0) * (yMax - yMin);
      }} else {{
        y1 = yMin;
        y2 = yMin + (absP / 100.0) * (yMax - yMin);
      }}
      const xAt = (y) => Math.max(2.2, (wTop + (y / hTotal) * (wBot - wTop)) - 1.6).toFixed(1);
      const fillCol = absP >= 85 ? '#ff5252' : col;
      poly = `<polygon points="2.0,${{y1.toFixed(1)}} ${{xAt(y1)}},${{y1.toFixed(1)}} ${{xAt(y2)}},${{y2.toFixed(1)}} 2.0,${{y2.toFixed(1)}}" fill="${{fillCol}}"/>`;
    }}
    const html = `<g opacity="${{op}}" transform="translate(28, 190) scale(-1, 1)">` +
      `<path d="M ${{lTS}} 0 L 0 0 L 0 ${{hTotal}} L ${{lBS}} ${{hTotal}}" fill="none" stroke="${{col}}" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>` +
      `<path d="M ${{(wTop - parseFloat(lTO)).toFixed(1)}} 0 L ${{wTop.toFixed(1)}} 0 L ${{wBot.toFixed(1)}} ${{hTotal}} L ${{(wBot - parseFloat(lBO)).toFixed(1)}} ${{hTotal}}" fill="none" stroke="${{col}}" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>` +
      `${{poly}}</g>`;
    if (battGauge.lastHtml !== html) {{ g.innerHTML = html; battGauge.lastHtml = html; }}
  }}

  const meterLeds = {{}};
  let lastPulseTick = performance.now();
  function configureMeterLed(id, watts, isOnlineIdle) {{
    if (!meterLeds[id]) meterLeds[id] = {{ w: 0, idle: false, acc: 0, flashUntil: 0 }};
    meterLeds[id].w = Math.max(0, watts || 0);
    meterLeds[id].idle = Boolean(isOnlineIdle);
  }}
  function setStaticFaultLed(id, color) {{
    const el = document.getElementById(id);
    if (!el) return;
    el.style.background = color;
    el.style.boxShadow = color === '#333' ? 'none' : `0 0 9px ${{color}}`;
  }}
  function tickFrame(now) {{
    const dt = Math.min(0.25, Math.max(0.0, (now - lastPulseTick) / 1000.0));
    lastPulseTick = now;
    stepBattGauge(dt);
    for (const id in meterLeds) {{
      const st = meterLeds[id], el = document.getElementById(id);
      if (!el) continue;
      if (st.w >= 12) {{
        st.acc += (st.w * dt) / 3600.0;
        if (st.acc >= 1.0) {{ st.acc -= Math.floor(st.acc); st.flashUntil = now + 75.0; }}
        el.style.background = now < st.flashUntil ? '#00e676' : 'rgba(0, 230, 118, 0.16)';
        el.style.boxShadow = now < st.flashUntil ? '0 0 11px #00e676, 0 0 4px #ffffff' : 'none';
      }} else {{
        st.acc = 0;
        el.style.background = st.idle ? 'rgba(0, 230, 118, 0.28)' : '#333';
        el.style.boxShadow = 'none';
      }}
    }}
    requestAnimationFrame(tickFrame);
  }}
  requestAnimationFrame(tickFrame);

  async function pollData() {{
    try {{
      const r = await fetch('/api/data');
      if (!r.ok) return;
      const d = await r.json();
      const ind = d.indicators || {{}};

      const mb = document.getElementById('mqtt_banner');
      const mcol = d.mqtt.connected ? '#4caf50' : '#f44336';
      mb.style.borderColor = mcol;
      setText('mqtt_title', `📡 MQTT Broker (${{d.mqtt.host}}, ${{d.mqtt.user}}):`);
      const mm = document.getElementById('mqtt_msg');
      mm.style.color = mcol;
      setText('mqtt_msg', d.mqtt.msg);

      setText('top_pv', d.top.tot_pv_w);
      setText('top_batt_sign', d.top.batt_sign);
      setText('top_batt_w', d.top.batt_w);
      setText('top_load', d.top.tot_load_w);
      setText('top_cap', d.top.cap_kw);
      setText('top_temp_lbl', d.top.temp_lbl);
      setText('top_temps', d.top.temps_str);

      const gTower = document.getElementById('icon_grid_tower');
      const gGen = document.getElementById('icon_gas_gen');
      if (gTower && gGen) {{
        gTower.style.display = d.main.is_gen ? 'none' : 'block';
        gGen.style.display = d.main.is_gen ? 'block' : 'none';
      }}

      renderGridWedge('grid_load_bars', 28, 48, 47, d.main.grid_pct, ind.show_grid_bar);
      renderSolarRays('main_pv_bars', 134, d.main.pv_pct, d.main.pv_sun_on, ind.show_solar_bar);

      battGauge.visible = Boolean(ind.show_battery_bar);
      battGauge.targetPct = d.batt.signed_pct || 0;
      if (battGauge.targetPct > 0.2) battGauge.targetShape = 1.0;
      else if (battGauge.targetPct < -0.2) battGauge.targetShape = -1.0;

      setText('lbl_m_grid_top_l', d.main.grid_top_l);
      setText('lbl_m_grid_top_p', d.main.grid_top_l ? '|' : '');
      setText('lbl_m_grid_top_r', d.main.grid_top_r);
      setText('lbl_m_grid_bot_l', d.main.grid_bot_l);
      setText('lbl_m_grid_bot_p', d.main.grid_bot_l ? '|' : '');
      setText('lbl_m_grid_bot_r', d.main.grid_bot_r);
      setText('lbl_m_grid_bot_idle', d.main.grid_bot_idle);

      setText('lbl_m_pv_top', d.main.pv_top_txt);
      setText('lbl_m_pv_bot', d.main.pv_bot_txt);
      const pvBotEl = document.getElementById('lbl_m_pv_bot');
      if (pvBotEl) pvBotEl.style.fill = d.main.pv_high ? '#ff5252' : (d.main.pv_active ? '#ffe082' : 'rgba(224,247,250,0.32)');

      setText('main_bus_v', d.main.bus_v);
      const mBusEl = document.getElementById('main_bus_v');
      if (mBusEl) {{
        mBusEl.style.fill = d.main.bus_high ? '#ff5252' : '#ffffff';
        mBusEl.style.filter = d.main.bus_high ? 'drop-shadow(0 0 6px rgba(255,23,68,0.9))' : 'drop-shadow(0px 0px 4px rgba(140,215,255,0.8))';
      }}

      setText('lbl_m_out_top_l', d.main.out_top_l);
      setText('lbl_m_out_top_p', d.main.out_top_l ? '|' : '');
      setText('lbl_m_out_top_r', d.main.out_top_r);
      setText('lbl_m_out_bot_l', d.main.out_bot_l);
      setText('lbl_m_out_bot_p', d.main.out_bot_l ? '|' : '');
      setText('lbl_m_out_bot_r', d.main.out_bot_r);
      setText('lbl_m_out_bot_idle', d.main.out_bot_idle);

      renderVoltronicLoadBulb('main_load_bulb', 913, 91, false, d.main.out_active || d.main.load_pct > 0);
      renderLoadWedge('main_load_bars', 922, 67, 48, d.main.load_pct, ind.show_load_bar);

      const wAcCommon = d.main.w_bypass + d.main.w_grid_chg;
      updateFlowLine('path_m_ac_in', wAcCommon, false, '#ffffff');
      updateFlowLine('path_m_gchg1', d.main.w_grid_chg, false, '#ffffff');
      updateFlowLine('path_m_gchg2', d.main.w_grid_chg, false, '#ffd54f');
      setText('lbl_m_gchg', d.main.gchg_lbl);

      updateFlowLine('path_m_bypass', d.main.w_bypass, false, '#ffffff');
      updateFlowLine('path_m_pv1', d.main.w_pv, false, '#ffd54f');
      updateFlowLine('path_m_pv2', d.main.w_pv, false, '#ffd54f');
      updateFlowLine('path_m_inv1', d.main.w_hv_inv, false, '#ffd54f');
      updateFlowLine('path_m_inv2', d.main.w_hv_inv, false, '#ffffff');

      setBoxActive('box_m_grid', d.main.grid_active);
      setBoxActive('ac_split_node', d.main.grid_active);
      setBoxActive('box_m_pv', d.main.pv_active);
      setBoxActive('conv_m_acdc', d.main.w_grid_chg > 12);
      setBoxActive('conv_m_mppt', d.main.w_pv > 12);
      setBoxActive('conv_m_dcac', d.main.w_hv_inv > 12);
      setBoxActive('conv_m_48v', d.main.w_48v > 12);

      const hasOnlineIsland = d.batt.online_islands > 0;
      const anyIslandActive48v = d.islands.some(isl => isl.online && isl.w_48v > 12);
      const pTop = document.getElementById('path_m_48v_top');
      const pBot = document.getElementById('path_m_48v_bot');
      const c48 = document.getElementById('conv_m_48v');
      const pNet = document.getElementById('path_net_batt');
      const mNode = document.getElementById('multi_node_grp');
      const lblMPwr = document.getElementById('lbl_m_48v_pwr');
      const lblAvgV = document.getElementById('lbl_batt_avg_v');

      setText('lbl_batt_avg_v', d.batt.avg_v_txt);

      if (!hasOnlineIsland) {{
        if (pTop.getAttribute('d') !== 'M 445 140 L 445 198') pTop.setAttribute('d', 'M 445 140 L 445 198');
        if (pBot.getAttribute('d') !== 'M 418 214 L 94 214') pBot.setAttribute('d', 'M 418 214 L 94 214');
        if (c48.getAttribute('transform') !== 'translate(0, 26)') c48.setAttribute('transform', 'translate(0, 26)');
        mNode.style.display = 'none';
        lblMPwr.setAttribute('x', '256'); lblMPwr.setAttribute('y', '205');
        if (lblAvgV) lblAvgV.setAttribute('x', '256');
      }} else {{
        if (pTop.getAttribute('d') !== 'M 445 140 L 445 172') pTop.setAttribute('d', 'M 445 140 L 445 172');
        if (pBot.getAttribute('d') !== 'M 418 188 L 285 188 L 285 214') pBot.setAttribute('d', 'M 418 188 L 285 188 L 285 214');
        if (c48.getAttribute('transform') !== 'translate(0, 0)') c48.setAttribute('transform', 'translate(0, 0)');
        if (pNet.getAttribute('d') !== 'M 285 214 L 94 214') pNet.setAttribute('d', 'M 285 214 L 94 214');
        mNode.style.display = 'block';
        lblMPwr.setAttribute('x', '352'); lblMPwr.setAttribute('y', '179');
        if (lblAvgV) lblAvgV.setAttribute('x', '190');
        updateFlowLine('path_net_batt', d.batt.w, d.batt.rev, '#ffd54f');
        setText('lbl_net_batt', (anyIslandActive48v && d.batt.w > 12) ? `${{d.batt.w}}W | ${{d.batt.a}}A` : '');
        setText('lbl_xfer_chev', d.batt.xfer_txt);
      }}

      updateFlowLine('path_m_48v_top', d.main.w_48v, d.main.rev_48v, '#ffd54f');
      updateFlowLine('path_m_48v_bot', d.main.w_48v, d.main.rev_48v, '#ffd54f');
      setText('lbl_m_48v_pwr', d.main.pwr_48v_txt);

      renderBattBars(d.batt.bars);
      setText('batt_chem_svg', d.batt.chem);
      setText('batt_soc_svg', d.batt.soc);

      configureMeterLed('led_m_inv', d.main.led_inv_w, d.main.led_inv_on);
      configureMeterLed('led_m_chg', d.main.led_chg_w, false);
      setStaticFaultLed('led_m_flt', d.main.led_flt);

      let activeWings = 0;
      for (let idx = 0; idx < d.islands.length; idx++) {{
        const isl = d.islands[idx];
        const wEl = document.getElementById(`wing_isl_${{idx}}`);
        const lEl = document.getElementById(`led_grp_isl_${{idx}}`);
        if (!wEl) continue;
        if (!isl.online) {{
          wEl.style.display = 'none';
          if (lEl) lEl.style.display = 'none';
        }} else {{
          activeWings++;
          wEl.style.display = 'block';
          if (lEl) lEl.style.display = 'block';
          const yb = 244 + (idx * 162);
          renderSolarRays(`isl${{idx}}_pv_bars`, yb + 72, isl.pv_pct, isl.pv_sun_on, ind.show_solar_bar);
          setText(`lbl_i${{idx}}_pv_top`, isl.pv_top_txt);
          setText(`lbl_i${{idx}}_pv_bot`, isl.pv_bot_txt);
          const iPvBotEl = document.getElementById(`lbl_i${{idx}}_pv_bot`);
          if (iPvBotEl) iPvBotEl.style.fill = isl.pv_high ? '#ff5252' : (isl.pv_active ? '#ffe082' : 'rgba(224,247,250,0.32)');

          setText(`isl${{idx}}_bus_v`, isl.bus_v);
          const iBusEl = document.getElementById(`isl${{idx}}_bus_v`);
          if (iBusEl) {{
            iBusEl.style.fill = isl.bus_high ? '#ff5252' : '#ffffff';
            iBusEl.style.filter = isl.bus_high ? 'drop-shadow(0 0 6px rgba(255,23,68,0.9))' : 'drop-shadow(0px 0px 4px rgba(140,215,255,0.8))';
          }}

          updateFlowLine(`path_i${{idx}}_pv1`, isl.w_pv, false, '#ffd54f');
          updateFlowLine(`path_i${{idx}}_pv2`, isl.w_pv, false, '#ffd54f');
          updateFlowLine(`path_i${{idx}}_48v_top`, isl.w_48v, isl.rev_48v, '#ffd54f');
          updateFlowLine(`path_i${{idx}}_48v_bot`, isl.w_48v, isl.rev_48v, '#ffd54f');
          setText(`lbl_i${{idx}}_48v_pwr`, isl.pwr_48v_txt);

          setBoxActive(`box_i${{idx}}_pv`, isl.pv_active);
          setBoxActive(`conv_i${{idx}}_mppt`, isl.w_pv > 12);
          setBoxActive(`conv_i${{idx}}_48v`, isl.w_48v > 12);
          setBoxActive(`conv_i${{idx}}_dcac`, isl.w_hv_inv > 12);

          const acGrp = document.getElementById(`isl${{idx}}_ac_grp`);
          if (!isl.island_active) {{
            acGrp.style.display = 'none';
          }} else {{
            acGrp.style.display = 'block';
            setText(`lbl_i${{idx}}_out_top_l`, isl.out_top_l);
            setText(`lbl_i${{idx}}_out_top_p`, isl.out_top_l ? '|' : '');
            setText(`lbl_i${{idx}}_out_top_r`, isl.out_top_r);
            setText(`lbl_i${{idx}}_out_bot_l`, isl.out_bot_l);
            setText(`lbl_i${{idx}}_out_bot_p`, isl.out_bot_l ? '|' : '');
            setText(`lbl_i${{idx}}_out_bot_r`, isl.out_bot_r);
            renderVoltronicLoadBulb(`isl${{idx}}_load_bulb`, 913, yb + 72, true, true);
            renderLoadWedge(`isl${{idx}}_load_bars`, 922, yb + 48, 48, isl.load_pct, ind.show_load_bar);
            updateFlowLine(`path_i${{idx}}_inv1`, isl.w_hv_inv, false, '#ffd54f');
            updateFlowLine(`path_i${{idx}}_inv2`, isl.w_hv_inv, false, '#ffffff');
          }}
          configureMeterLed(`led_i${{idx}}_inv`, isl.led_inv_w, isl.led_inv_on);
          configureMeterLed(`led_i${{idx}}_chg`, isl.led_chg_w, false);
          setStaticFaultLed(`led_i${{idx}}_flt`, isl.led_flt);
        }}
      }}

      const svg = document.getElementById('lcd_svg');
      const targetH = activeWings === 0 ? 264 : (244 + activeWings * 156);
      const targetVB = `0 0 960 ${{targetH}}`;
      if (svg.getAttribute('viewBox') !== targetVB) svg.setAttribute('viewBox', targetVB);

      const dc = document.getElementById('device_cards');
      if (dc.innerHTML !== d.cards_html) dc.innerHTML = d.cards_html;
      setText('event_log_pre', d.log_lines);
    }} catch (e) {{}}
  }}

  pollData();
  setInterval(pollData, 1500);
  </script></body></html>"""

class BridgeWebHandler(BaseHTTPRequestHandler):
  def log_message(self, format, *args): pass

  def do_GET(self):
    if self.path == "/api/data":
      self.send_response(200)
      self.send_header("Content-type", "application/json; charset=utf-8")
      self.end_headers()
      self.wfile.write(json.dumps(build_api_data()).encode('utf-8'))
      return
    self.send_response(200)
    self.send_header("Content-type", "text/html; charset=utf-8")
    self.end_headers()
    self.wfile.write(render_main_page_html().encode('utf-8'))

  def do_POST(self):
    if self.path == "/save":
      length = int(self.headers.get('Content-Length', 0))
      body = self.rfile.read(length).decode('utf-8')
      params = parse_qs(body)
      if "cfg" in params:
        try:
          new_cfg = json.loads(params["cfg"][0])
          global config
          if new_cfg.get("mqtt_pass") == PASSWORD_MASK:
            new_cfg["mqtt_pass"] = config.get("mqtt_pass", "")
          mqtt_changed = (
            new_cfg.get("mqtt_host") != config.get("mqtt_host") or
            int(new_cfg.get("mqtt_port", 1883)) != int(config.get("mqtt_port", 1883)) or
            new_cfg.get("mqtt_user", "") != config.get("mqtt_user", "") or
            new_cfg.get("mqtt_pass", "") != config.get("mqtt_pass", "")
          )
          save_config(new_cfg)
          config = new_cfg
          for d_cfg in config.get("devices", []):
            if d_cfg.get("id") in devices_map:
              devices_map[d_cfg["id"]].cfg = d_cfg
          if mqtt_changed or not mqtt_status["connected"]:
            reconnect_mqtt()
        except Exception as e:
          log_event("WEB", f"Config save error: {e}")
    self.send_response(303)
    self.send_header("Location", "/")
    self.end_headers()

def reconnect_mqtt():
  global mqtt_client
  with mqtt_op_lock:
    try:
      mqtt_status["connected"] = False
      mqtt_status["rc_msg"] = f"Connecting to {config['mqtt_host']}:{config['mqtt_port']}..."
      mqtt_client.loop_stop()
      if config.get("mqtt_user"):
        mqtt_client.username_pw_set(config["mqtt_user"], config.get("mqtt_pass", ""))
      else:
        mqtt_client.username_pw_set(None, None)
      mqtt_client.connect_async(config["mqtt_host"], int(config["mqtt_port"]), 60)
      mqtt_client.loop_start()
    except Exception as e:
      mqtt_status["rc_msg"] = f"Connect Error: {e}"

def main():
  global config, mqtt_client
  config = load_config()
  log_event("SYSTEM", f"Starting Unified Solar MQTT Bridge v{BRIDGE_VERSION}")

  mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
  if config.get("mqtt_user"):
    mqtt_client.username_pw_set(config["mqtt_user"], config.get("mqtt_pass", ""))

  def on_connect(client, userdata, flags, rc, properties=None):
    code = rc.value if hasattr(rc, 'value') else int(rc)
    if code == 0:
      mqtt_status["connected"] = True
      mqtt_status["rc_msg"] = "CONNECTED (Discovery Published to Home Assistant)"
      log_event("MQTT", f"Connected to broker {config['mqtt_host']}:{config['mqtt_port']}")
      client.subscribe("solar/+/set/+")
      for dev in devices_map.values():
        dev.publish_discovery()
        dev.publish_availability(dev.online, "MQTT Reconnected")
    else:
      mqtt_status["connected"] = False
      mqtt_status["rc_msg"] = RC_MAP.get(code, f"Connection Refused (Code {code})")
      log_event("MQTT", f"Auth/Connect Failed: {mqtt_status['rc_msg']}")

  def on_disconnect(client, userdata, flags, rc, properties=None):
    code = rc.value if hasattr(rc, 'value') else int(rc)
    mqtt_status["connected"] = False
    if code != 0:
      mqtt_status["rc_msg"] = RC_MAP.get(code, f"Disconnected (Code {code})")

  def on_message(client, userdata, msg):
    parts = msg.topic.split("/")
    if len(parts) == 4 and parts[0] == "solar" and parts[2] == "set":
      dev_id, prop = parts[1], parts[3]
      if dev_id in devices_map:
        devices_map[dev_id].handle_mqtt_command(prop, msg.payload.decode('utf-8').strip())

  mqtt_client.on_connect = on_connect
  mqtt_client.on_disconnect = on_disconnect
  mqtt_client.on_message = on_message
  mqtt_client.connect_async(config["mqtt_host"], int(config["mqtt_port"]), 60)
  mqtt_client.loop_start()

  for d_cfg in config.get("devices", []):
    dev = AnenjiDevice(d_cfg, mqtt_client) if d_cfg.get("type") == "anenji_tcp" else EasunDevice(d_cfg, mqtt_client)
    devices_map[dev.id] = dev
    threading.Thread(target=dev.run_worker, daemon=True).start()

  def handle_exit(signum, frame):
    log_event("SYSTEM", "Stopping service, saving energy data...")
    for dev in devices_map.values():
      dev.save_energy()
      dev.publish_availability(False, "Service stopped")
    mqtt_client.loop_stop()
    sys.exit(0)

  signal.signal(signal.SIGTERM, handle_exit)
  signal.signal(signal.SIGINT, handle_exit)

  web = HTTPServer(('0.0.0.0', int(config.get("web_port", 8080))), BridgeWebHandler)
  web.serve_forever()

if __name__ == "__main__":
  main()
