import socket
import threading
import struct
import time
import json

# --- CONFIGURATION ---
INVERTER_PORT = 18899
LOCAL_CONTROL_PORT = 9999
BIND_IP = '0.0.0.0'
POLL_INTERVAL = 0.5 # Fast updates

# --- SHARED STATE ---
current_inverter_conn = None
latest_data_json = {
    "grid_charge_setting": 3, 
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
        if raw[1] != 3: return None 
        byte_count = raw[2]
        data = raw[3 : 3 + byte_count]
        return [x[0] for x in struct.iter_unpack('>H', data)]
    except:
        return None

def to_signed(val):
    if val > 32768:
        return val - 65536
    return val

# --- WORKER: INVERTER POLLER ---
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
                    # 1. READ SENSORS (200-240)
                    conn.send(build_read_packet(200, 40))
                    time.sleep(0.1)
                    vals = read_modbus_response(conn)

                    # 2. READ SETTING (331)
                    conn.send(build_read_packet(331, 1))
                    time.sleep(0.1)
                    vals_setting = read_modbus_response(conn)
                    
                    if vals and vals_setting:
                        charge_val = vals_setting[0]
                        
                        v_grid      = vals[2] / 10.0
                        f_grid      = vals[3] / 100.0 # Reg 203: Grid Freq
                        v_out       = vals[5] / 10.0
                        f_out       = vals[6] / 100.0 # Reg 206: Out Freq?
                        v_batt      = vals[15] / 10.0
                        v_pv        = vals[13] / 10.0
                        
                        p_pv        = vals[9]   # PV Power
                        p_batt_flow = to_signed(vals[8])  # Reg 208
                        p_load      = vals[14]            # Reg 214
                        
                        # --- CALCULATIONS ---
                        # Currents (I = P/V)
                        i_batt = round(abs(p_batt_flow) / v_batt, 1) if v_batt > 0 else 0
                        i_pv   = round(p_pv / v_pv, 1) if v_pv > 0 else 0
                        i_out  = round(p_load / v_out, 1) if v_out > 0 else 0
                        
                        # Apparent Power (VA) = V * A
                        p_apparent = round(v_out * i_out)
                        
                        soc_guess = vals[29] # Reg 229

                        latest_data_json = {
                            "grid_charge_setting": charge_val, 
                            "status_raw":   vals[1],        
                            
                            "grid_volt":    v_grid, 
                            "grid_freq":    f_grid,  # <--- NEW
                            
                            "ac_out_volt":  v_out,
                            "ac_out_freq":  f_out,   # <--- NEW
                            "ac_load_watt": p_load,
                            "ac_out_va":    p_apparent, # <--- NEW (Calculated)
                            "ac_out_amp":   i_out,      # <--- NEW (Calculated)
                            
                            "batt_volt":    v_batt,
                            "batt_power_watt": p_batt_flow,
                            "batt_current": i_batt,  # <--- NEW
                            "batt_soc":     soc_guess,
                            
                            "pv_input_volt": v_pv, 
                            "pv_input_watt": p_pv,         
                            "pv_current":    i_pv,   # <--- NEW
                            
                            "inverter_temp": vals[19] / 10.0 if vals[19] < 1000 else vals[19],
                            
                            # DEBUG: Expose mystery registers to find other sensors
                            "debug_reg_204": vals[4],
                            "debug_reg_207": vals[7]
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

# --- WORKER: COMMAND API ---
def control_server():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((BIND_IP, LOCAL_CONTROL_PORT))
    s.listen(5)

    while True:
        client, _ = s.accept()
        try:
            req = client.recv(1024).strip().decode().upper()
            if req == "JSON":
                client.send(json.dumps(latest_data_json).encode())
            elif current_inverter_conn:
                if req == "CHARGE_ON":
                    current_inverter_conn.send(build_write_packet(331, 2))
                    client.send(b"OK")
                elif req == "CHARGE_OFF":
                    current_inverter_conn.send(build_write_packet(331, 3))
                    client.send(b"OK")
                elif req.startswith("MODE_"):
                    val = int(req.split("_")[1])
                    current_inverter_conn.send(build_write_packet(301, val))
                    client.send(b"OK")
            else:
                client.send(b"OFFLINE")
        except:
            pass
        finally:
            client.close()

if __name__ == "__main__":
    t1 = threading.Thread(target=inverter_server)
    t2 = threading.Thread(target=control_server)
    t1.start(); t2.start()