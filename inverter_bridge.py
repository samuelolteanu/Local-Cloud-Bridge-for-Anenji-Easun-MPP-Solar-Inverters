import socket
import threading
import struct
import time
import json
import os
import traceback

# --- CONFIGURATION ---
INVERTER_PORT = 18899
LOCAL_CONTROL_PORT = 9999
BIND_IP = '0.0.0.0'
POLL_INTERVAL = 1.0 
INVERTER_RATED_WATT = 6200 

# --- TRANSLATION MAPS ---

# FAULT CODES (Reg 101 - Numeric)
FAULT_MAP = {
    0:  "No Fault",
    1:  "Over temperature of inverter module",
    2:  "Over temperature of DCDC module",
    3:  "Battery voltage is too high",
    4:  "Over temperature of PV module",
    5:  "Output short circuited",
    6:  "Output voltage is too high",
    7:  "Overload time out",
    8:  "Bus voltage is too high",
    9:  "Bus soft start failed",
    10: "PV over current",
    11: "PV over voltage",
    12: "DCDC over current",
    13: "Over current or surge",
    14: "Bus voltage is too low",
    15: "Inverter failed (Self-checking)",
    18: "Op current offset is too high",
    19: "Inverter current offset is too high", 
    20: "DC/DC current offset is too high",
    21: "PV current offset is too high",
    22: "Output voltage is too low",
    23: "Inverter negative power",
    51: "Over Current Inverter",
    52: "Bus Voltage Too Low",
    53: "Inverter Soft Start Failed",
    55: "Over DC Voltage in AC Output",
    56: "Battery Connection Open",
    57: "Current Sensor Failed",
    58: "Output Voltage Too Low",
    99: "Unknown Fault"
}

# STATUS MAP (Reg 201)
STATUS_MAP = {
    0: "Standby / Power Off",
    1: "Fault Mode",           
    2: "Line Mode (On-Grid)",  
    3: "Battery Mode",         
    4: "Bypass / Warning Mode", 
    5: "Power Saving Mode",
    6: "Online Mode",
    7: "Bypass Mode",
    8: "Digital Bypass",
    9: "Eco Mode"
}

# --- SHARED STATE ---
current_inverter_conn = None
modbus_lock = threading.Lock()
last_cmd_time = 0 

latest_data_json = {
    "fault_code": 0,
    "fault_msg": "No Fault",      
    "warning_code": 0,            
    "warning_msg": "No Warning",  
    "device_status_code": 0,
    "device_status_msg": "Standby",
    "fault_bitmask": 0,
    "warning_bitmask": 0,
    "charger_priority": 3,
    "output_mode": 0,
    "ac_input_range": 0,
    "buzzer_mode": 3,
    "backlight_status": 1,
    "return_to_default": 0,
    "batt_volt": 0,
    "ac_load_va": 0,
    "ac_load_real_watt": 0,
    "ac_load_pct": 0,
    "batt_power_watt": 0,
    "grid_power_watt": 0,
    "ac_output_amp": 0,
    "pv_input_watt": 0,
    "pv_input_volt": 0,
    "pv_current": 0,
    "batt_soc": 0,
    "temp_dc": 0,
    "temp_inv": 0,
    "max_ac_amps": 0,
    "soc_back_to_grid": 100,  
    "soc_back_to_batt": 100,
    "soc_cutoff": 0,
    "batt_current": 0
}

def modbus_crc(data):
    crc = 0xFFFF
    for pos in data:
        crc ^= pos
        for i in range(8):
            if (crc & 1) != 0:
                crc >>= 1
                crc ^= 0xA001
            else:
                crc >>= 1
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
    finally: conn.settimeout(2.0)

def read_modbus_response(conn):
    try:
        raw = conn.recv(1024)
        if len(raw) < 5: return None
        if raw[1] & 0x80: return None
        
        payload = raw[:-2]
        crc_recv = raw[-2:]
        if modbus_crc(payload) != crc_recv: return None

        byte_count = raw[2]
        if len(raw) < (3 + byte_count): return None
        
        data = raw[3 : 3 + byte_count]
        return [x[0] for x in struct.iter_unpack('>H', data)]
    except:
        return None

