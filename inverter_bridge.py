import socket
import threading
import struct
import time
import json

# --- CONFIGURATION ---
INVERTER_PORT = 18899
LOCAL_CONTROL_PORT = 9999
BIND_IP = '0.0.0.0'
POLL_INTERVAL = 0.5

# --- SHARED STATE ---
current_inverter_conn = None
modbus_lock = threading.Lock()
latest_data_json = {
    "grid_charge_setting": 3,
    "output_mode": 0, # <--- NEW: Defaults to UTI
    "batt_volt": 0,
    "ac_load_watt": 0,
    "batt_power_watt": 0
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
    global current_inverter_conn, latest_data_json
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((BIND_IP, INVERTER_PORT))
    s.listen(1)
    print(f"[*] Waiting for Inverter on {INVERTER_PORT}...")

    while True:
        try:
            conn, addr = s.accept()
            print(f"[+] Connected: {addr}")
            current_inverter_conn = conn
            
            while True:
                try:
                    with modbus_lock:
                        # 1. READ SENSORS (200-240)
                        conn.send(build_read_packet(200, 40))
                        time.sleep(0.1)
                        vals = read_modbus_response(conn)

                        # 2. READ SETTINGS (Block read 301-331 to save time)
                        # We read a range to get both Mode (301) and Charge (331)
                        # But they are far apart, so we do two small reads for speed.
                        
                        vals_mode = None
                        vals_charge = None
                        
                        if vals:
                            # Read Mode (301)
                            conn.send(build_read_packet(301, 1))
                            time.sleep(0.1)
                            vals_mode = read_modbus_response(conn)
                            
                            # Read Charge (331)
                            conn.send(build_read_packet(331, 1))
                            time.sleep(0.1)
                            vals_charge = read_modbus_response(conn)

                    if vals and vals_mode and vals_charge:
                        p_batt_flow = to_signed(vals[8])
                        v_batt = vals[15] / 10.0
                        
                        latest_data_json = {
                            "output_mode":  vals_mode[0],   # <--- REAL FEEDBACK
                            "grid_charge_setting": vals_charge[0], 
                            
                            "grid_volt":    vals[2] / 10.0, 
                            "grid_freq":    vals[3] / 100.0,
                            "ac_out_volt":  vals[5] / 10.0,
                            "ac_load_watt": vals[14],
                            "batt_volt":    v_batt,
                            "batt_power_watt": p_batt_flow,
                            "batt_soc":     vals[29],
                            "pv_input_watt": vals[9],         
                            "inverter_temp": vals[19] / 10.0 if vals[19] < 1000 else vals[19],
                            
                            # Calculated
                            "batt_current": round(abs(p_batt_flow) / v_batt, 1) if v_batt > 0 else 0,
                            "pv_current":   round(vals[9] / (vals[13]/10.0), 1) if vals[13] > 0 else 0
                        }

                    time.sleep(POLL_INTERVAL)
                except Exception as e:
                    print(f"Poll Error: {e}")
                    break
        except Exception as e:
            print(f"Listener Error: {e}")
        finally:
            if current_inverter_conn: current_inverter_conn.close()
            current_inverter_conn = None

def control_server():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((BIND_IP, LOCAL_CONTROL_PORT))
    s.listen(5)

    while True:
        client, _ = s.accept()
        try:
            req = client.recv(1024).strip().decode().upper()
            if not req: continue

            if req == "JSON":
                client.send(json.dumps(latest_data_json).encode())
            elif current_inverter_conn:
                cmd_packet = None
                if req == "CHARGE_ON":
                    cmd_packet = build_write_packet(331, 2)
                elif req == "CHARGE_OFF":
                    cmd_packet = build_write_packet(331, 3)
                elif req.startswith("MODE_"):
                    val = int(req.split("_")[1])
                    cmd_packet = build_write_packet(301, val)

                if cmd_packet:
                    with modbus_lock:
                        current_inverter_conn.send(cmd_packet)
                        read_modbus_response(current_inverter_conn) # Flush buffer
                    client.send(b"OK")
            else:
                client.send(b"OFFLINE")
        except: pass
        finally: client.close()

if __name__ == "__main__":
    t1 = threading.Thread(target=inverter_server)
    t2 = threading.Thread(target=control_server)
    t1.start(); t2.start()
