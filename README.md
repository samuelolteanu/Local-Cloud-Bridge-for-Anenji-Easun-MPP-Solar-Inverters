# Local Cloud Bridge for Anenji / Easun / MPP Solar Inverters

**Unchain your inverter from the cloud.**

This project provides a fully local, privacy-focused control system for "Cloud-Only" Hybrid Inverters. These devices are commonly sold under brands like **Anenji**, **Easun**, **MPP Solar**, and others that use the **Desmonitor**, **SmartEss**, or **WatchPower** mobile apps.

By hijacking the inverter's network traffic and redirecting it to a local Python script, we achieve **1-second real-time updates**, complete offline control, and instant integration with Home Assistant—without voiding the warranty, opening the case, or using RS232 adapters.

## 🚀 Features

* **⚡ Real-Time 1-Second Updates:** Replaces the slow 5-minute cloud refresh rate with instant high-frequency polling.
* **🔒 100% Local Control:** Acts as a transparent TCP bridge. No data is sent to external cloud servers; the system works entirely offline.
* **🎛️ Full Device Management:** Change critical settings instantly from Home Assistant: 
    * **Output Modes:** Switch between UTI, SOL, SBU, SUB, and SUF.
    * **Battery Management:** Set AC Charging Amps and specific SOC Thresholds.
    * **System Controls:** Toggle Buzzer, LCD Backlight, and AC Input Range.
* **🔋 Smart Calculations:** Auto-calculates Real-time Battery Current, PV Current, and Net Power.
* **🛠 No Hardware Mods:** Uses the inverter's existing WiFi dongle.

---

## 📋 Prerequisites

1. **Compatible Inverter:** Hybrid inverter with WiFi dongle (Anenji, Easun, etc.).
   * *Verified Hardware:* ANENJI ANJ-6200W-48V

2. **Network Control:** * **Method A (Best):** OpenWRT / pfSense Router (Robust).
   * **Method B (Alternative):** Consumer Router + AdGuard Home/Pi-hole + Linux Bridge. (NOT TESTED)

3. **Local Server:** A Linux system (Raspberry Pi, Proxmox LXC, Docker) with a **Static IP** (e.g., `192.168.0.105`).

---

## 🛠️ Installation

### Step 0: The OpenWRT "Fast Track" 🚀
Simply add this block to your `/etc/config/firewall` file to redirect the hardcoded Chinese cloud IP (`8.218.202.213`) to your local bridge and skip Step 1. If it's not working, go back Step 1.

**Edit:** `/etc/config/firewall`

```ini
config redirect 'inverter_hijack'
    option name 'Inverter Hijack'
    option src 'lan'
    option proto 'tcp'
    option src_ip '192.168.0.111'     # Your Inverter IP
    option src_dip '8.218.202.213'    # The common Cloud IP (Verified on Anenji)
    option src_dport '18899'          # The common Cloud Port
    option dest_ip '192.168.0.105'    # Your Bridge Server IP
    option dest_port '18899'
    option target 'DNAT'

config nat 'inverter_snat'
    option name 'Inverter Loopback'
    option src 'lan'
    option proto 'tcp'
    option dest_ip '192.168.0.105'
    option dest_port '18899'
    option target 'MASQUERADE'
```

### Step 1: Identify Your Cloud Target 🕵️

Even if using the "Catch-All" method, it is good to confirm the port.
Since you have a Linux server on the same network, use it to sniff the traffic.

1.  **Install tools:** `apt update && apt install dsniff tcpdump`
2.  **Spoof the traffic:** Tell the inverter (`192.168.0.111`) that YOU are the router (`192.168.0.1`).
    ```bash
    # Replace IPs with: [Inverter IP] [Router IP]
    arpspoof -i eth0 -t 192.168.0.111 192.168.0.1
    ```
    
    *(Leave running in Terminal 1)*
2.  **Watch DNS queries:** In Terminal 2: `tcpdump -i eth0`
    * **You should see this:**
    
    ```terminal
    14:26:59.963092 IP 192.168.0.111.51118 > 8.218.202.213.18899: Flags [S], seq 4912356, win 4380, options [mss 1460], length 0
    ```