def inverter_server():
    global current_inverter_conn, latest_data_json, last_cmd_time
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((BIND_IP, INVERTER_PORT))
    s.listen(1)
    print(f"[*] Waiting for Inverter on {INVERTER_PORT}...")

    loop_counter = 0

    while True:
        try:
            conn, addr = s.accept()
            print(f"[+] Connected: {addr}")
            current_inverter_conn = conn
            conn.settimeout(2.0)
            
            while True:
                with modbus_lock:
                    try:
                        # 1. READ SENSORS
                        flush_buffer(conn) 
                        conn.send(build_read_packet(200, 40))
                        time.sleep(0.1) 
                        vals = read_modbus_response(conn)

                        # 2. READ FAULT/WARNING (Block 100)
                        vals_fault = None
                        if loop_counter % 2 == 0:
                            flush_buffer(conn)
                            conn.send(build_read_packet(100, 6)) 
                            time.sleep(0.1)
                            vals_fault = read_modbus_response(conn)

                        # 3. READ SETTINGS
                        is_cooldown = (time.time() - last_cmd_time) < 10.0
                        if (loop_counter % 5 == 0) and (not is_cooldown):
                            # Step 3a: Read Main Settings
                            flush_buffer(conn)
                            conn.send(build_read_packet(301, 5))
                            time.sleep(0.1)
                            vals_300 = read_modbus_response(conn)
                            
                            # Step 3b: Read Auto-Return (306) Separately
                            flush_buffer(conn)
                            conn.send(build_read_packet(306, 1))
                            time.sleep(0.1)
                            vals_306 = read_modbus_response(conn)

                            flush_buffer(conn)
                            conn.send(build_read_packet(331, 1))
                            time.sleep(0.1)
                            vals_prio = read_modbus_response(conn)

                            flush_buffer(conn)
                            conn.send(build_read_packet(333, 1))
                            time.sleep(0.1)
                            vals_amps = read_modbus_response(conn)

                            flush_buffer(conn)
                            conn.send(build_read_packet(341, 3))
                            time.sleep(0.1)
                            vals_soc = read_modbus_response(conn)

                            if vals_300 and len(vals_300) >= 5:
                                latest_data_json["output_mode"] = vals_300[0]
                                latest_data_json["ac_input_range"] = vals_300[1]
                                latest_data_json["buzzer_mode"] = vals_300[2]
                                latest_data_json["backlight_status"] = vals_300[4]

                            if vals_306:
                                latest_data_json["return_to_default"] = vals_306[0]

                            if vals_prio: latest_data_json["charger_priority"] = vals_prio[0]
                            if vals_amps: latest_data_json["max_ac_amps"] = vals_amps[0] / 10.0
                            if vals_soc and len(vals_soc) >= 3:
                                latest_data_json["soc_back_to_grid"] = vals_soc[0]
                                latest_data_json["soc_back_to_batt"] = vals_soc[1]
                                latest_data_json["soc_cutoff"] = vals_soc[2]

                        # --- UPDATE SENSORS JSON ---
                        if vals_fault and len(vals_fault) >= 6:
                            try:
                                status_code = vals[1] if vals and len(vals) >= 2 else 0
                                
                                # PRE-CALCULATE BATTERY ACTIVITY for Physics Check
                                batt_is_active = False
                                if vals and len(vals) >= 10:
                                    p_discharge = vals[8]
                                    p_charge = vals[9]
                                    # If we are discharging (>0) or charging (>0), battery is connected
                                    if p_discharge > 0 or p_charge > 0 or status_code == 3:
                                        batt_is_active = True

                                # 1. FAULTS
                                numeric_fault = vals_fault[1] 
                                latest_data_json["fault_code"] = numeric_fault
                                
                                # 2. WARNINGS DECODING
                                reg_104 = vals_fault[4] # Primary Warnings
                                reg_105 = vals_fault[5] # Secondary/Critical Warnings
                                
                                active_warnings = [] 
                                
                                # GLITCH FILTER: Skip decoding if data is garbage (All 1s)
                                if reg_104 != 65535 and reg_105 != 65535:
                                    
                                    # -- Reg 104 Checks --
                                    if (reg_104 & 1):   active_warnings.append("Fan Locked (01)")
                                    if (reg_104 & 2):   active_warnings.append("Temperature High (02)")
                                    if (reg_104 & 4):   active_warnings.append("Low Battery (04)")
                                    if (reg_104 & 8):   active_warnings.append("BMS Fail (19)")
                                    if (reg_104 & 16):  active_warnings.append("Output Derating (10)")
                                    if (reg_104 & 32):  active_warnings.append("PV Energy Low (15)")
                                    
                                    # PHYSICS CHECK: Only show "Battery Open" (Reg104 Bit6) if battery is IDLE
                                    if (reg_104 & 64) and not batt_is_active:
                                        active_warnings.append("Battery Open (bP)")
                                        
                                    if (reg_104 & 128): active_warnings.append("Power Limit (09)")
                                    if (reg_104 & 256): active_warnings.append("Low Battery (04-Alt)")

                                    # -- Reg 105 Checks (CRITICAL) --
                                    
                                    # SMART FILTER 1: System Fault Context
                                    if (reg_105 & 1):
                                        if status_code not in [2, 3]: 
                                            active_warnings.append("System Fault (01)")

                                    # SMART FILTER 2: Battery Recovery Context
                                    is_recovering = (reg_105 & 4096) != 0
                                    
                                    # PHYSICS CHECK: Only show "Battery Open" (Reg105 Bit6) if battery is IDLE
                                    # AND not recovering
                                    if (reg_105 & 64) and not is_recovering and not batt_is_active:
                                        active_warnings.append("Battery Open (64)")
                                    
                                    if is_recovering:
                                        if status_code == 2: 
                                            active_warnings.append("Battery Recovering (Waiting for Charge)")
                                        else:
                                            active_warnings.append("Battery Cutoff/Under Voltage (4096)")

                                    # Update JSON
                                    if not active_warnings:
                                        latest_data_json["warning_msg"] = "No Warning"
                                        latest_data_json["warning_code"] = 0
                                    else:
                                        latest_data_json["warning_msg"] = ", ".join(active_warnings)
                                        latest_data_json["warning_code"] = 99
                                    
                                    # FAULT FALLBACK
                                    if numeric_fault == 0 and status_code == 1 and active_warnings:
                                        latest_data_json["fault_msg"] = "FAULT: " + ", ".join(active_warnings)
                                    else:
                                        latest_data_json["fault_msg"] = FAULT_MAP.get(numeric_fault, f"Unknown Fault {numeric_fault}")

                                    latest_data_json["fault_bitmask"] = reg_104
                                    latest_data_json["warning_bitmask"] = reg_105
                            except Exception as e:
                                print(f"[!] Error decoding warnings: {e}")
                                traceback.print_exc()

                        if vals and len(vals) >= 40:
                            # Status decoding
                            status_code = vals[1]
                            latest_data_json["device_status_code"] = status_code
                            latest_data_json["device_status_msg"] = STATUS_MAP.get(status_code, f"Unknown ({status_code})")

                            v_batt = vals[15] / 10.0
                            p_load_va = vals[14]
                            p_load_real = vals[13]
                            v_ac_out = vals[5] / 10.0
                            v_pv = vals[19] / 10.0
                            p_pv = vals[23]
                            
                            p_batt_discharge = vals[8]
                            p_batt_charge = vals[9]
                            if p_batt_charge > 0:
                                batt_power = -p_batt_charge
                            else:
                                batt_power = to_signed(p_batt_discharge)

                            latest_data_json.update({
                                "grid_volt":    vals[2] / 10.0,
                                "grid_power_watt": vals[4],
                                "ac_output_amp": vals[11] / 10.0,
                                "grid_freq":    vals[3] / 100.0,
                                "ac_out_volt":  v_ac_out,
                                "ac_load_va":   p_load_va,
                                "ac_load_real_watt": p_load_real,
                                "ac_load_pct":  round(min((p_load_va / INVERTER_RATED_WATT) * 100, 300), 1),
                                "batt_volt":    v_batt,
                                "batt_power_watt": batt_power,
                                "batt_soc":     vals[29],
                                "pv_input_watt": p_pv,
                                "pv_input_volt": v_pv,
                                "pv_current":    round(p_pv / v_pv, 2) if v_pv > 0 else 0.0,
                                "temp_dc":      vals[26],
                                "temp_inv":     vals[27],
                                "batt_current": round(abs(batt_power) / v_batt, 1) if v_batt > 0 else 0
                            })

                    except:
                        break 
                loop_counter += 1
                time.sleep(POLL_INTERVAL)
        except Exception:
            time.sleep(1)
        finally:
            if current_inverter_conn: current_inverter_conn.close()
            current_inverter_conn = None

