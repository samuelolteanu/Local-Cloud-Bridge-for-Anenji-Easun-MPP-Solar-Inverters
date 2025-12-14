# Local Cloud Bridge for Anenji / Easun / MPP Solar Inverters

**Unchain your inverter from the cloud.**

This project provides a fully local, privacy-focused control system for "Cloud-Only" Hybrid Inverters (commonly sold under brands like **Anenji**, **Easun**, **MPP Solar**, and others using the **Desmonitor / SmartEss / WatchPower** apps).

By hijacking the inverter's network traffic and redirecting it to a local Python script, we achieve **1-second real-time updates**, complete offline control, and instant integration with Home Assistant—without voiding the warranty or opening the case.

## 🚀 Features

* **⚡ 1-Second Updates:** Replaces the slow 5-minute cloud refresh rate with instant real-time data.
* **🔒 100% Local Control:** No data is sent to Chinese or European cloud servers. Works even when the internet is down.
* **🏠 Home Assistant Integration:** Native sensors and switches for Grid Charging, Priority Modes, and Load monitoring.
* **🔋 Accurate Math:** Fixes "unsigned integer" bugs (e.g., negative battery flow showing as huge numbers) and calculates missing values like Amps.
* **🛠 No Hardware Mods:** Uses the inverter's existing WiFi dongle. No USB cables or RS485 adapters required.

## 📋 Prerequisites

1.  **A Compatible Inverter:** Any inverter that uses the "Pro" WiFi dongle (often blue/black) and connects to `server.desmonitor.com` or similar.
    * *Tested on: ANENJI ANJ-6200W-48V*
2.  **A Router with Custom Firewall (OpenWRT) OR a Local DNS Server:** You need a way to "trick" the inverter into talking to your local server instead of the cloud.
    * *Recommended: OpenWRT Router*
    * *Alternative: Pi-hole / AdGuard Home (DNS Rewrite)*
3.  **A Local Server:** A Raspberry Pi, Proxmox LXC, or Docker container to run the bridge script.

---

## ⚙️ Architecture

The inverter is hard-coded to communicate with a remote cloud server on TCP port **18899**.

1.  **The Hijack:** We use a router firewall rule to intercept all traffic destined for TCP port 18899 and redirect it to our local server IP (`192.168.0.105`).
2.  **The Bridge:** A Python script listens on port 18899, pretending to be the cloud. It accepts the Modbus TCP connection.
3.  **The Translation:** The script reads the raw registers, fixes signed/unsigned data issues, calculates Amps, and exposes the data via a simple JSON API.
4.  **The Interface:** Home Assistant queries this JSON API every second using `netcat` (nc) for zero-latency updates.

---

## 🛠️ Installation

### Step 1: Network Hijack (The Critical Step)

You must prevent the inverter from reaching the real internet.

#### Option A: OpenWRT (Best Method)
Add a **Port Forwarding (DNAT)** rule to your router's firewall:
* **Name:** `Inverter Hijack`
* **Protocol:** TCP
* **External Zone:** LAN (Yes, we are hijacking LAN traffic)
* **External Port:** 18899
* **Internal IP:** `192.168.0.105` (Your Bridge Server IP)
* **Internal Port:** 18899

*Note: You may also need a "NAT Loopback" (Masquerade) rule if your router doesn't handle local redirection automatically.*

#### Option B: DNS Hijack (Pi-hole / AdGuard)
If you can't touch your router's firewall:
1.  Find the domain your inverter tries to contact (e.g., `server.desmonitor.com`).
2.  Add a **DNS Rewrite** in AdGuard/Pi-hole: `server.desmonitor.com` -> `192.168.0.105`.

---

### Step 2: The Python Bridge Service

Create the script on your server (e.g., `/root/inverter_bridge.py`). This script mimics the cloud server.


### Step 3: Run as a Service
Create a systemd service to keep it running forever. File: /etc/systemd/system/inverter-bridge.service

Ini, TOML

[Unit]
Description=Inverter Modbus TCP Bridge
After=network.target

[Service]
ExecStart=/usr/bin/python3 -u /root/inverter_bridge.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target


Enable it:

Bash

systemctl daemon-reload
systemctl enable --now inverter-bridge



#### Step 4: Home Assistant Configuration
Add this to your configuration.yaml. We use nc (Netcat) instead of Python for the command line to ensure sub-1-second performance.

command_line:
  - sensor:
      name: "Inverter Bridge Data"
      command: 'echo "JSON" | nc -w 1 192.168.0.105 9999'
      scan_interval: 1
      value_template: "{{ value_json.batt_volt }}"
      json_attributes:
        - grid_charge
        - grid_volt
        - ac_load_watt
        - ac_out_volt
        - batt_volt
        - batt_power
        - batt_soc
        - batt_current
        - pv_watt
        - pv_volt
        - temp

  - switch:
      name: "Grid Charging"
      scan_interval: 2
      command_on: 'echo "CHARGE_ON" | nc -w 1 192.168.0.105 9999'
      command_off: 'echo "CHARGE_OFF" | nc -w 1 192.168.0.105 9999'
      command_state: 'echo "JSON" | nc -w 1 192.168.0.105 9999 | jq ".grid_charge"'
      value_template: "{{ value == '2' }}"

template:
  - sensor:
      - name: "House Load"
        unit_of_measurement: "W"
        state: "{{ state_attr('sensor.inverter_bridge_data', 'ac_load_watt') }}"
      - name: "Battery Power"
        unit_of_measurement: "W"
        state: "{{ state_attr('sensor.inverter_bridge_data', 'batt_power') }}"
      - name: "Battery SOC"
        unit_of_measurement: "%"
        state: "{{ state_attr('sensor.inverter_bridge_data', 'batt_soc') }}"


Register	Function	Description
202	Grid Voltage	0.1 V
203	Grid Frequency	0.01 Hz
205	Output Voltage	0.1 V
208	Battery Power	Signed Int (Positive = Discharge, Negative = Charge)
209	PV Power	Watts
213	PV Voltage	0.1 V
214	Active Load	Watts (Real House Load)
215	Battery Voltage	0.1 V
219	Inverter Temp	0.1 °C (or raw Int)
229	Battery SOC	Percentage %
301	Output Mode	0=UTI, 1=SOL, 2=SBU, 3=SUB
331	Grid Charge	2=Enabled, 3=Disabled



⚠️ Disclaimer
This project is not affiliated with Anenji, Easun, or MPP Solar. Reverse engineering protocols involves some risk.

Safety: You are dealing with high-voltage equipment. Do not change write-registers (300+) unless you know what you are doing.

Updates: Your inverter will no longer receive firmware updates from the cloud (which is usually a good thing).

Status: The official app will show "Offline" or "System Abnormal." This is normal and indicates the hijack is working.