---

### Step 2: Configure Network Hijack

We use a **"Catch-All" Port Redirect**. This works even if the inverter uses a hardcoded IP or changes domains.

**OpenWRT Configuration:**

**Network** -> **Firewall** -> **Port Forwards**:
* **Name:** `Inverter Hijack`
* **Protocol:** `TCP`
* **Source Zone:** `LAN`
* **Source IP:** `192.168.0.111` (Your Inverter's IP)
* **Source Port:** `Any`
* **External IP:** `Any` (Leave blank or 0.0.0.0/0)
* **External Port:** `18899` (The Cloud Port)
* **Internal Zone:** `LAN`
* **Internal IP:** `192.168.0.105` (Your Bridge Server)
* **Internal Port:** `18899`

**Network** -> **Firewall** -> **NAT Rules**:
* **Name:** `Inverter Loopback`
* **Protocol:** `TCP`
* **Source Zone:** `LAN`
* **Source IP:** `192.168.0.111` (Your Inverter's IP)
* **Source Port:** `Any`
* **External IP:** `Any` (Leave blank or 0.0.0.0/0)
* **External Port:** `18899` (The Cloud Port)
* **Internal Zone:** `LAN`
* **Internal IP:** `192.168.0.105` (Your Bridge Server)
* **Internal Port:** `18899`
* **Action:** `MASQUERADE`

---

### Step 3: Install the Bridge Service

1. Upload `inverter_service.py` to `/root/inverter_service.py`.

2. Create the systemd service: `/etc/systemd/system/inverter-bridge.service`

```ini
[Unit]
Description=Inverter Modbus TCP Bridge
After=network.target

[Service]
ExecStart=/usr/bin/python3 -u /root/inverter_service.py
WorkingDirectory=/root
StandardOutput=inherit
StandardError=inherit
Restart=always
RestartSec=5
User=root
```
[Install]
WantedBy=multi-user.target

Enable it:
```bash
systemctl daemon-reload
systemctl enable --now inverter-bridge
```

### Step 4: Home Assistant Configuration

Add this to your `configuration.yaml`. We use `nc` (Netcat) instead of Python for the command line to ensure sub-1-second performance.


```yaml
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
  set_soc_grid_direct: '/bin/sh -c "echo SET_SOC_GRID_{{ val }} | nc -w 5 192.168.0.105 9999"'
  set_soc_batt_direct: '/bin/sh -c "echo SET_SOC_BATT_{{ val }} | nc -w 5 192.168.0.105 9999"'
  set_soc_cutoff_direct: '/bin/sh -c "echo SET_SOC_CUTOFF_{{ val }} | nc -w 5 192.168.0.105 9999"'
  set_ac_range: >
    /bin/sh -c "echo SET_AC_RANGE_{% if is_state('input_select.inverter_ac_range', 'Appliances (APL)') %}0{% elif is_state('input_select.inverter_ac_range', 'UPS (UPS)') %}1{% else %}2{% endif %} | nc -w 5 192.168.0.105 9999"
  set_buzzer_mute: '/bin/sh -c "echo SET_BUZZER_0 | nc -w 5 192.168.0.105 9999"'
  set_buzzer_nd2: '/bin/sh -c "echo SET_BUZZER_1 | nc -w 5 192.168.0.105 9999"'
  set_buzzer_nd3: '/bin/sh -c "echo SET_BUZZER_2 | nc -w 5 192.168.0.105 9999"'
  set_buzzer_fault: '/bin/sh -c "echo SET_BUZZER_3 | nc -w 5 192.168.0.105 9999"'
  set_backlight_on: '/bin/sh -c "echo SET_BACKLIGHT_1 | nc -w 5 192.168.0.105 9999"'
  set_backlight_off: '/bin/sh -c "echo SET_BACKLIGHT_0 | nc -w 5 192.168.0.105 9999"'
  grid_charge_on: '/bin/sh -c "echo CHARGE_ON | nc -w 5 192.168.0.105 9999"'
  grid_charge_off: '/bin/sh -c "echo CHARGE_OFF | nc -w 5 192.168.0.105 9999"'
  set_return_default_on: '/bin/sh -c "echo SET_RETURN_DEFAULT_1 | nc -w 5 192.168.0.105 9999"'
  set_return_default_off: '/bin/sh -c "echo SET_RETURN_DEFAULT_0 | nc -w 5 192.168.0.105 9999"'
  set_charge_amps_direct: '/bin/sh -c "echo SET_AMPS_{{ val }} | nc -w 5 192.168.0.105 9999"'
  set_total_amps_direct: '/bin/sh -c "echo SET_TOTAL_AMPS_{{ val }} | nc -w 5 192.168.0.105 9999"'
# --- SENSOR CONFIGURATION ---
command_line:
  - sensor:
      name: "Inverter Bridge Data"
      # Using -w 3 ensures it fails/timeouts if bridge is down
      command: 'echo "JSON" | nc -w 3 192.168.0.105 9999' 
      scan_interval: 2
      # Only mark as "Online" if we actually got valid JSON data
      value_template: >
        {% if value_json is defined %}
          Online
        {% else %}
          Offline
        {% endif %}
      # If the command fails (exit code 1), the sensor becomes Unavailable automatically.
      json_attributes:
        - fault_msg
        - warning_code
        - warning_msg
        - device_status_msg
        - device_status_code
        - fault_code
        - fault_bitmask
        - warning_bitmask
        - return_to_default
        - ac_output_amp
        - ac_load_real_watt
        - ac_load_va
        - grid_current
        - buzzer_mode
        - backlight_status
        - ac_input_range
        - max_total_amps
        - max_ac_amps
        - temp_dc
        - temp_inv
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
        - soc_back_to_grid 
        - soc_back_to_batt 
        - soc_cutoff
        - grid_freq

template:
  - number:
      - name: "Max Charging Current (Total)"
        unique_id: max_charging_current_total
        icon: mdi:battery-charging-high
        state: "{{ state_attr('sensor.inverter_bridge_data', 'max_total_amps') | float(0) }}"
        availability: "{{ states('sensor.inverter_bridge_data') == 'Online' }}"
        set_value:
          service: shell_command.set_total_amps_direct
          data:
            val: "{{ value | int }}"
        min: 10
        max: 120
        step: 1

      - name: "Max AC Charge Amps"
        unique_id: num_max_ac_amps
        min: 5
        max: 80
        step: 1
        unit_of_measurement: "A"
        icon: mdi:current-ac
        state: "{{ state_attr('sensor.inverter_bridge_data', 'max_ac_amps') | int(0) }}"
        availability: "{{ states('sensor.inverter_bridge_data') == 'Online' }}"
        set_value:
          service: shell_command.set_charge_amps_direct
          data:
            val: "{{ value | int }}"

      - name: "Back to Grid SOC (Prg 43)"
        unique_id: num_soc_grid
        min: 4
        max: 50
        step: 1
        unit_of_measurement: "%"
        icon: mdi:battery-arrow-down
        state: "{{ state_attr('sensor.inverter_bridge_data', 'soc_back_to_grid') | int(0) }}"
        availability: "{{ states('sensor.inverter_bridge_data') == 'Online' }}"
        set_value:
          service: shell_command.set_soc_grid_direct
          data:
            val: "{{ value | int }}"

      - name: "Back to Battery SOC (Prg 44)"
        unique_id: num_soc_batt
        min: 60
        max: 100
        step: 1
        unit_of_measurement: "%"
        icon: mdi:battery-arrow-up
        state: "{{ state_attr('sensor.inverter_bridge_data', 'soc_back_to_batt') | int(0) }}"
        availability: "{{ states('sensor.inverter_bridge_data') == 'Online' }}"
        set_value:
          service: shell_command.set_soc_batt_direct
          data:
            val: "{{ value | int }}"

      - name: "Cut-off SOC (Prg 45)"
        unique_id: num_soc_cutoff
        min: 3
        max: 30
        step: 1
        unit_of_measurement: "%"
        icon: mdi:battery-alert
        state: "{{ state_attr('sensor.inverter_bridge_data', 'soc_cutoff') | int(0) }}"
        availability: "{{ states('sensor.inverter_bridge_data') == 'Online' }}"
        set_value:
          service: shell_command.set_soc_cutoff_direct
          data:
            val: "{{ value | int }}"
            
  - switch:
      - name: "Inverter LCD Backlight"
        unique_id: inverter_backlight_switch
        state: "{{ state_attr('sensor.inverter_bridge_data', 'backlight_status') == 1 }}"
        # This switch becomes Unavailable if the bridge is down
        availability: "{{ states('sensor.inverter_bridge_data') == 'Online' }}"
        turn_on:
          service: shell_command.set_backlight_on
        turn_off:
          service: shell_command.set_backlight_off
        icon: mdi:monitor-shimmer

      - name: "Grid Charging"
        unique_id: grid_chargingz_template
        state: "{{ state_attr('sensor.inverter_bridge_data', 'charger_priority') | int(default=3) == 2 }}"
        # This switch becomes Unavailable if the bridge is down
        availability: "{{ states('sensor.inverter_bridge_data') == 'Online' }}"
        turn_on:
          service: shell_command.grid_charge_on
        turn_off:
          service: shell_command.grid_charge_off
        icon: mdi:flash
      
      - name: "Inverter Return to Default Screen"
        unique_id: inverter_return_default_switch
        state: "{{ state_attr('sensor.inverter_bridge_data', 'return_to_default') == 1 }}"
        availability: "{{ states('sensor.inverter_bridge_data') == 'Online' }}"
        turn_on:
          service: shell_command.set_return_default_on
        turn_off:
          service: shell_command.set_return_default_off
        icon: mdi:arrow-u-left-top

  - sensor:
      # 1. Device Status (e.g., "Line Mode", "Battery Mode", "Warning Mode")
      - name: "Inverter Status"
        unique_id: inv_device_status
        icon: mdi:information-outline
        state: "{{ state_attr('sensor.inverter_bridge_data', 'device_status_msg') }}"
        availability: "{{ states('sensor.inverter_bridge_data') == 'Online' }}"

      # 2. Fault/Error Message (e.g., "No Fault", "Inverter current offset is too high")
      # This is for CRITICAL errors that stop the machine.
      - name: "Inverter Fault Message"
        unique_id: inv_fault_msg
        icon: mdi:alert-circle
        state: "{{ state_attr('sensor.inverter_bridge_data', 'fault_msg') }}"
        availability: "{{ states('sensor.inverter_bridge_data') == 'Online' }}"

      # 3. Warning Message (e.g., "No Warning", "BMS Communication Fail")
      # This is for ALERTS where the machine keeps running (Status 4).
      - name: "Inverter Warning Message"
        unique_id: inv_warning_msg
        icon: mdi:alert
        state: "{{ state_attr('sensor.inverter_bridge_data', 'warning_msg') }}"
        availability: "{{ states('sensor.inverter_bridge_data') == 'Online' }}"

      - name: "House Load Apparent Power"
        unique_id: inv_load_apparent_power
        unit_of_measurement: "VA"
        device_class: apparent_power
        state: "{{ state_attr('sensor.inverter_bridge_data', 'ac_load_va')}}"
        availability: "{{ states('sensor.inverter_bridge_data') == 'Online' }}"
        icon: mdi:flash-outline  

      - name: "House Load Power"
        unique_id: inv_house_load_watts
        unit_of_measurement: "W"
        device_class: power
        state: "{{ state_attr('sensor.inverter_bridge_data', 'ac_load_real_watt')}}"
        availability: "{{ states('sensor.inverter_bridge_data') == 'Online' }}"

      - name: "Output Power Factor"
        unique_id: inv_output_pf
        unit_of_measurement: "PF"    
        state_class: measurement
        availability: "{{ states('sensor.inverter_bridge_data') == 'Online' }}"
        state: >
          {% set real = state_attr('sensor.inverter_bridge_data', 'ac_load_real_watt') %}
          {% set va = state_attr('sensor.inverter_bridge_data', 'ac_load_va') %}
          {% if is_number(real) and is_number(va) %}
            {% if va | float > 0 %}
              {{ (real | float / va | float) | round(2) }}
            {% else %}
              1.0
            {% endif %}
          {% else %}
            None
          {% endif %}
          
      - name: "Inverter Temp (DC)"
        state: "{{ state_attr('sensor.inverter_bridge_data', 'temp_dc') }}"
        unit_of_measurement: "°C"
        unique_id: inv_dc_temp
        device_class: temperature
        availability: "{{ states('sensor.inverter_bridge_data') == 'Online' }}"

      - name: "Inverter Temp (AC)"
        state: "{{ state_attr('sensor.inverter_bridge_data', 'temp_inv') }}"
        unit_of_measurement: "°C"
        unique_id: inv_ac_temp
        device_class: temperature
        availability: "{{ states('sensor.inverter_bridge_data') == 'Online' }}"

      - name: "Inverter Load Percentage"
        state: "{{ state_attr('sensor.inverter_bridge_data', 'ac_load_pct') }}"
        unit_of_measurement: "%"
        icon: mdi:percent
        availability: "{{ states('sensor.inverter_bridge_data') == 'Online' }}"
        
      - name: "Grid Input Power"
        unique_id: inv_grid_power
        unit_of_measurement: "W"
        device_class: power
        state: "{{ state_attr('sensor.inverter_bridge_data', 'grid_power_watt') }}"
        availability: "{{ states('sensor.inverter_bridge_data') == 'Online' }}"

      - name: "PV Current"
        unique_id: inv_pv_current
        unit_of_measurement: "A"
        state: "{{ state_attr('sensor.inverter_bridge_data', 'pv_current') }}"
        availability: "{{ states('sensor.inverter_bridge_data') == 'Online' }}"
        
      - name: "Battery Current"
        unique_id: inv_batt_current
        unit_of_measurement: "A"
        state: "{{ state_attr('sensor.inverter_bridge_data', 'batt_current') }}"
        availability: "{{ states('sensor.inverter_bridge_data') == 'Online' }}"

      - name: "Battery Current (Calibrated)"
        unique_id: inv_batt_current_calibrated
        unit_of_measurement: "A"
        device_class: current
        availability: "{{ states('sensor.inverter_bridge_data') == 'Online' }}"
        state: >
          {% set raw_amps = state_attr('sensor.inverter_bridge_data', 'batt_current') %}
          
          {# Only calculate if raw_amps is actually a number #}
          {% if is_number(raw_amps) %}
            {{ (raw_amps | float * 0.92) | round(1) }}
          {% else %}
            None
          {% endif %}
        icon: mdi:current-dc
        
      - name: "BMS Battery Percentage"
        unique_id: inv_batt_soc
        unit_of_measurement: "%"
        device_class: battery
        state_class: measurement
        state: "{{ state_attr('sensor.inverter_bridge_data', 'batt_soc') }}"
        availability: "{{ states('sensor.inverter_bridge_data') == 'Online' }}"
          
      - name: "Battery Power Flow"
        unique_id: inv_batt_power
        unit_of_measurement: "W"
        device_class: power
        state: "{{ state_attr('sensor.inverter_bridge_data', 'batt_power_watt') }}"
        availability: "{{ states('sensor.inverter_bridge_data') == 'Online' }}"
      
      - name: "Battery Power Flow (Calibrated)"
        unique_id: inv_batt_power_calibrated
        unit_of_measurement: "W"
        device_class: power
        availability: "{{ states('sensor.inverter_bridge_data') == 'Online' }}"
        state: >
          {% set raw = state_attr('sensor.inverter_bridge_data', 'batt_power_watt') %}
          {% if is_number(raw) %}
            {% set raw_f = raw | float %}
            {% if raw_f < 0 %}
              {{ (raw_f * 0.86) | round(0) }}
            {% else %}
              {{ raw_f }}
            {% endif %}
          {% else %}
            None
          {% endif %}
        icon: mdi:battery-charging

      - name: "Grid Voltage"
        unique_id: inv_grid_voltage
        unit_of_measurement: "V"
        device_class: voltage
        state: "{{ state_attr('sensor.inverter_bridge_data', 'grid_volt') }}"
        availability: "{{ states('sensor.inverter_bridge_data') == 'Online' }}"

      - name: "Grid Frequency"
        unique_id: inv_grid_freq
        unit_of_measurement: "Hz"
        device_class: frequency
        state: "{{ state_attr('sensor.inverter_bridge_data', 'grid_freq') }}"
        availability: "{{ states('sensor.inverter_bridge_data') == 'Online' }}"

      - name: "Output Voltage"
        unique_id: inv_out_voltage
        unit_of_measurement: "V"
        device_class: voltage
        state: "{{ state_attr('sensor.inverter_bridge_data', 'ac_out_volt') }}"
        availability: "{{ states('sensor.inverter_bridge_data') == 'Online' }}"

      - name: "Battery Voltage (Inverter)"
        unique_id: inv_batt_voltage
        unit_of_measurement: "V"
        device_class: voltage
        state: "{{ state_attr('sensor.inverter_bridge_data', 'batt_volt') }}"
        availability: "{{ states('sensor.inverter_bridge_data') == 'Online' }}"

      - name: "PV Input Voltage"
        unique_id: inv_pv_voltage
        unit_of_measurement: "V"
        device_class: voltage
        state: "{{ state_attr('sensor.inverter_bridge_data', 'pv_input_volt') }}"
        availability: "{{ states('sensor.inverter_bridge_data') == 'Online' }}"

      - name: "PV Input Power"
        unique_id: inv_pv_power
        unit_of_measurement: "W"
        device_class: power
        state: "{{ state_attr('sensor.inverter_bridge_data', 'pv_input_watt') }}"
        availability: "{{ states('sensor.inverter_bridge_data') == 'Online' }}"
```

automations.yaml:

```yaml
- id: '1765747246000'
  alias: 'Inverter: Set Priority Mode'
  triggers:
  - entity_id: input_select.inverter_mode
    trigger: state
  conditions:
  - condition: template
    value_template: '{{ trigger.to_state.context.user_id != None }}'
  actions:
  - choose:
    - conditions: '{{ trigger.to_state.state == ''Utility First (UTI)'' }}'
      sequence:
      - action: shell_command.set_inverter_uti
    - conditions: '{{ trigger.to_state.state == ''Solar First (SOL)'' }}'
      sequence:
      - action: shell_command.set_inverter_sol
    - conditions: '{{ trigger.to_state.state == ''SBU (Solar-Batt-Util)'' }}'
      sequence:
      - action: shell_command.set_inverter_sbu
    - conditions: '{{ trigger.to_state.state == ''SUB (Solar-Util-Batt)'' }}'
      sequence:
      - action: shell_command.set_inverter_sub
    - conditions: '{{ trigger.to_state.state == ''SUF (GRID Feedback)'' }}'
      sequence:
      - action: shell_command.set_inverter_suf
  mode: restart
- id: '1765777872842'
  alias: 'Inverter: Set Charger Priority'
  triggers:
  - entity_id: input_select.inverter_charger_priority
    trigger: state
  conditions:
  - condition: template
    value_template: '{{ trigger.to_state.context.user_id != None }}'
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
- id: '1765823166092'
  alias: 'Inverter: Set AC Range'
  triggers:
  - entity_id: input_select.inverter_ac_range
    trigger: state
  conditions:
  - condition: template
    value_template: '{{ trigger.to_state.context.user_id != None }}'
  actions:
  - action: shell_command.set_ac_range
- id: '1765863406672'
  alias: 'Inverter: Set Buzzer Mode'
  triggers:
  - entity_id: input_select.inverter_buzzer_mode
    trigger: state
  conditions:
  - condition: template
    value_template: '{{ trigger.to_state.context.user_id != None }}'
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
- id: '1765864027241'
  alias: 'Inverter: Sync ALL Settings from Device'
  triggers:
  - entity_id: sensor.inverter_bridge_data
    trigger: state
  actions:
  - variables:
      mode_raw: '{{ state_attr(''sensor.inverter_bridge_data'', ''output_mode'') }}'
      range_raw: '{{ state_attr(''sensor.inverter_bridge_data'', ''ac_input_range'')
        }}'
      prio_raw: '{{ state_attr(''sensor.inverter_bridge_data'', ''charger_priority'')
        }}'
      buzzer_raw: '{{ state_attr(''sensor.inverter_bridge_data'', ''buzzer_mode'')
        }}'
      amps_raw: '{{ state_attr(''sensor.inverter_bridge_data'', ''max_ac_amps'') }}'
      soc_grid_raw: '{{ state_attr(''sensor.inverter_bridge_data'', ''soc_back_to_grid'')
        }}'
      soc_batt_raw: '{{ state_attr(''sensor.inverter_bridge_data'', ''soc_back_to_batt'')
        }}'
      soc_cut_raw: '{{ state_attr(''sensor.inverter_bridge_data'', ''soc_cutoff'')
        }}'
  - choose:
    - conditions: '{{ mode_raw is not none }}'
      sequence:
      - action: input_select.select_option
        target:
          entity_id: input_select.inverter_mode
        data:
          option: '{% set m = mode_raw | int %} {% if m == 0 %}Utility First (UTI)
            {% elif m == 1 %}Solar First (SOL) {% elif m == 2 %}SBU (Solar-Batt-Util)
            {% elif m == 3 %}SUB (Solar-Util-Batt) {% elif m == 4 %}SUF (GRID Feedback)
            {% else %}{{ states(''input_select.inverter_mode'') }}{% endif %}

            '
  - choose:
    - conditions: '{{ range_raw is not none }}'
      sequence:
      - action: input_select.select_option
        target:
          entity_id: input_select.inverter_ac_range
        data:
          option: '{% set r = range_raw | int %} {% if r == 0 %}Appliances (APL) {%
            elif r == 1 %}UPS (UPS) {% elif r == 2 %}Generator (GEN) {% else %}{{
            states(''input_select.inverter_ac_range'') }}{% endif %}

            '
  - choose:
    - conditions: '{{ prio_raw is not none }}'
      sequence:
      - action: input_select.select_option
        target:
          entity_id: input_select.inverter_charger_priority
        data:
          option: '{% set p = prio_raw | int %} {% if p == 1 %}Solar First (CSO) {%
            elif p == 2 %}Solar + Utility (SNU) {% elif p == 3 %}Solar Only (OSO)
            {% else %}{{ states(''input_select.inverter_charger_priority'') }}{% endif
            %}

            '
  mode: single
  max_exceeded: silent


  
```
Useful commands:

```bash
systemctl stop inverter-bridge.service
nano /etc/systemd/system/inverter-bridge.service
systemctl daemon-reload
systemctl start inverter-bridge
systemctl status inverter-bridge
```

**Isolate HA issues:**
This command prints all data on any terminal on local network:

```terminal
echo "JSON" | nc -w 1 <bridge ip> 9999
```
```json
{"fault_code": 0, "fault_msg": "No Fault", "warning_code": 0, "warning_msg": "No Warning", "device_status_code": 3, "device_status_msg": "Battery Mode", "fault_bitmask": 0, "warning_bitmask": 65, "charger_priority": 3, "output_mode": 3, "ac_input_range": 1, "buzzer_mode": 0, "backlight_status": 1, "return_to_default": 0, "batt_volt": 52.6, "ac_load_va": 1194, "ac_load_real_watt": 855, "ac_load_pct": 19.3, "batt_power_watt": 936, "grid_power_watt": 0, "ac_output_amp": 5.2, "pv_input_watt": 0, "pv_input_volt": 135.3, "pv_current": 0.0, "batt_soc": 59, "temp_dc": 26, "temp_inv": 30, "max_total_amps": 101.0, "max_ac_amps": 70.0, "batt_current": 17.8, "soc_back_to_grid": 10, "soc_back_to_batt": 60, "soc_cutoff": 3, "grid_volt": 0.0, "grid_freq": 0.0, "ac_out_volt": 229.8}
```

### 📊 Register Map

| Register | Function | Unit / Description | Script Variable |
| :--- | :--- | :--- | :--- |
| **101** | **Fault Code** | **Numeric Error Code (e.g. 19)** | `vals_fault[1]` |
| **104** | **Warning Bitmask 1** | **Primary Warnings**<br>Bit 6 = Battery Open (bP)<br>Bit 8 = Low Battery (04) | `vals_fault[4]` |
| **105** | **Warning Bitmask 2** | **Critical / Hidden Warnings**<br>Bit 0 = System Fault (01)<br>Bit 6 = Internal Batt Relay Open (64)<br>Bit 12 = Battery Cutoff / Recovery (4096) | `vals_fault[5]` |
| **201** | Device Status | 0=Standby, 2=Line, 3=Batt, 1=Fault | `vals[1]` |
| **202** | Grid Voltage | 0.1 V | `vals[2]` |
| **203** | Grid Frequency | 0.01 Hz | `vals[3]` |
| **204** | Grid Power | Watts (Power drawn from Grid) | `vals[4]` |
| **205** | Output Voltage | 0.1 V | `vals[5]` |
| **208** | **Batt Discharge** | **Signed Int (Net Power)** | `vals[8]` |
| **209** | **Batt Charge** | **Watts (Charging Only)** | `vals[9]` |
| **211** | Output Current | 0.1 A (Load Amps) | `vals[11]` |
| **213** | **Active Output Power** | **Watts (Real House Load)** | `vals[13]` |
| **214** | **Apparent Output** | **VA (Volt-Amps)** | `vals[14]` |
| **215** | Battery Voltage | 0.1 V | `vals[15]` |
| **219** | **PV Voltage** | **0.1 V** | `vals[19]` |
| **223** | **PV Power** | **Watts** | `vals[23]` |
| **226** | DC/Heatsink Temp | °C | `vals[26]` |
| **227** | **Inverter Temp** | **°C** | `vals[27]` |
| **229** | Battery SOC | Percentage % | `vals[29]` |
| **301** | Output Mode | 0=UTI, 1=SOL, 2=SBU, 3=SUB, 4=SUF | `vals_300[0]` |
| **302** | AC Input Range | 0=Appliances, 1=UPS, 2=Gen | `vals_300[1]` |
| **303** | Buzzer Mode | 0=Mute, 1=Src/Warn/Flt, 2=Warn/Flt, 3=Flt | `vals_300[2]` |
| **305** | **LCD Backlight** | **0=Off, 1=On** | `vals_300[4]` |
| **306** | **Auto Return Screen** | **0=Disabled, 1=Enabled (LCD Set 19)** | `vals_306[0]` |
| **331** | Charger Priority | 1=Solar(CSO), 2=Solar+Grid(SNU), 3=Only Solar(OSO) | `vals_prio[0]` |
| **333** | Max AC Charge | 0.1 A (e.g. 300 = 30A) | `vals_amps[0]` |
| **341** | SOC Back to Grid | Percentage % | `vals_soc[0]` |
| **342** | SOC Back to Batt | Percentage % | `vals_soc[1]` |
| **343** | SOC Cut-off | Percentage % | `vals_soc[2]` |



## ⚠️ Disclaimer & Safety Warning

**Use at your own risk.** This project is not affiliated with Anenji, Easun, MPP Solar, or any other manufacturer.

* **⚡ Active Control Risk:** This bridge now supports **writing settings** to the inverter (Registers 300+). Changing physical parameters like **Max Charging Amps** or **Battery Cut-off Limits** can stress your battery or inverter if set incorrectly. Always verify your battery's datasheet before changing these values in Home Assistant.
* **🔌 Cloud Disconnection:** By design, this bridge **hijacks** the inverter's network traffic. The official mobile app will permanently show **"Offline"**, and you will **not** receive firmware updates from the manufacturer while this script is running.
* **🛠️ Expert Use Only:** While the read-logic is safe, the write-logic touches the inverter's internal memory. Do not modify the `shell_command` values in `configuration.yaml` unless you understand the Modbus protocol specific to your device.





