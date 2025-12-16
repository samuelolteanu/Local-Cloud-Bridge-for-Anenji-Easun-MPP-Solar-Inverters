# Local Cloud Bridge for Anenji / Easun / MPP Solar Inverters

**Unchain your inverter from the cloud.**

This project provides a fully local, privacy-focused control system for "Cloud-Only" Hybrid Inverters. These devices are commonly sold under brands like **Anenji**, **Easun**, **MPP Solar**, and others that use the **Desmonitor**, **SmartEss**, or **WatchPower** mobile apps.

By hijacking the inverter's network traffic and redirecting it to a local Python script, we achieve **1-second real-time updates**, complete offline control, and instant integration with Home Assistant—without voiding the warranty, opening the case, or using RS485 adapters.

## 🚀 Features

* **⚡ Real-Time 1-Second Updates:** Replaces the slow 5-minute cloud refresh rate with instant high-frequency polling.
* **🔒 100% Local Control:** Acts as a transparent TCP bridge. No data is sent to external cloud servers; the system works entirely offline.
* **🎛️ Full Device Management:** Change critical settings instantly from Home Assistant: 
    * **Output Modes:** Switch between UTI, SOL, SBU, SUB, and SUF.
    * **Battery Management:** Set AC Charging Amps and specific SOC Thresholds (Back-to-Grid, Back-to-Battery, Cut-off).
    * **System Controls:** Toggle Buzzer, LCD Backlight, and AC Input Range (UPS/Appliance).
* **🔋 Smart Calculations:** Auto-calculates values the inverter doesn't report natively, such as **Real-time Battery Current (Amps)**, **PV Current**, and **Signed Battery Power** (handling charging/discharging logic).
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
**# --- PRIORITY MODE SELECTOR ---
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
      value_template: "Online"
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
        **input_number:

  inverter_soc_grid:
    name: "Back to Grid SOC (Prg 43)"
    min: 4
    max: 50
    step: 1
    unit_of_measurement: "%"
    icon: mdi:battery-arrow-down

  inverter_soc_batt:
    name: "Back to Battery SOC (Prg 44)"
    min: 60  
    max: 100
    step: 1
    unit_of_measurement: "%"
    icon: mdi:battery-arrow-up

  inverter_soc_cutoff:
    name: "Cut-off SOC (Prg 45)"
    min: 3
    max: 19
    step: 1
    unit_of_measurement: "%"
    icon: mdi:battery-alert
    
input_select:
  
  inverter_buzzer_mode:
    name: "Inverter Buzzer Mode"
    options:
      - "Mute (nd1)"
      - "Source/Warn/Fault (nd2)"
      - "Warn/Fault (nd3)"
      - "Fault Only (nd4)"
    icon: mdi:volume-high

  inverter_ac_range:
    name: "AC Input Range"
    options:
      - "Appliances (APL)"
      - "UPS (UPS)"
      - "Generator (GEN)"
    icon: mdi:sine-wave

  inverter_mode:
    name: Inverter Output Source Priority
    options:
      - "Utility First (UTI)"
      - "Solar First (SOL)"
      - "SBU (Solar-Batt-Util)"
      - "SUB (Solar-Util-Batt)"
      - "SUF (GRID Feedback)"
    icon: mdi:source-branch
    
  inverter_charger_priority:
    name: Charger Source Priority
    options:
      - "Solar First (CSO)"
      - "Solar + Utility (SNU)"
      - "Solar Only (OSO)"
    icon: mdi:battery-charging

  inverter_max_ac_amps:
    name: "Max AC Charge Amps"
    options:
      - "5"
      - "10"
      - "15"
      - "20"
      - "25"
      - "30"
      - "35"
      - "40"
      - "45"
      - "50"
      - "55"
      - "60"
      - "65"
      - "70"
      - "75"
      - "80"
    icon: mdi:current-ac