def control_server():
    global latest_data_json, last_cmd_time
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((BIND_IP, LOCAL_CONTROL_PORT))
    s.listen(5)

    while True:
        try:
            client, _ = s.accept()
            client.settimeout(2.0)
            req = client.recv(1024).strip().decode().upper()
            
            if req == "JSON":
                client.send(json.dumps(latest_data_json).encode())
            
            elif current_inverter_conn and req:
                if time.time() - last_cmd_time < 0.5:
                    client.send(b"BUSY")
                    client.close()
                    continue

                cmd_packet = None
                
                # --- COMMANDS ---
                
                # NEW SPECIFIC COMMAND (Reg 306)
                if req.startswith("SET_RETURN_DEFAULT_"):
                    cmd_packet = build_write_packet(306, int(req.split("_")[3]))
                    latest_data_json["return_to_default"] = int(req.split("_")[3])

                elif req.startswith("MODE_"):
                    cmd_packet = build_write_packet(301, int(req.split("_")[1]))
                    latest_data_json["output_mode"] = int(req.split("_")[1])
                elif req.startswith("SET_AC_RANGE_"):
                    cmd_packet = build_write_packet(302, int(req.split("_")[3]))
                    latest_data_json["ac_input_range"] = int(req.split("_")[3])
                elif req == "CHARGE_ON" or req == "SNU_SET": 
                    cmd_packet = build_write_packet(331, 2)
                    latest_data_json["charger_priority"] = 2
                elif req == "CHARGE_OFF" or req == "OSO_SET": 
                    cmd_packet = build_write_packet(331, 3)
                    latest_data_json["charger_priority"] = 3
                elif req == "CSO_SET":
                    cmd_packet = build_write_packet(331, 1)
                    latest_data_json["charger_priority"] = 1
                elif req.startswith("SET_AMPS_"):
                    try:
                        amps = int(req.split("_")[2])
                        cmd_packet = build_write_packet(333, amps * 10)
                        latest_data_json["max_ac_amps"] = amps
                    except: pass
                elif req.startswith("SET_SOC_GRID_"):
                    try:
                        val = int(req.split("_")[3])
                        if val >= latest_data_json["soc_cutoff"]: 
                            cmd_packet = build_write_packet(341, val)
                            latest_data_json["soc_back_to_grid"] = val
                    except: pass
                elif req.startswith("SET_SOC_BATT_"):
                    try:
                        val = int(req.split("_")[3])
                        cmd_packet = build_write_packet(342, val)
                        latest_data_json["soc_back_to_batt"] = val
                    except: pass
                elif req.startswith("SET_SOC_CUTOFF_"):
                    try:
                        val = int(req.split("_")[3])
                        if val <= latest_data_json["soc_back_to_grid"]:
                            cmd_packet = build_write_packet(343, val)
                            latest_data_json["soc_cutoff"] = val
                    except: pass
                elif req.startswith("SET_BUZZER_"):
                    cmd_packet = build_write_packet(303, int(req.split("_")[2]))
                    latest_data_json["buzzer_mode"] = int(req.split("_")[2])
                elif req.startswith("SET_BACKLIGHT_"):
                    cmd_packet = build_write_packet(305, int(req.split("_")[2]))
                    latest_data_json["backlight_status"] = int(req.split("_")[2])

                if cmd_packet:
                    with modbus_lock:
                        flush_buffer(current_inverter_conn)
                        current_inverter_conn.send(cmd_packet)
                        last_cmd_time = time.time()
                    client.send(b"OK")
            else:
                client.send(b"OFFLINE")
            client.close()
        except: pass

if __name__ == "__main__":
    t1 = threading.Thread(target=inverter_server, daemon=True)
    t2 = threading.Thread(target=control_server, daemon=True)
    t1.start(); t2.start()
    try:
        while True: time.sleep(1)
    except KeyboardInterrupt: os._exit(0)
