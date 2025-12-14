import socket
import threading
import struct
import time
import json

# --- CONFIGURATION ---
INVERTER_PORT = 18899
LOCAL_CONTROL_PORT = 9999
BIND_IP = '0.0.0.0'
POLL_INTERVAL = 1.0 

# --- SHARED STATE ---
current_inverter_conn = None
modbus_lock = threading.Lock()
last_cmd_time = 0 

# Initialize State
latest_data_json = {
    "grid_charge_setting": 3,
    "output_mode": 0,
    "batt_volt": 0,
    "ac_load_watt": 0,
    "batt_power_watt": 0,
    "grid_power_watt": 0,
    "pv_input_watt": 0,
    "batt_soc": 0
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

def build_write_packet(reg, value):
    payload = struct.pack('>BBHHB', 1, 16, reg, 1, 2) + struct.pack('>H', value)
    return payload + modbus_crc(payload)

def build_read_packet(start, count):
    payload = struct.pack('>BBHH', 1, 3, start, count)
    return payload + modbus_crc(payload)

def read_modbus_response(conn):
    try:
        raw = conn.recv(1024)
        if not raw or len(raw) < 5: return None
        if raw[1] > 4: return None 
        byte_count = raw[2]
        data = raw[3 : 3 + byte_count]
        return [x[0] for x in struct.iter_unpack('>H', data)]
    except:
        return None

def to_signed(val):
    if val > 32768: return val - 65536
    return val

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
                try:
                    with modbus_lock:
                        # 1. READ SENSORS (Always)
                        conn.send(build_read_packet(200, 40))
                        time.sleep(0.1)
                        vals = read_modbus_response(conn)

                        # 2. READ SETTINGS (Only if not in cooldown)
                        vals_mode = None
                        vals_charge = None
                        is_cooldown = (time.time() - last_cmd_time) < 10.0
                        
                        if (loop_counter % 5 == 0) and (not is_cooldown):
                            conn.send(build_read_packet(301, 1))
                            time.sleep(0.1)
                            vals_mode = read_modbus_response(conn)
                            
                            conn.send(build_read_packet(331, 1))
                            time.sleep(0.1)
                            vals_charge = read_modbus_response(conn)

                    # 3. UPDATE JSON (Thread-Safe Merge)
                    # We only update fields we actually read.
                    if vals:
                        p_batt_flow = to_signed(vals[8])
                        v_batt = vals[15] / 10.0
                        
                        # Update SENSORS (Always safe)
                        latest_data_json.update({
                            "grid_volt":    vals[2] / 10.0, 
                            "grid_power_watt": vals[4],
                            "grid_freq":    vals[3] / 100.0,
                            "ac_out_volt":  vals[5] / 10.0,
                            "ac_load_watt": vals[14],
                            "batt_volt":    v_batt,
                            "batt_power_watt": p_batt_flow,
                            "batt_soc":     vals[29],
                            "pv_input_watt": vals[9],         
                            "inverter_temp": vals[19] / 10.0 if vals[19] < 1000 else vals[19],
                            "batt_current": round(abs(p_batt_flow) / v_batt, 1) if v_batt > 0 else 0,
                            "pv_current":   round(vals[9] / (vals[13]/10.0), 1) if vals[13] > 0 else 0
                        })

                        # Update SETTINGS (ONLY if we actually read them)
                        # This prevents overwriting your manual command with old data
                        if vals_mode:
                            latest_data_json["output_mode"] = vals_mode[0]
                        
                        if vals_charge:
                            latest_data_json["grid_charge_setting"] = vals_charge[0]

                    loop_counter += 1
                    time.sleep(POLL_INTERVAL)
                    
                except Exception as e:
                    print(f"Poll Error: {e}")
                    break
        except Exception as e:
            print(f"Listener Error: {e}")
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
                cmd_packet = None
                
                # PREPARE COMMAND
                if req == "CHARGE_ON":
                    cmd_packet = build_write_packet(331, 2)
                    # INSTANTLY UPDATE JSON
                    latest_data_json["grid_charge_setting"] = 2
                    
                elif req == "CHARGE_OFF":
                    cmd_packet = build_write_packet(331, 3)
                    # INSTANTLY UPDATE JSON
                    latest_data_json["grid_charge_setting"] = 3
                    
                elif req.startswith("MODE_"):
                    val = int(req.split("_")[1])
                    cmd_packet = build_write_packet(301, val)
                    # INSTANTLY UPDATE JSON
                    latest_data_json["output_mode"] = val

                if cmd_packet:
                    with modbus_lock:
                        current_inverter_conn.send(cmd_packet)
                        read_modbus_response(current_inverter_conn)
                        last_cmd_time = time.time() # Start Cooldown
                            
                    client.send(b"OK")
            else:
                client.send(b"OFFLINE")
            client.close()
        except:
            pass

if __name__ == "__main__":
    t1 = threading.Thread(target=inverter_server)
    t2 = threading.Thread(target=control_server)
    t1.start(); t2.start()