#--- CONTROL LOGIC ---
shell_command:
  set_inverter_uti: '/bin/sh -c "echo MODE_0 | nc -w 5 192.168.0.105 9999"'
  set_inverter_sol: '/bin/sh -c "echo MODE_1 | nc -w 5 192.168.0.105 9999"'
  set_inverter_sbu: '/bin/sh -c "echo MODE_2 | nc -w 5 192.168.0.105 9999"'
  set_inverter_sub: '/bin/sh -c "echo MODE_3 | nc -w 5 192.168.0.105 9999"'
  set_inverter_suf: '/bin/sh -c "echo MODE_4 | nc -w 5 192.168.0.105 9999"'
  set_charger_cso: '/bin/sh -c "echo CSO_SET | nc -w 5 192.168.0.105 9999"'
  set_charger_snu: '/bin/sh -c "echo SNU_SET | nc -w 5 192.168.0.105 9999"'
  set_charger_oso: '/bin/sh -c "echo OSO_SET | nc -w 5 192.168.0.105 9999"'
  set_charge_amps: '/bin/sh -c "echo SET_AMPS_{{ states("input_select.inverter_max_ac_amps") }} | nc -w 5 192.168.0.105 9999"'
  set_soc_grid: '/bin/sh -c "echo SET_SOC_GRID_{{ states("input_number.inverter_soc_grid") | int }} | nc -w 5 192.168.0.105 9999"'
  set_soc_batt: '/bin/sh -c "echo SET_SOC_BATT_{{ states("input_number.inverter_soc_batt") | int }} | nc -w 5 192.168.0.105 9999"'
  set_soc_cutoff: '/bin/sh -c "echo SET_SOC_CUTOFF_{{ states("input_number.inverter_soc_cutoff") | int }} | nc -w 5 192.168.0.105 9999"'
  set_ac_range: >
    /bin/sh -c "echo SET_AC_RANGE_{% if is_state('input_select.inverter_ac_range', 'Appliances (APL)') %}0{% elif is_state('input_select.inverter_ac_range', 'UPS (UPS)') %}1{% else %}2{% endif %} | nc -w 5 192.168.0.105 9999"
  set_buzzer_mute: '/bin/sh -c "echo SET_BUZZER_0 | nc -w 5 192.168.0.105 9999"'
  set_buzzer_nd2: '/bin/sh -c "echo SET_BUZZER_1 | nc -w 5 192.168.0.105 9999"'
  set_buzzer_nd3: '/bin/sh -c "echo SET_BUZZER_2 | nc -w 5 192.168.0.105 9999"'
  set_buzzer_fault: '/bin/sh -c "echo SET_BUZZER_3 | nc -w 5 192.168.0.105 9999"'
  
  set_backlight_on: '/bin/sh -c "echo SET_BACKLIGHT_1 | nc -w 5 192.168.0.105 9999"'
  set_backlight_off: '/bin/sh -c "echo SET_BACKLIGHT_0 | nc -w 5 192.168.0.105 9999"'


command_line:
  - sensor:
      name: "Inverter Bridge Data"
      # Give it 3 seconds to fetch JSON (sensors are fast, but 3s is safer)
      command: 'echo "JSON" | nc -w 3 192.168.0.105 9999' 
      scan_interval: 1  # 2 seconds is a good balance
      value_template: "Online"
      json_attributes:
        - ac_output_amp
        - ac_load_real_watt
        - ac_load_va
        - grid_current
        - buzzer_mode    
        - backlight_status
        - fault_code
        - ac_input_range
        - max_ac_amps
        - temp_dc     
        - temp_inv
        - device_status   
        - ac_load_pct  
        - charger_priority
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
        - soc_back_to_grid   # Prg 43
        - soc_back_to_batt   # Prg 44
        - soc_cutoff         # Prg 45

  - switch:
      name: "Grid Charging"
      unique_id: grid_chargingz
      command_timeout: 5
      # These send commands that the script now maps to Register 316
      command_on: 'echo "CHARGE_ON" | nc -w 5 192.168.0.105 9999'
      command_off: 'echo "CHARGE_OFF" | nc -w 5 192.168.0.105 9999'
      
      # We check the NEW "charger_priority" value from the JSON
      command_state: 'echo "JSON" | nc -w 3 192.168.0.105 9999 | jq ".charger_priority"'
      
      # ON only if Priority is 2 (SNU/Solar+Utility)
      value_template: "{{ value == '2' }}"
      icon: mdi:flash


