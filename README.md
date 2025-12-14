# Local Cloud Bridge for Anenji / Easun / MPP Solar Inverters

**Unchain your inverter from the cloud.**

This project provides a fully local, privacy-focused control system for "Cloud-Only" Hybrid Inverters. These devices are commonly sold under brands like **Anenji**, **Easun**, **MPP Solar**, and others that use the **Desmonitor**, **SmartEss**, or **WatchPower** mobile apps.

By hijacking the inverter's network traffic and redirecting it to a local Python script, we achieve **1-second real-time updates**, complete offline control, and instant integration with Home Assistant—without voiding the warranty, opening the case, or using RS485 adapters.

## 🚀 Features

* **⚡ 1-Second Updates:** Replaces the slow 5-minute cloud refresh rate with instant real-time data.
* **🔒 100% Local Control:** No data is sent to external cloud servers. The system works even when the internet is down.
* **🏠 Home Assistant Integration:** Native sensors and switches for Grid Charging, Priority Modes, and Load monitoring.
* **🔋 Accurate Math:** Auto-calculates values the inverter doesn't provide natively (e.g., Battery Current, correct signed Battery Flow, and true House Load).
* **🛠 No Hardware Mods:** Uses the inverter's existing WiFi dongle.

---

## 📋 Prerequisites

1.  **Compatible Inverter:** Any hybrid inverter using the WiFi dongle (typically blue or black) that connects to `server.desmonitor.com` or similar Chinese cloud servers.
    * *Verified Hardware:* ANENJI ANJ-6200W-48V
2.  **Network Control:** You need a method to redirect traffic.
    * *Best:* **OpenWRT Router** (or pfSense/MikroTik) to create a NAT Hijack rule.
    * *Alternative:* **Local DNS** (Pi-hole / AdGuard Home) to rewrite the server domain.
3.  **Local Server:** A Raspberry Pi, Proxmox LXC, or Docker container to run the bridge script.

---

## ⚙️ Architecture

The inverter is hard-coded to communicate with a remote cloud server on TCP port **18899**. We perform a "Man-in-the-Middle" attack on our own device:

1.  **The Hijack:** The router intercepts traffic destined for the cloud (TCP 18899) and redirects it to the local server IP (`192.168.0.105`).
2.  **The Bridge:** The `inverter_bridge.py` script listens on port 18899. It accepts the connection, reads the Modbus registers, and exposes the data via a lightweight JSON API.
3.  **The Interface:** Home Assistant queries this API every second using `netcat` (nc) for zero-latency updates.

---

## 🛠️ Installation

### Step 1: Network Hijack

You must prevent the inverter from reaching the real internet and force it to talk to your server.

#### Method A: OpenWRT (Recommended)
Add a **Port Forwarding (DNAT)** rule to your router's firewall:
* **Name:** `Inverter Hijack`
* **Protocol:** TCP
* **External Zone:** LAN (We are hijacking internal LAN traffic)
* **External Port:** 18899
* **Internal IP:** `192.168.0.105` (Your Bridge Server IP)
* **Internal Port:** 18899

*Tip: Ensure you also have a "NAT Loopback" (Masquerade) rule active so the inverter accepts the response from your local server.*

#### Method B: DNS Rewrite
If you cannot edit firewall rules, use Pi-hole or AdGuard Home:
1.  Check your logs to see what domain the inverter requests (e.g., `server.desmonitor.com`).
2.  Add a DNS Rewrite: `server.desmonitor.com` ➡ `192.168.0.105`.

---

### Step 2: Install the Bridge Service

1.  Upload the `inverter_bridge.py` script to your server (e.g., `/root/inverter_bridge.py`).
### Step 3: Run as a Service
Create a systemd service to keep it running forever.

File: /etc/systemd/system/inverter-bridge.service

```ini
[Unit]
Description=Inverter Modbus TCP Bridge
After=network.target

[Service]
# Update the path below to match where you saved the script
ExecStart=/usr/bin/python3 -u /root/inverter_bridge.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable it:
```bash
systemctl daemon-reload
systemctl enable --now inverter-bridge
```

### Step 4: Home Assistant Configuration
Add this to your configuration.yaml. We use nc (Netcat) instead of Python for the command line to ensure sub-1-second performance.

```yaml
# --- PRIORITY MODE SELECTOR ---
input_select:
  inverter_mode:
    name: Inverter Output Source Priority
    options:
      - "Utility First (UTI)"
      - "Solar First (SOL)"
      - "SBU (Solar-Batt-Util)"
      - "SUB (Solar-Util-Batt)"
      - "SUF (GRID Feedback)"
    icon: mdi:source-branch

