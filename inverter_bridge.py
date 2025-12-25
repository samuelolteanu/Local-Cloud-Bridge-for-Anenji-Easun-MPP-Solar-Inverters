import socket
import threading
import struct
import time
import json
import os
import signal
import sys

# --- CONFIGURATION ---
INVERTER_PORT = 18899
LOCAL_CONTROL_PORT = 9999
BIND_IP = '0.0.0.0'
POLL_INTERVAL = 1.0 
INVERTER_RATED_WATT = 6200 
OFFLINE_THRESHOLD = 10 

# --- ENERGY MIGRATION ---
ENERGY_FILE = "/root/inverter_energy.json"
SAVE_INTERVAL = 300 

# --- STATUS MAPS ---
STATUS_MAP = {
    0: "Power On", 1: "Standby", 2: "Line Mode (On-Grid)",
    3: "Off-Grid (Battery)", 4: "Bypass", 5: "Charging", 6: "Fault"
}

BATTERY_TYPE_MAP = {
    0: "AGN", 1: "FLD", 2: "USR", 4: "LI2", 6: "LI4", 8: "LIb"
}

# --- FAULT CODES (Mapped from Manual) ---
FAULT_BIT_MAP = {
    1: "F01: Over temp inverter", 2: "F02: Over temp DCDC", 3: "F03: Batt volt high",
    4: "F04: Over temp PV", 5: "F05: Output short", 6: "F06: Output volt high",
    7: "F07: Overload timeout", 8: "F08: Bus volt high", 9: "F09: Bus soft start fail",
    10: "F10: PV over current", 11: "F11: PV over volt", 12: "F12: DCDC over current",
    13: "F13: Over current/surge", 14: "F14: Bus volt low", 15: "F15: Inverter fail",
    18: "F18: Op current offset", 19: "F19: Inv current offset", 20: "F20: DCDC current offset",
    21: "F21: PV current offset", 22: "F22: Output volt low", 23: "F23: Inv negative power"
}

# --- WARNING CODES (Mapped from Manual) ---
WARNING_BIT_MAP = {
    0: "W01: Grid Offline", 2: "W02: Temp high", 4: "W04: Low battery",
    7: "W07: Overload", 10: "W10: Power derating", 14: "W14: Fan blocked",
    15: "W15: PV energy low", 19: "W19: BMS Comms Fail", 21: "W21: BMS Over Current"
}

# --- SHARED STATE ---
current_inverter_conn = None
modbus_lock = threading.Lock()
last_cmd_time = 0 
energy_lock = threading.Lock()

# --- ENERGY MANAGEMENT ---
def load_or_create_energy_data():
    default_structure = {
        "total_pv_kwh": 0.0, "total_grid_input_kwh": 0.0,
        "total_load_kwh": 0.0, "total_battery_charge_kwh": 0.0,
        "total_battery_discharge_kwh": 0.0
    }
    if os.path.exists(ENERGY_FILE):
        try:
            with open(ENERGY_FILE, 'r') as f:
                data = json.load(f)
                for key in default_structure:
                    if key not in data: data[key] = default_structure[key]
                
                # --- PRINT ONLY AT STARTUP ---
                print(f"[*] Loaded energy data:")
                print(f"    PV: {data['total_pv_kwh']:.2f} kWh")
                print(f"    Grid Input: {data['total_grid_input_kwh']:.2f} kWh")
                print(f"    Load: {data['total_load_kwh']:.2f} kWh")
                print(f"    Batt Charge: {data['total_battery_charge_kwh']:.2f} kWh")
                print(f"    Batt Discharge: {data['total_battery_discharge_kwh']:.2f} kWh")
                return data
        except: return default_structure.copy()
    else: return default_structure.copy()

energy_data = load_or_create_energy_data()

def save_energy_to_disk():
    with energy_lock:
        try:
            with open(ENERGY_FILE + ".tmp", 'w') as f:
                json.dump(energy_data, f, indent=2)
            os.replace(ENERGY_FILE + ".tmp", ENERGY_FILE)
        except Exception as e: print(f"[!] Energy Save Failed: {e}")

# --- HELPER: DECODE BITMASKS ---
def decode_flags(val, map_dict, prefix="Unknown"):
    active_list = []
    if val == 0: return []
    for bit in range(32):
        if (val >> bit) & 1:
            msg = map_dict.get(bit, f"{prefix} (Bit {bit})")
            active_list.append(msg)
    return active_list

