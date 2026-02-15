# Local Cloud Bridge for Anenji / Easun / MPP Solar Inverters

**Unchain your inverter from the cloud.**

This project provides a fully local, privacy-focused control system for "Cloud-Only" Hybrid Inverters. These devices are commonly sold under brands like **Anenji**, **Easun**, **MPP Solar**, and others that use the **Desmonitor**, **SmartEss**, or **WatchPower** mobile apps.

The current registers are for SRNE based single phased inverters. For Voltronic/Axpert based models, those would likely differ and need adjustment.

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

2. **Network Control (Choose One):**
   * **Method A (Router-Based):** OpenWRT / pfSense Router.
   * **Method B (Bridge-Based):** Linux Server acting as the Inverter's Gateway (Robust, works even if main router dies).

3. **Local Server:** A Linux system (Raspberry Pi, Proxmox LXC, Docker) with a **Static IP** (e.g., `192.168.0.105`).

---

## 🛠️ Installation

### Step 0: Choose Your Hijack Method 🚀

#### Option A: The Router Method (For OpenWRT Users)
If you have an OpenWRT router, simply try to add this block to `/etc/config/firewall` to redirect the cloud IP (`8.218.202.213`) to your local bridge.

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

### Option B: Stand-alone / Debian LXC or VM, Raspberry Pi Zero W, etc

Use this if you don't have an Openwrt router. Disable the build in dhcp server, use Dnsmasq instead (careful about remaining locked out).

1. Install Dependencies (on the Bridge Server):

```bash
apt update && apt install dnsmasq iptables-persistent -y
```
2. Configure DHCP (/etc/dnsmasq.conf): This forces the Inverter to use the Bridge (192.168.0.105) as its Gateway, while other devices use the normal router (.1).

```ini
# Core Settings
interface=eth0
bind-interfaces
port=0 # Disables DNS to avoid conflicts

# DHCP Range
dhcp-range=192.168.0.100,192.168.0.240,12h

# Global Options (Standard Devices -> Main Router)
dhcp-option=6,8.8.8.8              # DNS
dhcp-option=3,192.168.0.1          # Standard Gateway

# Inverter Specific (Reservation & Hijack)
# Replace AA:BB:CC... with your Inverter Dongle MAC
dhcp-host=AA:BB:CC:DD:EE:FF,192.168.0.111,Solar-Inverter,set:solar_inverter

# Send the Bridge IP (.105) as Gateway ONLY to the tagged inverter
dhcp-option=tag:solar_inverter,3,192.168.0.105
````
3. Configure Routing & Firewall: Run these commands to enable traffic forwarding and hijack the solar port locally.

```bash
# 1. Enable IP Forwarding (Required for the LXC to act as a Gateway)
sysctl -w net.ipv4.ip_forward=1

# 2. Redirect "Control" Traffic to Local Script
# Any TCP packet from the Dongle (192.168.0.111) destined for Port 18899 
# is grabbed and sent to the LXC's local port 18899.
iptables -t nat -A PREROUTING -s 192.168.0.111 -p tcp --dport 18899 -j REDIRECT --to-port 18899

# 3. Redirect "Data Logging" Traffic to Local Script
# We send Port 38899 to 18899 as well, because your script's new "AT+DTUPN?" 
# logic is universal enough to handle the initial handshake for both.
iptables -t nat -A PREROUTING -s 192.168.0.111 -p tcp --dport 38899 -j REDIRECT --to-port 18899

# 4. BLOCK any other Internet Access (The "Kill Switch")
# trying to pass through the LXC to the WAN. 
iptables -A FORWARD -s 192.168.0.111 -j DROP

# 5. Save the Rules
# Ensures these persist after a reboot.
netfilter-persistent save
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