#--- CONTROL LOGIC ---
shell_command:
  set_inverter_uti: '/bin/sh -c "echo MODE_0 | nc -w 5 192.168.0.105 9999"'
  set_inverter_sol: '/bin/sh -c "echo MODE_1 | nc -w 5 192.168.0.105 9999"'
  set_inverter_sbu: '/bin/sh -c "echo MODE_2 | nc -w 5 192.168.0.105 9999"'
  set_inverter_sub: '/bin/sh -c "echo MODE_3 | nc -w 5 192.168.0.105 9999"'
  set_inverter_suf: '/bin/sh -c "echo MODE_4 | nc -w 5 192.168.0.105 9999"'

command_line:
  - sensor:
      name: "Inverter Bridge Data"
      # Give it 3 seconds to fetch JSON (sensors are fast, but 3s is safer)
      command: 'echo "JSON" | nc -w 3 192.168.0.105 9999' 
      scan_interval: 1  # 2 seconds is a good balance
      
      
      value_template: "{{ value_json.batt_volt }}"
      json_attributes:
        - output_mode
        - grid_charge_setting
        - grid_volt 
        - batt_volt
        - ac_load_watt
        - ac_out_volt  
        - ac_out_amp       
        - pv_input_volt
        - pv_input_watt
        - inverter_temp
        - batt_power_watt
        - batt_soc
        - batt_current
        - pv_current
        - grid_power_watt

  - switch:
        name: "Grid Charging"
        unique_id: grid_chargingz
        command_timeout: 5
        
        # ON/OFF Commands
        command_on: 'echo "CHARGE_ON" | nc -w 5 192.168.0.105 9999'
        command_off: 'echo "CHARGE_OFF" | nc -w 5 192.168.0.105 9999'
        
        # State Check
        command_state: 'echo "JSON" | nc -w 3 192.168.0.105 9999 | jq ".grid_charge_setting"'
        
        # CRITICAL MISSING LINE:
        # Tells HA: "If the number is 2, the switch is ON. Anything else is OFF."
        value_template: "{{ value == '2' }}"
        
        icon: mdi:flash

template:
  - sensor:
      - name: "Grid Input Power"
        unique_id: inv_grid_power
        unit_of_measurement: "W"
        device_class: power
        state: "{{ state_attr('sensor.inverter_bridge_data', 'grid_power_watt') }}"
  - sensor:
      - name: "PV Current"
        unique_id: inv_pv_current
        unit_of_measurement: "A"
        state: "{{ state_attr('sensor.inverter_bridge_data', 'pv_current') }}"
        
      - name: "Battery Current"
        unique_id: inv_batt_current
        unit_of_measurement: "A"
        state: "{{ state_attr('sensor.inverter_bridge_data', 'batt_current') }}"
        
  - sensor:
      - name: "BMS Battery Percentage"
        unique_id: inv_batt_soc
        unit_of_measurement: "%"
        device_class: battery
        state_class: measurement
        state: >
          {{ state_attr('sensor.inverter_bridge_data', 'batt_soc') }}

      - name: "PV Current"
        unique_id: inv_pv_current
        unit_of_measurement: "A"
        device_class: current
        state: >
          {{ state_attr('sensor.inverter_bridge_data', 'pv_current') }}
  - sensor:
      - name: "Battery Power Flow"
        unique_id: inv_batt_power
        unit_of_measurement: "W"
        device_class: power
        state: >
          {{ state_attr('sensor.inverter_bridge_data', 'batt_power_watt') }}
          
  - sensor:
      - name: "Grid Voltage"
        unique_id: inv_grid_voltage
        unit_of_measurement: "V"
        device_class: voltage
        state: >
          {{ state_attr('sensor.inverter_bridge_data', 'grid_volt') }}

  - sensor:
      - name: "Output Voltage"
        unique_id: inv_out_voltage
        unit_of_measurement: "V"
        device_class: voltage
        state: >
          {{ state_attr('sensor.inverter_bridge_data', 'ac_out_volt') }}

  - sensor:
      - name: "House Load"
        unique_id: inv_house_load
        unit_of_measurement: "W"
        device_class: power
        state: >
          {{ state_attr('sensor.inverter_bridge_data', 'ac_load_watt') }}

  - sensor:
      - name: "Battery Voltage (Inverter)"
        unique_id: inv_batt_voltage
        unit_of_measurement: "V"
        device_class: voltage
        state: >
          {{ state_attr('sensor.inverter_bridge_data', 'batt_volt') }}

  - sensor:
      - name: "PV Input Voltage"
        unique_id: inv_pv_voltage
        unit_of_measurement: "V"
        device_class: voltage
        state: >
          {{ state_attr('sensor.inverter_bridge_data', 'pv_input_volt') }}

  - sensor:
      - name: "PV Input Power"
        unique_id: inv_pv_power
        unit_of_measurement: "W"
        device_class: power
        state: >
          {{ state_attr('sensor.inverter_bridge_data', 'pv_input_watt') }}

  - sensor:
      - name: "Inverter Temperature"
        unique_id: inv_temp
        unit_of_measurement: "°C"
        device_class: temperature
        state: >
          {{ state_attr('sensor.inverter_bridge_data', 'inverter_temp') }}
        