def get_empty_data():
    return {
        "fault_code": 0, "fault_msg": "No Fault", "fault_list": [],
        "warning_code": 0, "warning_msg": "No Warning", "warning_list": [],
        "device_status_code": None, "device_status_msg": "Initializing...", 
        "batt_volt": None, "ac_load_real_watt": None, "ac_load_pct": None,
        "batt_power_watt": None, "grid_power_watt": None, "ac_output_amp": None, 
        "pv_input_watt": None, "pv_input_volt": None, "batt_soc": None, 
        "temp_dc": None, "temp_inv": None, "max_total_amps": None, 
        "max_ac_amps": None, "batt_current": None, "grid_volt": None,
        "grid_freq": None, "ac_out_volt": None, "charger_priority": 3, 
        "output_mode": 0, "ac_input_range": 0, "buzzer_mode": 3,
        "backlight_status": 1, "soc_back_to_grid": 100, "soc_back_to_batt": 100, 
        "soc_cutoff": 0, "battery_type_code": None, "battery_type_msg": None,
        "total_pv_energy_kwh": round(energy_data["total_pv_kwh"], 4),
        "total_grid_input_kwh": round(energy_data["total_grid_input_kwh"], 4),
        "total_load_kwh": round(energy_data["total_load_kwh"], 4),
        "total_battery_charge_kwh": round(energy_data["total_battery_charge_kwh"], 4),
        "total_battery_discharge_kwh": round(energy_data["total_battery_discharge_kwh"], 4)
    }

latest_data_json = get_empty_data()

# --- MODBUS PROTOCOL ---
def modbus_crc(data):
    crc = 0xFFFF
    for pos in data:
        crc ^= pos
        for i in range(8):
            if (crc & 1) != 0: crc >>= 1; crc ^= 0xA001
            else: crc >>= 1
    return struct.pack('<H', crc)

def to_signed(val):
    return val - 65536 if val >= 32768 else val

def build_write_packet(reg, value):
    payload = struct.pack('>BBHHB', 1, 16, reg, 1, 2) + struct.pack('>H', value)
    return payload + modbus_crc(payload)

def build_read_packet(start, count):
    payload = struct.pack('>BBHH', 1, 3, start, count)
    return payload + modbus_crc(payload)

def flush_buffer(conn):
    try:
        conn.settimeout(0.01)
        while conn.recv(1024): pass
    except: pass
    finally: conn.settimeout(2.5)

def read_modbus_response(conn):
    try:
        raw = conn.recv(1024)
        if len(raw) < 5 or raw[1] & 0x80: return None
        if modbus_crc(raw[:-2]) != raw[-2:]: return None
        return [x[0] for x in struct.iter_unpack('>H', raw[3 : 3 + raw[2]])]
    except: return None