template:
  
  

  - switch:
      - name: "Inverter LCD Backlight"
        unique_id: inverter_backlight_switch
        state: "{{ state_attr('sensor.inverter_bridge_data', 'backlight_status') == 1 }}"
        turn_on:
          service: shell_command.set_backlight_on
        turn_off:
          service: shell_command.set_backlight_off
        icon: mdi:monitor-shimmer
        
  - sensor:
      - name: "House Load Apparent Power"
        unique_id: inv_load_apparent_power
        unit_of_measurement: "VA"
        device_class: apparent_power
        state: >
          {{ state_attr('sensor.inverter_bridge_data', 'ac_load_va') | float(0) }}
        icon: mdi:flash-outline  

      - name: "House Load Power"
        unique_id: inv_house_load_watts
        unit_of_measurement: "W"
        device_class: power
        state: >
          {{ state_attr('sensor.inverter_bridge_data', 'ac_load_real_watt') | float(0) }}

      - name: "Output Power Factor"
        unique_id: inv_output_pf
        unit_of_measurement: "PF"    
        state_class: measurement
        state: >
          {% set real = state_attr('sensor.inverter_bridge_data', 'ac_load_real_watt') | float(0) %}
          {% set va = state_attr('sensor.inverter_bridge_data', 'ac_load_va') | float(0) %}
          {% if va > 0 %}
            {{ (real / va) | round(2) }}
          {% else %}
            1.0
          {% endif %}

      - name: "Inverter Fault Status"
        unique_id: inv_fault_text
        icon: mdi:alert-circle-outline
        state: >
          {% set code = state_attr('sensor.inverter_bridge_data', 'fault_code') | int(default=0) %}
          {% set faults = {
            0: "Normal",
            1: "Fan Locked (01)",
            2: "Over Temperature (02)",
            3: "Batt Voltage High (03)",
            4: "Low Battery (04)",
            5: "Output Short Circuit (05)",
            6: "Output Voltage High (06)",
            7: "Overload Time Out (07)",
            8: "Bus Voltage High (08)",
            9: "Bus Soft Start Fail (09)",
            51: "Over Current (51)",
            52: "Bus Voltage Low (52)",
            53: "Inverter Soft Start Fail (53)",
            55: "DC Voltage High (55)",
            57: "Current Sensor Fail (57)",
            58: "Output Voltage Low (58)"
          } %}
          {{ faults.get(code, "Unknown Error " + code|string) }}
          
  - sensor:
      - name: "Inverter Temp (DC)"
        state: "{{ state_attr('sensor.inverter_bridge_data', 'temp_dc') }}"
        unit_of_measurement: "°C"
        unique_id: inv_dc_temp
        device_class: temperature

      - name: "Inverter Temp (AC)"
        state: "{{ state_attr('sensor.inverter_bridge_data', 'temp_inv') }}"
        unit_of_measurement: "°C"
        unique_id: inv_ac_temp
        device_class: temperature

  - sensor:
      - name: "Inverter Load Percentage"
        state: "{{ state_attr('sensor.inverter_bridge_data', 'ac_load_pct') }}"
        unit_of_measurement: "%"
        icon: mdi:percent

      - name: "Inverter Status"
        unique_id: inv_device_status
        state: >
          {% set raw_status = state_attr('sensor.inverter_bridge_data', 'device_status') | int(default=-1) %}
          
          {% if raw_status == 2 %} Line Mode (On-Grid)
          {% elif raw_status == 3 %} Battery Mode (Off-Grid)
          {% elif raw_status == 0 %} Standby / Power Off
          {% elif raw_status == 1 %} Fault / Error
          {% else %} Unknown ({{ raw_status }})
          {% endif %}
        icon: mdi:information-outline
        
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

      - name: "Battery Current (Calibrated)"
        unique_id: inv_batt_current_calibrated
        unit_of_measurement: "A"
        device_class: current
        state: >
          {% set raw_amps = state_attr('sensor.inverter_bridge_data', 'batt_current') | float(0) %}
          {# Calibrate only if charging, or always if you prefer. 0.92 = 69A/75A #}
          {{ (raw_amps * 0.92) | round(1) }}
        icon: mdi:current-dc
        
  - sensor:
      - name: "BMS Battery Percentage"
        unique_id: inv_batt_soc
        unit_of_measurement: "%"
        device_class: battery
        state_class: measurement
        state: >
          {{ state_attr('sensor.inverter_bridge_data', 'batt_soc') }}
          
  - sensor:
      - name: "Battery Power Flow"
        unique_id: inv_batt_power
        unit_of_measurement: "W"
        device_class: power
        state: >
          {{ state_attr('sensor.inverter_bridge_data', 'batt_power_watt') }}
      
  - sensor:
      - name: "Battery Power Flow (Calibrated)"
        unique_id: inv_batt_power_calibrated
        unit_of_measurement: "W"
        device_class: power
        state: >
          {% set raw = state_attr('sensor.inverter_bridge_data', 'batt_power_watt') | float(0) %}
          {# Only calibrate charging (negative values) because discharge efficiency is different #}
          {% if raw < 0 %}
            {{ (raw * 0.86) | round(0) }}
          {% else %}
            {{ raw }}
          {% endif %}
        icon: mdi:battery-charging

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

```

Inverter: Sync ALL Settings from Device (exclude it from the logs, it will flood them)
paset it in the ui:
```yaml
alias: "Inverter: Sync ALL Settings from Device"
description: Updates all HA Dropdowns and Sliders when Inverter settings change externally
triggers:
  - entity_id: sensor.inverter_bridge_data
    trigger: state
actions:
  - variables:
      mode: >-
        {{ state_attr('sensor.inverter_bridge_data', 'output_mode') |
        int(default=0) }}
      range: >-
        {{ state_attr('sensor.inverter_bridge_data', 'ac_input_range') |
        int(default=0) }}
      prio: >-
        {{ state_attr('sensor.inverter_bridge_data', 'charger_priority') |
        int(default=3) }}
      buzzer: >-
        {{ state_attr('sensor.inverter_bridge_data', 'buzzer_mode') |
        int(default=3) }}
      amps: >-
        {{ state_attr('sensor.inverter_bridge_data', 'max_ac_amps') |
        int(default=30) }}
      soc_grid: >-
        {{ state_attr('sensor.inverter_bridge_data', 'soc_back_to_grid') |
        int(default=50) }}
      soc_batt: >-
        {{ state_attr('sensor.inverter_bridge_data', 'soc_back_to_batt') |
        int(default=100) }}
      soc_cut: >-
        {{ state_attr('sensor.inverter_bridge_data', 'soc_cutoff') |
        int(default=20) }}
  - action: input_select.select_option
    target:
      entity_id: input_select.inverter_mode
    data:
      option: >
        {% if mode == 0 %} Utility First (UTI) {% elif mode == 1 %} Solar First
        (SOL) {% elif mode == 2 %} SBU (Solar-Batt-Util) {% elif mode == 3 %}
        SUB (Solar-Util-Batt) {% elif mode == 4 %} SUF (GRID Feedback) {% endif
        %}
  - action: input_select.select_option
    target:
      entity_id: input_select.inverter_ac_range
    data:
      option: >
        {% if range == 0 %} Appliances (APL) {% elif range == 1 %} UPS (UPS) {%
        elif range == 2 %} Generator (GEN) {% endif %}
  - action: input_select.select_option
    target:
      entity_id: input_select.inverter_charger_priority
    data:
      option: >
        {% if prio == 1 %} Solar First (CSO) {% elif prio == 2 %} Solar +
        Utility (SNU) {% else %} Solar Only (OSO) {% endif %}
  - action: input_select.select_option
    target:
      entity_id: input_select.inverter_buzzer_mode
    data:
      option: >
        {% if buzzer == 0 %} Mute (nd1) {% elif buzzer == 1 %} Source/Warn/Fault
        (nd2) {% elif buzzer == 2 %} Warn/Fault (nd3) {% else %} Fault Only
        (nd4) {% endif %}
  - action: input_select.select_option
    target:
      entity_id: input_select.inverter_max_ac_amps
    data:
      option: "{{ amps | string }}"
  - action: input_number.set_value
    target:
      entity_id: input_number.inverter_soc_grid
    data:
      value: "{{ soc_grid }}"
  - action: input_number.set_value
    target:
      entity_id: input_number.inverter_soc_batt
    data:
      value: "{{ soc_batt }}"
  - action: input_number.set_value
    target:
      entity_id: input_number.inverter_soc_cutoff
    data:
      value: "{{ soc_cut }}"
mode: single
max_exceeded: silent


```
Rest of the automation, paste them in automations.yaml:
```yaml
- id: '1765747246000'
  alias: 'Inverter: Set Priority Mode'
  description: Sends command to inverter when Dropdown changes in UI
  triggers:
  - entity_id: input_select.inverter_mode
    trigger: state
  actions:
  - choose:
    - conditions:
      - condition: template
        value_template: '{{ trigger.to_state.state == ''Utility First (UTI)'' }}'
      sequence:
      - action: shell_command.set_inverter_uti
    - conditions:
      - condition: template
        value_template: '{{ trigger.to_state.state == ''Solar First (SOL)'' }}'
      sequence:
      - action: shell_command.set_inverter_sol
    - conditions:
      - condition: template
        value_template: '{{ trigger.to_state.state == ''SBU (Solar-Batt-Util)'' }}'
      sequence:
      - action: shell_command.set_inverter_sbu
    - conditions:
      - condition: template
        value_template: '{{ trigger.to_state.state == ''SUB (Solar-Util-Batt)'' }}'
      sequence:
      - action: shell_command.set_inverter_sub
    - conditions:
      - condition: template
        value_template: '{{ trigger.to_state.state == ''SUF (GRID Feedback)'' }}'
      sequence:
      - action: shell_command.set_inverter_suf
  mode: restart
- id: '1765777872842'
  alias: 'Inverter: Set Charger Priority'
  description: ''
  triggers:
  - entity_id: input_select.inverter_charger_priority
    trigger: state
  actions:
  - choose:
    - conditions: '{{ trigger.to_state.state == ''Solar First (CSO)'' }}'
      sequence:
      - action: shell_command.set_charger_cso
    - conditions: '{{ trigger.to_state.state == ''Solar + Utility (SNU)'' }}'
      sequence:
      - action: shell_command.set_charger_snu
    - conditions: '{{ trigger.to_state.state == ''Solar Only (OSO)'' }}'
      sequence:
      - action: shell_command.set_charger_oso
  mode: single
- id: '1765815798845'
  alias: 'Inverter: Set Max AC Amps'
  description: ''
  triggers:
  - entity_id: input_select.inverter_max_ac_amps
    trigger: state
  actions:
  - action: shell_command.set_charge_amps
  mode: single
- id: '1765820126314'
  alias: 'Inverter: Set Back to Grid'
  description: ''
  triggers:
  - entity_id: input_number.inverter_soc_grid
    trigger: state
  actions:
  - action: shell_command.set_soc_grid
- id: '1765820142770'
  alias: 'Inverter: Set Back to Battery'
  description: ''
  triggers:
  - entity_id: input_number.inverter_soc_batt
    trigger: state
  actions:
  - action: shell_command.set_soc_batt
- id: '1765820168559'
  alias: 'Inverter: Set Cut-off'
  description: ''
  triggers:
  - entity_id: input_number.inverter_soc_cutoff
    trigger: state
  actions:
  - action: shell_command.set_soc_cutoff
- id: '1765823166092'
  alias: 'Inverter: Set AC Range'
  description: ''
  triggers:
  - entity_id: input_select.inverter_ac_range
    trigger: state
  actions:
  - action: shell_command.set_ac_range
- id: '1765863406672'
  alias: 'Inverter: Set Buzzer Mode'
  description: ''
  triggers:
  - entity_id: input_select.inverter_buzzer_mode
    trigger: state
  actions:
  - choose:
    - conditions: '{{ trigger.to_state.state == ''Mute (nd1)'' }}'
      sequence:
        action: shell_command.set_buzzer_mute
    - conditions: '{{ trigger.to_state.state == ''Source/Warn/Fault (nd2)'' }}'
      sequence:
        action: shell_command.set_buzzer_nd2
    - conditions: '{{ trigger.to_state.state == ''Warn/Fault (nd3)'' }}'
      sequence:
        action: shell_command.set_buzzer_nd3
    - conditions: '{{ trigger.to_state.state == ''Fault Only (nd4)'' }}'
      sequence:
        action: shell_command.set_buzzer_fault

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

Status: The official app will show "Offline". This is normal and indicates the hijack is working.













