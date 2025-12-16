# Local Cloud Bridge for Anenji / Easun / MPP Solar Inverters

**Unchain your inverter from the cloud.**

This project provides a fully local, privacy-focused control system for "Cloud-Only" Hybrid Inverters. These devices are commonly sold under brands like **Anenji**, **Easun**, **MPP Solar**, and others that use the **Desmonitor**, **SmartEss**, or **WatchPower** mobile apps.

By hijacking the inverter's network traffic and redirecting it to a local Python script, we achieve **1-second real-time updates**, complete offline control, and instant integration with Home Assistant—without voiding the warranty, opening the case, or using RS232 adapters.

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

1. **Compatible Inverter:** Any hybrid inverter using the WiFi dongle or built-in WiFi card that connects to `server.desmonitor.com` (IP: `8.218.202.213`) or similar Chinese cloud servers.
   * *Verified Hardware:* ANENJI ANJ-6200W-48V

2. **Network Control:** You need a way to intercept your inverter's traffic and redirect it to your local server. Choose the method that matches your network setup:

   **Method A: Router with Firewall Access (Recommended)**
   - Works with: OpenWRT, pfSense, OPNsense, MikroTik, or most consumer routers with "Port Forwarding" features
   - This is the cleanest solution because it works at the network layer
   - You'll create a NAT rule that redirects TCP port 18899 traffic to your bridge server

   **Method B: DNS Rewrite + Bridge Server with iptables**
   - Works with: AdGuard Home, Pi-hole, or any DNS server + Linux bridge server with root access
   - Use this if you can't access your router's firewall settings
   - **Important:** DNS rewrite alone is NOT enough - you also need iptables rules on your bridge server
   - This combines DNS resolution with local port forwarding

   **❌ What WON'T Work:**
   - DNS rewrite alone (AdGuard/Pi-hole without iptables) - DNS can only change IP addresses, not port numbers
   - Manually editing hosts files on the inverter (these devices don't typically allow this)

3. **Local Server:** A Raspberry Pi, Proxmox LXC, or Docker container to run the bridge script. This server must:
   - Be always-on and have a static IP address (e.g., `192.168.0.105`)
   - Run Python 3.6 or newer
   - Have network access to your inverter

---

**Which method should you choose?**

- **If you have an OpenWRT/pfSense router or can access your router's advanced settings:** Use Method A - it's simpler and more reliable.
- **If you only have AdGuard Home/Pi-hole and cannot touch your router:** Use Method B - but be prepared to configure both DNS and iptables.
- **If you're unsure:** Check if your router has a "Port Forwarding" or "Virtual Server" page in its admin panel. If yes, use Method A. If no, use Method B.

---

## ⚙️ Architecture

The inverter is hard-coded to communicate with a remote cloud server (`server.desmonitor.com` / `8.218.202.213`) on TCP port **18899**. We perform a "Man-in-the-Middle" attack on our own device:

1. **The Hijack:** The router intercepts traffic destined for the cloud (TCP 18899) and redirects it to the local server IP (`192.168.0.105`).
2. **The Bridge:** The `inverter_bridge.py` script listens on port 18899. It accepts the connection, reads the Modbus registers, and exposes the data via a lightweight JSON API.
3. **The Interface:** Home Assistant queries this API every second using `netcat` (nc) for zero-latency updates.

---

## 🛠️ Installation

### Step 1: Network Hijack

You must prevent the inverter from reaching the real internet and force it to talk to your server.

#### Method A: OpenWRT / pfSense / Consumer Router (Recommended)

Add a **Port Forwarding (DNAT)** rule to your router's firewall to redirect cloud traffic to your local bridge server.

**Step 1: Identify Your Inverter's Cloud Server**

First, check what your inverter is trying to reach:
- Common domain: `server.desmonitor.com`
- Common IP: `8.218.202.213`
- Port: `18899` (TCP)

You can verify this by checking your router's connection logs or DNS query logs.

**Step 2: Configure Port Forwarding**

**For OpenWRT:**

Add these rules to your firewall configuration:

```ini
config redirect 'inverter_hijack'
	option name 'Inverter Hijack'
	option src 'lan'
	option proto 'tcp'
	option src_ip '192.168.0.111'        # Your inverter's IP
	option src_dip '8.218.202.213'       # Cloud server IP
	option src_dport '18899'
	option dest_ip '192.168.0.105'       # Your bridge server IP
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

Or via LuCI web interface:
1. Go to **Network** → **Firewall** → **Port Forwards**
2. Click **Add**
3. Configure:
   - **Name:** `Inverter Hijack`
   - **Protocol:** TCP
   - **Source zone:** LAN
   - **Source IP:** `192.168.0.111` (your inverter)
   - **External IP:** `8.218.202.213` (cloud server)
   - **External port:** `18899`
   - **Internal IP:** `192.168.0.105` (bridge server)
   - **Internal port:** `18899`

**For pfSense/OPNsense:**

1. Go to **Firewall** → **NAT** → **Port Forward**
2. Click **Add**
3. Configure:
   - **Interface:** LAN
   - **Protocol:** TCP
   - **Source:** Single host or alias → `192.168.0.111` (inverter IP)
   - **Destination:** Single host → `8.218.202.213` (cloud server)
   - **Destination port:** `18899`
   - **Redirect target IP:** `192.168.0.105` (bridge server)
   - **Redirect target port:** `18899`

**For Consumer Routers (TP-Link, Asus, Netgear, etc.):**

Most consumer routers don't support source-based NAT rules. You may need to:
1. Block the cloud server IP (`8.218.202.213`) in the router's firewall
2. Use Method B (DNS + iptables) instead

---

#### Method B: DNS Rewrite (Pi-hole / AdGuard Home) + iptables

**⚠️ Important:** DNS rewrite alone is NOT sufficient. DNS can only change IP addresses, not TCP ports. You must also configure iptables on your bridge server to complete the redirect.

**Step 1: Find Your Inverter's Cloud Server**

Check your DNS server's query log to identify the domain:

- **Pi-hole:** Go to **Query Log** and filter by your inverter's IP
- **AdGuard Home:** Go to **Query Log** and look for requests from your inverter (e.g., `192.168.0.111`)
- Common domains: `server.desmonitor.com`, `server.smarten-ess.com`
- Common IPs: `8.218.202.213`

**Step 2: Configure DNS Rewrite**

Add a DNS rewrite rule pointing the cloud server to your bridge server:

- **Pi-hole:** 
  - Go to **Local DNS** → **DNS Records**
  - Add domain: `server.desmonitor.com`
  - Add IP: `192.168.0.105` (your bridge server IP)

- **AdGuard Home:**
  - Go to **Filters** → **DNS Rewrites** → **Add DNS Rewrite**
  - Domain: `server.desmonitor.com`
  - Answer: `192.168.0.105` (your bridge server IP)

**Step 3: Enable IP Forwarding on Bridge Server**

SSH into your bridge server and enable packet forwarding:

```bash
# Enable IP forwarding temporarily
echo 1 > /proc/sys/net/ipv4/ip_forward

# Make it permanent across reboots
echo "net.ipv4.ip_forward=1" >> /etc/sysctl.conf
sysctl -p
```

**Step 4: Configure iptables Port Redirect**

Add a NAT rule to redirect incoming traffic on port 18899:

```bash
# Create the redirect rule
iptables -t nat -A PREROUTING -p tcp --dport 18899 -j REDIRECT --to-port 18899

# Save the rules
mkdir -p /etc/iptables
iptables-save > /etc/iptables/rules.v4
```

**Step 5: Make iptables Rules Persistent**

Create a systemd service to restore rules after reboot:

```bash
cat > /etc/systemd/system/iptables-restore.service << 'EOF'
[Unit]
Description=Restore iptables rules for Inverter Bridge
Before=network-pre.target
Wants=network-pre.target

[Service]
Type=oneshot
ExecStart=/sbin/iptables-restore /etc/iptables/rules.v4
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF

# Enable and start the service
systemctl enable iptables-restore
systemctl start iptables-restore
```

**Step 6: Verify Configuration**

Test that everything is set up correctly:

```bash
# 1. Check IP forwarding is enabled
cat /proc/sys/net/ipv4/ip_forward
# Expected output: 1

# 2. Verify iptables rule exists
iptables -t nat -L PREROUTING -n -v
# Should show a REDIRECT rule for tcp dpt:18899

# 3. Test DNS resolution (from another device on your network)
nslookup server.desmonitor.com
# Should return: 192.168.0.105 (your bridge server IP)

# 4. Check bridge service is running
systemctl status inverter-bridge
# Should show: active (running)
```

**Troubleshooting Method B:**

| Issue | Solution |
|-------|----------|
| Inverter still shows offline | Check DNS server query log - confirm requests resolve to your bridge IP |
| "Connection refused" error | Verify bridge script is running: `systemctl status inverter-bridge` |
| Rules disappear after reboot | Check if systemd service is enabled: `systemctl is-enabled iptables-restore` |
| iptables command fails | Install iptables: `apt install iptables` (Debian/Ubuntu) or `yum install iptables` (CentOS/RHEL) |

**Note for Proxmox LXC Users:**

If your bridge server runs in an LXC container, you must enable nesting to use iptables:

```bash
# On Proxmox host, edit container config (replace CTID with your container ID)
nano /etc/pve/lxc/CTID.conf

# Add this line:
features: nesting=1

# Save and restart container
pct reboot CTID
```

---

### Step 2: Install the Bridge Service

1. Upload the `inverter_bridge.py` script to your server (e.g., `/root/inverter_bridge.py`).

### Step 3: Run as a Service

Create a systemd service to keep it running forever.

**File:** `/etc/systemd/system/inverter-bridge.service`

```ini
[Unit]
Description=Inverter Modbus TCP Bridge
After=network.target

[Service]
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

Add this to your `configuration.yaml`. We use `nc` (Netcat) instead of Python for the command line to ensure sub-1-second performance.


```yaml
input_number:
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
  grid_charge_on: '/bin/sh -c "echo CHARGE_ON | nc -w 5 192.168.0.105 9999"'
  grid_charge_off: '/bin/sh -c "echo CHARGE_OFF | nc -w 5 192.168.0.105 9999"'

command_line:
  - sensor:
      name: "Inverter Bridge Data"
      command: 'echo "JSON" | nc -w 3 192.168.0.105 9999' 
      scan_interval: 2
      value_template: "Online"
      json_attributes:
        # --- NEW TEXT FIELDS ---
        - fault_msg             # The text description (e.g., "BMS Communication Fail")
        - device_status_msg     # The text status (e.g., "Fault Mode")
        - device_status_code    # The numeric status (0-9)
        # -----------------------
        - fault_code
        - fault_bitmask         # Optional: for debugging
        - warning_bitmask       # Optional: for debugging
        - ac_output_amp
        - ac_load_real_watt
        - ac_load_va
        - grid_current
        - buzzer_mode    
        - backlight_status
        - ac_input_range
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

      - name: "Grid Charging"
        unique_id: grid_chargingz_template
        # LOGIC: 
        # Read 'charger_priority' from the main bridge sensor.
        # 2 = SNU (Solar + Utility) = Grid Charging ON
        # 3 = OSO (Solar Only) = Grid Charging OFF
        state: "{{ state_attr('sensor.inverter_bridge_data', 'charger_priority') | int(default=3) == 2 }}"
        turn_on:
          service: shell_command.grid_charge_on
        turn_off:
          service: shell_command.grid_charge_off
        
        icon: mdi:flash

  - sensor:
      - name: "Inverter Status"
        unique_id: inv_device_status
        state: "{{ state_attr('sensor.inverter_bridge_data', 'device_status_msg') }}"
        icon: mdi:information-outline

      - name: "Inverter Fault Message"
        state: "{{ state_attr('sensor.inverter_bridge_data', 'fault_msg') }}"
        icon: mdi:alert-circle

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

      - name: "Inverter Load Percentage"
        state: "{{ state_attr('sensor.inverter_bridge_data', 'ac_load_pct') }}"
        unit_of_measurement: "%"
        icon: mdi:percent
        
      - name: "Grid Input Power"
        unique_id: inv_grid_power
        unit_of_measurement: "W"
        device_class: power
        state: "{{ state_attr('sensor.inverter_bridge_data', 'grid_power_watt') }}"

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
        
      - name: "BMS Battery Percentage"
        unique_id: inv_batt_soc
        unit_of_measurement: "%"
        device_class: battery
        state_class: measurement
        state: >
          {{ state_attr('sensor.inverter_bridge_data', 'batt_soc') }}
          
      - name: "Battery Power Flow"
        unique_id: inv_batt_power
        unit_of_measurement: "W"
        device_class: power
        state: >
          {{ state_attr('sensor.inverter_bridge_data', 'batt_power_watt') }}
      
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

      - name: "Grid Voltage"
        unique_id: inv_grid_voltage
        unit_of_measurement: "V"
        device_class: voltage
        state: >
          {{ state_attr('sensor.inverter_bridge_data', 'grid_volt') }}

      - name: "Output Voltage"
        unique_id: inv_out_voltage
        unit_of_measurement: "V"
        device_class: voltage
        state: >
          {{ state_attr('sensor.inverter_bridge_data', 'ac_out_volt') }}

      - name: "Battery Voltage (Inverter)"
        unique_id: inv_batt_voltage
        unit_of_measurement: "V"
        device_class: voltage
        state: >
          {{ state_attr('sensor.inverter_bridge_data', 'batt_volt') }}

      - name: "PV Input Voltage"
        unique_id: inv_pv_voltage
        unit_of_measurement: "V"
        device_class: voltage
        state: >
          {{ state_attr('sensor.inverter_bridge_data', 'pv_input_volt') }}

      - name: "PV Input Power"
        unique_id: inv_pv_power
        unit_of_measurement: "W"
        device_class: power
        state: >
          {{ state_attr('sensor.inverter_bridge_data', 'pv_input_watt') }}

```

Automation: "Inverter: Sync ALL Settings from Device" (exclude it from the logbook or database, it will flood them)
Paste this as a new automation in the ui:

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

Rest of the automation, add them to automations.yaml:
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

| Register | Function | Unit / Description | Script Variable |
| :--- | :--- | :--- | :--- |
| **101** | **Fault Code** | **Numeric Error Code (e.g. 19)** | `vals_fault[1]` |
| **104** | **Fault Bitmask** | **Bit 3 = Error 19 (Battery Open)** | `vals_fault[4]` |
| **105** | **Warning Bitmask** | **Bit flags for Warnings** | `vals_fault[5]` |
| **201** | Device Status | 0=Standby, 2=Line, 3=Batt, 1/4=Fault | `vals[1]` |
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