# --- MAIN SERVER ---
def inverter_server():
    global current_inverter_conn, latest_data_json, last_cmd_time
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((BIND_IP, INVERTER_PORT))
    s.listen(1)

    consecutive_failures = 0
    loop_counter = 0
    last_integration_time = time.time()
    last_save_time = time.time()

    while True:
        try:
            print("[*] Waiting for Inverter connection...")
            conn, addr = s.accept()
            current_inverter_conn = conn
            conn.settimeout(5.0) 
            print(f"[*] Inverter connected from {addr[0]}:{addr[1]}")
            
            # --- HANDSHAKE ---
            try:
                print("[*] Sending Handshake (AT+DTUPN?)...")
                conn.send(b'AT+DTUPN?\r\n')
                conn.recv(1024)
                time.sleep(0.5)
                print("[*] Handshake OK.")
            except: 
                print("[!] Handshake Failed.")
                conn.close(); continue
            
            conn.settimeout(2.5)
            consecutive_failures = 0
            
            while True:
                now = time.time()
                time_delta = now - last_integration_time
                last_integration_time = now

                with modbus_lock:
                    try:
                        flush_buffer(conn)
                        
                        # 1. READ SENSORS (200-239)
                        conn.send(build_read_packet(200, 40))
                        time.sleep(0.15) 
                        vals = read_modbus_response(conn)

                        if vals is None:
                            consecutive_failures += 1
                            if consecutive_failures >= OFFLINE_THRESHOLD: break
                        else:
                            consecutive_failures = 0
                            dev_status = vals[1]
                            v_batt = vals[15] / 10.0
                            batt_p = -(-vals[9] if vals[9] > 0 else to_signed(vals[8]))
                            v_pv = vals[19] / 10.0; p_pv = vals[23]
                            v_grid = vals[2] / 10.0; p_grid = vals[4]
                            p_load = vals[13]

                            # Energy Integration
                            if time_delta > 0 and time_delta < 5.0:
                                with energy_lock:
                                    if p_pv > 0: energy_data["total_pv_kwh"] += (p_pv * time_delta) / 3600000.0
                                    if p_grid > 0: energy_data["total_grid_input_kwh"] += (p_grid * time_delta) / 3600000.0
                                    if p_load > 0: energy_data["total_load_kwh"] += (p_load * time_delta) / 3600000.0
                                    if batt_p > 0: energy_data["total_battery_charge_kwh"] += (batt_p * time_delta) / 3600000.0
                                    elif batt_p < 0: energy_data["total_battery_discharge_kwh"] += (abs(batt_p) * time_delta) / 3600000.0
                                    latest_data_json["total_pv_energy_kwh"] = round(energy_data["total_pv_kwh"], 4)

                            if (now - last_save_time) > SAVE_INTERVAL:
                                save_energy_to_disk(); last_save_time = now

                            batt_current = -round(abs(batt_p)/v_batt, 1) if (v_batt > 0 and batt_p < 0) else (round(batt_p/v_batt, 1) if v_batt > 0 else 0)
                            
                            latest_data_json.update({
                                "device_status_code": dev_status,
                                "device_status_msg": STATUS_MAP.get(dev_status, f"Unknown Code {dev_status}"),
                                "grid_volt": v_grid, "grid_freq": vals[3] / 100.0,
                                "grid_power_watt": p_grid, "grid_current": round(p_grid / v_grid, 1) if v_grid > 0 else 0.0,
                                "ac_out_volt": vals[5] / 10.0, "ac_output_amp": vals[11] / 10.0,
                                "ac_load_real_watt": p_load, "ac_load_pct": round(min((vals[14]/INVERTER_RATED_WATT)*100, 300), 1),
                                "batt_volt": v_batt, "batt_soc": vals[29], "batt_power_watt": batt_p, 
                                "batt_current": batt_current, "pv_input_watt": p_pv, "pv_input_volt": v_pv,
                                "pv_current": round(p_pv / v_pv, 2) if v_pv > 0 else 0.0,
                                "temp_dc": vals[27], "temp_inv": vals[26], "inverter_temp": vals[26]
                            })
                            
                            # 2. READ FAULTS & WARNINGS (100-111)
                            if loop_counter % 2 == 0:
                                conn.send(build_read_packet(100, 12))
                                time.sleep(0.1)
                                vf = read_modbus_response(conn)
                                if vf and len(vf) >= 10:
                                    fault_val = (vf[0] << 16) | vf[1]
                                    warn_val = (vf[8] << 16) | vf[9]
                                    
                                    f_list = decode_flags(fault_val, FAULT_BIT_MAP, "Unknown F")
                                    w_list = decode_flags(warn_val, WARNING_BIT_MAP, "Unknown W")

                                    latest_data_json.update({
                                        "fault_code": fault_val, "fault_msg": ", ".join(f_list) if f_list else "No Fault", "fault_list": f_list,
                                        "warning_code": warn_val, "warning_msg": ", ".join(w_list) if w_list else "No Warning", "warning_list": w_list
                                    })

                            # 3. READ SETTINGS (300 series)
                            is_cooldown = (time.time() - last_cmd_time) < 10.0
                            if (loop_counter % 5 == 0) and (not is_cooldown):
                                conn.send(build_read_packet(301, 6)); time.sleep(0.1); v300 = read_modbus_response(conn)
                                if v300:
                                    latest_data_json.update({
                                        "output_mode": v300[0], "ac_input_range": v300[1], "buzzer_mode": v300[2], 
                                        "backlight_status": v300[4], "return_to_default": v300[5]
                                    })
                                conn.send(build_read_packet(331, 3)); time.sleep(0.1); v330 = read_modbus_response(conn)
                                if v330:
                                    latest_data_json.update({
                                        "charger_priority": v330[0], "max_total_amps": v330[1] / 10.0, "max_ac_amps": v330[2] / 10.0
                                    })
                                conn.send(build_read_packet(341, 3)); time.sleep(0.1); vsoc = read_modbus_response(conn)
                                if vsoc:
                                    latest_data_json.update({
                                        "soc_back_to_grid": vsoc[0], "soc_back_to_batt": vsoc[1], "soc_cutoff": vsoc[2]
                                    })
                                conn.send(build_read_packet(322, 1)); time.sleep(0.1); v322 = read_modbus_response(conn)
                                if v322:
                                    latest_data_json.update({
                                        "battery_type_code": v322[0], "battery_type_msg": BATTERY_TYPE_MAP.get(v322[0], f"Unknown ({v322[0]})")
                                    })

                    except Exception:
                        consecutive_failures += 1
                        if consecutive_failures >= OFFLINE_THRESHOLD: break
                loop_counter += 1
                time.sleep(POLL_INTERVAL)
        except: 
            print("[!] Connection lost/failed. Retrying...")
            time.sleep(1)
        finally:
            if current_inverter_conn: current_inverter_conn.close(); current_inverter_conn = None
            latest_data_json = get_empty_data()

