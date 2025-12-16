import socket
import threading
import struct
import time
import json
import os

# --- CONFIGURATION ---
INVERTER_PORT = 18899
LOCAL_CONTROL_PORT = 9999
BIND_IP = '0.0.0.0'
POLL_INTERVAL = 1.0 
INVERTER_RATED_WATT = 6200 

# --- TRANSLATION MAPS ---

# Map for Fault Codes
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
    19: "BMS Communication Fail",
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

# Map for Device Status (Reg 201)
STATUS_MAP = {
    0: "Standby / Power Off",
    1: "Fault Mode",           
    2: "Line Mode (On-Grid)",  
    3: "Battery Mode",         
    4: "Fault Mode",       
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
    "device_status_code": 0,
    "device_status_msg": "Standby",
    "fault_bitmask": 0,
    "warning_bitmask": 0,
    "charger_priority": 3,
    "output_mode": 0,
    "ac_input_range": 0,
    "buzzer_mode": 3,
    "backlight_status": 1,
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

                        # 2. READ FAULT CODES (Block 100)
                        vals_fault = None
                        if loop_counter % 2 == 0:
                            flush_buffer(conn)
                            conn.send(build_read_packet(100, 6)) 
                            time.sleep(0.1)
                            vals_fault = read_modbus_response(conn)

                        # 3. READ SETTINGS
                        is_cooldown = (time.time() - last_cmd_time) < 10.0
                        if (loop_counter % 5 == 0) and (not is_cooldown):
                            flush_buffer(conn)
                            conn.send(build_read_packet(301, 5))
                            time.sleep(0.1)
                            vals_300 = read_modbus_response(conn)
                            
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

                            if vals_prio: latest_data_json["charger_priority"] = vals_prio[0]
                            if vals_amps: latest_data_json["max_ac_amps"] = vals_amps[0] / 10.0
                            if vals_soc and len(vals_soc) >= 3:
                                latest_data_json["soc_back_to_grid"] = vals_soc[0]
                                latest_data_json["soc_back_to_batt"] = vals_soc[1]
                                latest_data_json["soc_cutoff"] = vals_soc[2]

                        # --- UPDATE SENSORS JSON ---
                        if vals_fault and len(vals_fault) >= 5:
                            numeric_fault = vals_fault[1] # Reg 101
                            reg_104 = vals_fault[4]       # Reg 104 (Bitmask)
                            reg_105 = vals_fault[5] if len(vals_fault) > 5 else 0

                            final_error = 0
                            
                            # Priority A: Numeric Fault
                            if numeric_fault > 0 and numeric_fault < 100:
                                final_error = numeric_fault
                            
                            # Priority B: Bitmasks (Reg 104)
                            elif (reg_104 & 8):   # Bit 3 = Error 19
                                final_error = 19
                            elif (reg_104 & 2):   # Bit 1 = Error 02
                                final_error = 2   
                            elif (reg_104 & 4):   # Bit 2 = Error 07
                                final_error = 7   
                            
                            latest_data_json["fault_code"] = final_error
                            latest_data_json["fault_bitmask"] = reg_104
                            latest_data_json["warning_bitmask"] = reg_105
                            
                            # Lookup Description
                            latest_data_json["fault_msg"] = FAULT_MAP.get(final_error, f"Unknown Error {final_error}")

                        if vals and len(vals) >= 40:
                            # Status decoding
                            status_code = vals[1]
                            status_msg = STATUS_MAP.get(status_code, f"Unknown ({status_code})")

                            # --- v50 SAFETY OVERRIDE ---
                            # If we have a confirmed fault code, force the Status to "Fault Mode".
                            # This overrides "Line Mode" or "Unknown(4)" if the battery is dead.
                            if latest_data_json["fault_code"] != 0:
                                status_msg = "Fault Mode"
                                status_code = 1
                            # ---------------------------

                            latest_data_json["device_status_code"] = status_code
                            latest_data_json["device_status_msg"] = status_msg

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
                if req.startswith("MODE_"):
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