```

Automation 1 :
```yaml
alias: "Inverter: Set Priority Mode"
description: Sends command to inverter when Dropdown changes in UI
triggers:
  - entity_id: input_select.inverter_mode
    trigger: state
actions:
  - choose:
      - conditions:
          - condition: template
            value_template: "{{ trigger.to_state.state == 'Utility First (UTI)' }}"
        sequence:
          - action: shell_command.set_inverter_uti
      - conditions:
          - condition: template
            value_template: "{{ trigger.to_state.state == 'Solar First (SOL)' }}"
        sequence:
          - action: shell_command.set_inverter_sol
      - conditions:
          - condition: template
            value_template: "{{ trigger.to_state.state == 'SBU (Solar-Batt-Util)' }}"
        sequence:
          - action: shell_command.set_inverter_sbu
      - conditions:
          - condition: template
            value_template: "{{ trigger.to_state.state == 'SUB (Solar-Util-Batt)' }}"
        sequence:
          - action: shell_command.set_inverter_sub
      - conditions:
          - condition: template
            value_template: "{{ trigger.to_state.state == 'SUF (GRID Feedback)' }}"
        sequence:
          - action: shell_command.set_inverter_suf
mode: single

```
Automation 2 :
```yaml
alias: "Inverter: Sync Dropdown from Device"
triggers:
  - entity_id: sensor.inverter_bridge_data
    attribute: output_mode
    trigger: state
actions:
  - action: input_select.select_option
    target:
      entity_id: input_select.inverter_mode
    data:
      option: >
        {% set mode = state_attr('sensor.inverter_bridge_data', 'output_mode') |
        int(default=0) %} {% if mode == 0 %} Utility First (UTI) {% elif mode ==
        1 %} Solar First (SOL) {% elif mode == 2 %} SBU (Solar-Batt-Util) {%
        elif mode == 3 %} SUB (Solar-Util-Batt) {% elif mode == 4 %} SUF (GRID
        Feedback) {% endif %}

```


### 📊 Register Map

| Register | Function | Description |
| :--- | :--- | :--- |
| **202** | Grid Voltage | 0.1 V |
| **203** | Grid Frequency | 0.01 Hz |
| **204** | Grid Power | Watts (Power drawn from Grid) |
| **205** | Output Voltage | 0.1 V |
| **208** | Battery Power | Signed Int (Positive = Discharge, Negative = Charge) |
| **209** | PV Power | Watts |
| **213** | PV Voltage | 0.1 V |
| **214** | Active Load | Watts (Real House Load) |
| **215** | Battery Voltage | 0.1 V |
| **219** | Inverter Temp | 0.1 °C (or raw Int) |
| **229** | Battery SOC | Percentage % |
| **301** | Output Mode | 0=UTI, 1=SOL, 2=SBU, 3=SUB 4=SUF|
| **331** | Grid Charge | 2=Solar+PV, 3=Solar Only |



⚠️ Disclaimer
This project is not affiliated with Anenji, Easun, or MPP Solar. Reverse engineering protocols involves some risk.

Safety: You are dealing with high-voltage equipment. Do not change write-registers (300+) unless you know what you are doing.

Updates: Your inverter will no longer receive firmware updates from the cloud (which is usually a good thing).

Status: The official app will show "Offline" or "System Abnormal." This is normal and indicates the hijack is working.

Would you like me to help you draft the inverter_bridge.py script mentioned in Step 2, based on the architecture and register map