def control_server():
    global latest_data_json, last_cmd_time
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((BIND_IP, LOCAL_CONTROL_PORT))
    s.listen(5)
    while True:
        try:
            client, _ = s.accept()
            req = client.recv(1024).strip().decode().upper()
            if req == "JSON": client.send(json.dumps(latest_data_json).encode())
            elif current_inverter_conn and req:
                cmd_packet = None
                if req.startswith("MODE_"): cmd_packet = build_write_packet(301, int(req.split("_")[1])); latest_data_json["output_mode"] = int(req.split("_")[1])
                elif req.startswith("SET_AC_RANGE_"): cmd_packet = build_write_packet(302, int(req.split("_")[3])); latest_data_json["ac_input_range"] = int(req.split("_")[3])
                elif req == "CSO_SET": cmd_packet = build_write_packet(331, 1); latest_data_json["charger_priority"] = 1
                elif req == "SNU_SET" or req == "CHARGE_ON": cmd_packet = build_write_packet(331, 2); latest_data_json["charger_priority"] = 2
                elif req == "OSO_SET" or req == "CHARGE_OFF": cmd_packet = build_write_packet(331, 3); latest_data_json["charger_priority"] = 3
                elif req.startswith("SET_AMPS_"): cmd_packet = build_write_packet(333, int(req.split("_")[2]) * 10); latest_data_json["max_ac_amps"] = int(req.split("_")[2])
                elif req.startswith("SET_TOTAL_AMPS_"): cmd_packet = build_write_packet(332, int(req.split("_")[3]) * 10); latest_data_json["max_total_amps"] = int(req.split("_")[3])
                elif req.startswith("SET_SOC_GRID_"): cmd_packet = build_write_packet(341, int(req.split("_")[3])); latest_data_json["soc_back_to_grid"] = int(req.split("_")[3])
                elif req.startswith("SET_SOC_BATT_"): cmd_packet = build_write_packet(342, int(req.split("_")[3])); latest_data_json["soc_back_to_batt"] = int(req.split("_")[3])
                elif req.startswith("SET_SOC_CUTOFF_"): cmd_packet = build_write_packet(343, int(req.split("_")[3])); latest_data_json["soc_cutoff"] = int(req.split("_")[3])
                elif req.startswith("SET_BUZZER_"): cmd_packet = build_write_packet(303, int(req.split("_")[2])); latest_data_json["buzzer_mode"] = int(req.split("_")[2])
                elif req.startswith("SET_BACKLIGHT_"): cmd_packet = build_write_packet(305, int(req.split("_")[2])); latest_data_json["backlight_status"] = int(req.split("_")[2])
                elif req.startswith("SET_RETURN_DEFAULT_"): cmd_packet = build_write_packet(306, int(req.split("_")[3])); latest_data_json["return_to_default"] = int(req.split("_")[3])
                elif req.startswith("SET_BATTERY_TYPE_"): cmd_packet = build_write_packet(322, int(req.split("_")[3])); latest_data_json["battery_type_code"] = int(req.split("_")[3])
                
                if cmd_packet:
                    with modbus_lock: flush_buffer(current_inverter_conn); current_inverter_conn.send(cmd_packet); last_cmd_time = time.time()
                    client.send(b"OK")
            client.close()
        except: pass

def handle_exit(signum, frame):
    save_energy_to_disk(); sys.exit(0)

if __name__ == "__main__":
    signal.signal(signal.SIGTERM, handle_exit); signal.signal(signal.SIGINT, handle_exit)
    t1 = threading.Thread(target=inverter_server, daemon=True); t2 = threading.Thread(target=control_server, daemon=True)
    t1.start(); t2.start()
    while True: time.sleep(1)