[Install]
WantedBy=multi-user.target
```


Enable it:
```bash
systemctl daemon-reload
systemctl enable --now inverter-bridge
```

### Step 4: Home Assistant Configuration

Copy the configuration and automation files from this repository into your Home Assistant setup. Replace `192.168.0.105` with your bridge server's IP address throughout.

| File | HA Location | Description |
| :--- | :--- | :--- |
| [`configuration.yml`](configuration.yml) | `configuration.yaml` (or a package include) | Input selects, shell commands, sensors, template entities, utility meters |
| [`automations.yml`](automations.yml) | `automations.yaml` | Write-back automations and device-to-HA sync |
**Isolate HA issues:**
This command prints all data on any terminal on local network:

```terminal
echo "JSON" | nc -w 1 <bridge ip> 9999
```
```json
{"fault_code": 0, "fault_msg": "No Fault", "warning_code": 99, "warning_msg": "Warning Active", "device_status_code": 3, "device_status_msg": "Battery Mode", "fault_bitmask": 0, "warning_bitmask": 65, "batt_volt": 52.5, "ac_load_va": 1081, "ac_load_real_watt": 1036, "ac_load_pct": 17.4, "batt_power_watt": 1120, "grid_power_watt": 0, "ac_output_amp": 4.7, "pv_input_watt": 0, "pv_input_volt": 32.4, "pv_current": 0.0, "batt_soc": 63, "temp_dc": 32, "temp_inv": 27, "max_total_amps": 120.0, "max_ac_amps": 70.0, "batt_current": 21.3, "grid_volt": 0.0, "grid_freq": 0.0, "ac_out_volt": 230.0, "ac_out_amp": 4.7, "return_to_default": 0, "charger_priority": 3, "output_mode": 3, "ac_input_range": 1, "buzzer_mode": 0, "backlight_status": 1, "soc_back_to_grid": 10, "soc_back_to_batt": 60, "soc_cutoff": 3, "grid_current": 0.0, "inverter_temp": 27, "grid_charge_setting": 0, "total_pv_energy_kwh": 4269.3469, "total_grid_input_kwh": 14.3268, "total_load_kwh": 13.0326, "total_battery_charge_kwh": 5.3052, "total_battery_discharge_kwh": 4.6439, "ac_load_watt": 1036}
```

### 📊 Register Map

| Register | Function | Unit / Description | Script Variable |
| :--- | :--- | :--- | :--- |
| **100-101** | Fault Code | 32-bit Combined Fault Flags (High/Low) | `vf[0], vf[1]` |
| **108-109** | Warning Code | 32-bit Combined Warning Flags (High/Low) | `vf[8], vf[9]` |
| **201** | Device Status | 0=Power On, 1=Standby, 2=Line, 3=Batt, etc. | `vals[1]` |
| **202** | Grid Voltage | 0.1 V | `vals[2]` |
| **203** | Grid Frequency | 0.01 Hz | `vals[3]` |
| **204** | Grid Power | Watts (Power drawn from Grid) | `vals[4]` |
| **205** | Output Voltage | 0.1 V | `vals[5]` |
| **211** | Output Current | 0.1 A (Load Amps) | `vals[11]` |
| **213** | Active Output Power | Watts (Real House Load) | `vals[13]` |
| **214** | Apparent Output | VA (Volt-Amps) | `vals[14]` |
| **215** | Battery Voltage | 0.1 V | `vals[15]` |
| **219** | PV Voltage | 0.1 V | `vals[19]` |
| **223** | PV Input Power | Watts (Total PV) | `vals[23]` |
| **224** | PV Charging Power | Watts (Solar to Battery) | `vals[24]` |
| **226** | Inverter Temp | °C | `vals[26]` |
| **227** | DC/Heatsink Temp | °C | `vals[27]` |
| **229** | Battery SOC | Percentage % | `vals[29]` |
| **232** | Net Battery Current | 0.1 A (Signed: +Charging, -Discharging) | `vals[32]` |
| **301** | Output Mode | 0=UTI, 1=SOL, 2=SBU, 3=SUB, 4=SUF | `v300[0]` |
| **302** | AC Input Range | 0=Appliances, 1=UPS, 2=Gen | `v300[1]` |
| **303** | Buzzer Mode | 0=Mute, 1=Src/Warn/Flt, 2=Warn/Flt, 3=Flt | `v300[2]` |
| **305** | LCD Backlight | 0=Off, 1=On | `v300[4]` |
| **306** | Return to Default | 0=Disabled, 1=Enabled | `v300[5]` |
| **322** | Battery Type | 0=AGN, 1=FLD, 2=USR, 4=LI2, 6=LI4, 8=LIb | `v322[0]` |
| **324** | Bulk Charge Volt | 0.1 V | `v322[2]` |
| **325** | Float Charge Volt | 0.1 V | `v322[3]` |
| **329** | Low DC Cutoff Volt | 0.1 V | `v322[7]` |
| **331** | Charger Priority | 1=Solar(CSO), 2=Solar+Grid(SNU), 3=Only Solar(OSO) | `v330[0]` |
| **332** | Max Total Amps | 0.1 A (Total Charging Current) | `v330[1]` |
| **333** | Max AC Amps | 0.1 A (Grid Charging Current) | `v330[2]` |
| **341** | SOC Back to Grid | Percentage % | `vsoc[0]` |
| **342** | SOC Back to Batt | Percentage % | `vsoc[1]` |
| **343** | SOC Cut-off | Percentage % | `vsoc[2]` |

### 🧮 Derived Sensors Map

| Sensor | Formula | Unit / Description | Script Variable |
| :--- | :--- | :--- | :--- |
| **Grid Current** | `grid_power_watt / grid_volt` | A (Amperes drawn from grid) | `latest_data_json["grid_current"]` |
| **Battery Current** | `vals[32] / 10.0` | A (Signed Net Current) | `latest_data_json["batt_current"]` |
| **Battery Power** | `batt_current * batt_volt` | W (Signed Net Power) | `latest_data_json["batt_power_watt"]` |
| **PV Current** | `pv_input_watt / pv_input_volt` | A (Solar panel current) | `latest_data_json["pv_current"]` |
| **AC Load Percentage** | `min((ac_load_va / 6200) * 100, 300)` | % (Load relative to rated 6200W) | `latest_data_json["ac_load_pct"]` |
| **Total PV Energy** | `∫(p_pv * dt) / 3600000` | kWh (Cumulative solar production) | `energy_data["total_pv_kwh"]` |
| **Total Grid Input** | `∫(p_grid * dt) / 3600000` (when p_grid > 0) | kWh (Cumulative grid consumption) | `energy_data["total_grid_input_kwh"]` |
| **Total Load Energy** | `∫(p_load * dt) / 3600000` | kWh (Cumulative household consumption) | `energy_data["total_load_kwh"]` |
| **Total Battery Charge** | `∫(batt_p * dt) / 3600000` (when batt_p > 0) | kWh (Cumulative energy charged into battery) | `energy_data["total_battery_charge_kwh"]` |
| **Total Battery Discharge** | `∫(abs(batt_p) * dt) / 3600000` (when batt_p < 0) | kWh (Cumulative energy discharged from battery) | `energy_data["total_battery_discharge_kwh"]` |


## ⚠️ Disclaimer & Safety Warning

**Use at your own risk.** This project is not affiliated with Anenji, Easun, MPP Solar, or any other manufacturer.

* **⚡ Active Control Risk:** This bridge now supports **writing settings** to the inverter (Registers 300+). Changing physical parameters like **Max Charging Amps** or **Battery Cut-off Limits** can stress your battery or inverter if set incorrectly. Always verify your battery's datasheet before changing these values in Home Assistant.
* **🔌 Cloud Disconnection:** By design, this bridge **hijacks** the inverter's network traffic. The official mobile app will permanently show **"Offline"**, and you will **not** receive firmware updates from the manufacturer while this script is running.
* **🛠️ Expert Use Only:** While the read-logic is safe, the write-logic touches the inverter's internal memory. Do not modify the `shell_command` values in `configuration.yaml` unless you understand the Modbus protocol specific to your device.














