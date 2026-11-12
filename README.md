# Unified Solar MQTT Bridge & Web LCD (Anenji / Easun / Voltronic / Sumry)
<img width="3840" height="2160" alt="image" src="https://github.com/user-attachments/assets/8b9e6527-1c23-4b3a-9c30-536715f77de7" />


**Cloud-optional by design. Sub-second local telemetry, native Home Assistant MQTT Discovery, and a live Web LCD for hybrid solar inverters.**

This daemon bridges hybrid solar inverters commonly sold under **Anenji**, **Easun**, **MPP Solar**, **Sumry**, and **PowMr** directly into **Home Assistant** via MQTT. It eliminates the 5-minute polling delay and external server dependency of apps like *SmartEss*, *Desmonitor*, and *WatchPower*, replacing them with **1.5-second local polling**, full bi-directional parameter control, and an interactive **Voltronic-style Web LCD**.

Version 2.0 unifies two distinct hardware architectures into a single multi-threaded service:

1. **Sumry / SRNE Modbus RTU-over-TCP (`anenji_tcp`):** Intercepts traffic from the stock Wi-Fi dongle over the local network (e.g., **Anenji ANJ-6200W-48V**).

2. **Voltronic Axpert PI30 Serial (`easun_serial`):** Communicates directly over a standard **USB-B to USB-A cable** (or RS232 adapter) connected to the inverter's USB communication port (e.g., **Easun ISolar SML II 5.5kW**).

## 🚀 Key Features

* **📡 Zero-YAML Home Assistant Integration:** Full **MQTT Auto-Discovery** (`homeassistant/+`). Every sensor, dropdown (`select`), voltage/current limit (`number`), and toggle (`switch`) registers automatically under device cards in Home Assistant. Automatically cleans up legacy discovery topics on startup.

* **🖥️ Interactive Web LCD & Kiosk Mode:** Built-in web server (`:8080`) featuring a pixel-accurate SVG reproduction of the inverter's physical blue LCD screen:

  * Animated proportional power flow lines, dynamic AC bypass routing, HVDC bus boxes, solar ray gauges, and load/grid wedges.

  * Multi-inverter **Main + Island** topology view with real-time 48V DC bus cross-transfer indicators (`▲` / `▼` Watts between inverters). Automatically hides the Island inverter's AC output branch when running in pure MPPT charge-only mode (physical power switch OFF).

  * **1000 imp/kWh Utility-Meter Pulse LEDs:** The `AC/INV` and `CHG` status LEDs under the LCD bezel act as real-time optical energy meter pulse indicators, flashing once for every **1 Wh** ($1000\text{ pulses/kWh}$) of inverting or charging energy accumulated.

  * **DPI Scaling & Fullscreen Kiosk Mode (`?kiosk=1`):** Fine-tune text, icon, box, and line scaling in browser `localStorage`, or double-click to toggle dedicated wall-tablet display mode.

* **🛡️ Hardware-Safe Write Engine:**

  * **EEPROM Wear Protection:** Blocks redundant writes if the target register/parameter already matches the requested value, and enforces per-register cooldowns (5s on Anenji, 3s on Easun).

  * **Sync Gate:** Blocks incoming MQTT write commands until initial holding registers (`300-343` / `QPIRI` + `QFLAG`) are read and verified from the hardware.

  * **ACK Verification & Command Fallbacks:** Verifies Modbus Func 16 (`0x10`) and PI30 `ACK` responses before updating state, automatically falling back to alternate PI30 opcodes (`MCHGC` -> `MNCHGC0`, lowercase/uppercase `PE`/`PD` flags) when needed.

* **🧠 Live Telemetry Diagnostics (No Speculative Bitmasks):** Replaces noisy, permanently latched BMS/cloud warning bitmasks with a deterministic live behavior engine. Evaluates real-time voltages, frequencies, MPPT bounds, HVDC bus limits, temperatures, and loads into actionable **Faults**, **Serious Warnings**, and **Info Warnings**, while still polling hardware fault registers (`Reg 100-101` / `QPIWS`).

* **🔋 Precision Energy Integration:** Riemann-sum kWh counters (`Solar`, `Grid Import`, `House Load`, `Battery Charge`, `Battery Discharge`) saved atomically to disk every 5 minutes and on `SIGTERM`/`SIGINT`. Energy entities stay available across inverter reboots to prevent Home Assistant Energy Dashboard spikes. Includes low-wattage ADC compensation on Voltronic/Easun units.

## 🔌 First Connection & Hardware Setup

You can run a single inverter or a combined **Main (Grid-tied/Hybrid) + Island (Off-Grid)** dual-inverter setup sharing a common 48V battery bank.

### 1. Anenji / Sumry (Wi-Fi Dongle TCP Redirection)

The Anenji Wi-Fi dongle is pre-programmed to open an outbound TCP connection to a remote cloud server (`8.218.202.213` on ports `18899` and `38899`). Instead of opening the inverter or building a custom RS232 cable, you redirect that outbound connection to your local Linux host running this script.

When the dongle connects to port `18899`, the bridge emulates the cloud server by sending the `AT+DTUPN?\r\n` wake-up query. Once the dongle replies with its serial number, the bridge switches the socket into raw **Modbus RTU-over-TCP** polling. *(Secondary logging connections from port `38899` that do not answer the handshake are automatically filtered and closed).*

#### Option A: Router DNAT Redirect (OpenWRT / pfSense / MikroTik)

If your main router supports custom DNAT rules, redirect traffic from the dongle's static IP (`192.168.0.111`) destined for port `18899` to your bridge server (`192.168.0.105`).

**OpenWRT (`/etc/config/firewall`):**

```
config redirect 'inverter_hijack'
    option name 'Inverter Local Bridge'
    option src 'lan'
    option proto 'tcp'
    option src_ip '192.168.0.111'     # Wi-Fi Dongle Static IP
    option src_dport '18899'          # Cloud Control Port
    option dest_ip '192.168.0.105'    # Local Bridge Server IP
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

#### Option B: Gateway Interception via Linux / Proxmox LXC (`dnsmasq` + `iptables`)

If your main ISP router does not support custom DNAT rules, configure your Linux server / LXC (`192.168.0.105`) as the DHCP server and assign **itself** as the default gateway specifically for the inverter's MAC address. This works completely offline even if the main router loses power.

1. **Install packages on the Bridge Server:**

```
apt update && apt install dnsmasq iptables-persistent -y

```

2. **Configure `/etc/dnsmasq.conf`** (disable your router's built-in DHCP first):

```
interface=eth0
bind-interfaces
port=0  # Disable DNS if AdGuard/Pi-hole runs on the same host

# Standard LAN DHCP Range & Gateway (.1)
dhcp-range=192.168.0.100,192.168.0.240,12h
dhcp-option=6,192.168.0.1
dhcp-option=3,192.168.0.1

# Assign static IP to the Inverter Dongle and tag it
dhcp-host=AA:BB:CC:DD:EE:FF,192.168.0.111,Solar-Inverter,set:solar_inverter

# Push the Bridge Server (.105) as the Default Gateway ONLY for the inverter
dhcp-option=tag:solar_inverter,3,192.168.0.105

```

3. **Apply `iptables` Port Redirection & WAN Block:**

```
# Enable IPv4 forwarding
sysctl -w net.ipv4.ip_forward=1

# Redirect outbound Cloud Control (18899) and Logging (38899) to local port 18899
iptables -t nat -A PREROUTING -s 192.168.0.111 -p tcp --dport 18899 -j REDIRECT --to-port 18899
iptables -t nat -A PREROUTING -s 192.168.0.111 -p tcp --dport 38899 -j REDIRECT --to-port 18899

# Drop any other WAN packets from the dongle
iptables -A FORWARD -s 192.168.0.111 -j DROP

# Persist across reboots
netfilter-persistent save

```

*(Tip: Power-cycle the Wi-Fi dongle or restart your router's Wi-Fi AP after applying DHCP/NAT rules so the dongle immediately establishes a new TCP session to the local bridge).*

### 2. Easun / Voltronic Axpert PI30 (Direct USB-B Cable Connection)

For Voltronic/Axpert-based inverters (such as the **Easun ISolar SML II 5.5kW**), the bridge communicates locally at `2400 baud` using the **PI30 (`QPIGS` / `QPIRI` / `QMOD` / `QPIWS` / `QFLAG`)** protocol over a standard **USB-B ("printer") cable** plugged directly into the inverter's USB port and your Linux host.

1. **Create a persistent `udev` symlink** so the USB-serial chip inside the inverter always maps to `/dev/easun-rs485` regardless of USB port re-enumeration:

```
# Check your USB adapter's idVendor and idProduct (commonly 1a86:7523 for CH340 or 067b:2303 for PL2303)
lsusb

```

Create `/etc/udev/rules.d/99-solar-serial.rules`:

```
SUBSYSTEM=="tty", ATTRS{idVendor}=="1a86", ATTRS{idProduct}=="7523", SYMLINK+="easun-rs485", MODE="0666"

```

Reload `udev`:

```
udevadm control --reload-rules && udevadm trigger

```

*(If running inside a Proxmox LXC container, pass the `/dev/easun-rs485` or `/dev/ttyUSB*` device node through to the container).*

### 3. Operating the Island Inverter as a Zero-Idle Solar MPPT (Power Button OFF + Dry Relay Control)

In a dual-inverter setup sharing a 48V battery bank, the secondary (`island`) inverter does not need to keep its DC-AC inverter stage running 24/7:

* **Pure MPPT Mode (Power Switch OFF):** With the physical rocker switch on the Easun turned **OFF**, it acts as a standalone solar MPPT charger with **zero night-time battery self-consumption**. When PV voltage rises at sunrise, the unit automatically wakes up, charges the shared 48V battery bank (while reporting full telemetry over USB to the bridge), and goes completely back to sleep at sunset. In this state, the Web LCD automatically hides the Island AC output branch and shows only the PV $\rightarrow$ HVDC $\rightarrow$ 48V DC charging path.
* **Avoiding Split-State Battery Drain During Grid Bypass:** If the Island inverter's AC output is left ON while the Main inverter switches to Grid Bypass (due to low battery SOC), the Island inverter—having no grid input—would continue draining the depleted 48V battery bank to power its AC loads.
* **Dry-Contact Relay Automation:** To simplify operation and coordinate both units seamlessly, wire a smart dry-contact relay across the Easun's physical power toggle switch. Home Assistant can then automatically turn the Easun's AC output OFF whenever the Main inverter enters Grid Bypass (or when extra AC capacity isn't needed), dropping the Easun into zero-idle MPPT-only mode.

## 🛠️ Software Installation

### 1. Install Python Dependencies

```
apt update && apt install python3 python3-paho-mqtt python3-serial -y

```

*(Note: Requires `paho-mqtt >= 2.0.0` using `CallbackAPIVersion.VERSION2`. If your distro ships an older apt package, install via `pip3 install paho-mqtt pyserial --break-system-packages`).*

### 2. Deploy the Bridge Script & Systemd Service

Save the script to `/root/solar_mqtt_bridge_v43.py` and create `/etc/systemd/system/inverter-bridge.service`:

```
[Unit]
Description=Unified Solar MQTT Bridge & Web UI
After=network.target

[Service]
ExecStart=/usr/bin/python3 -u /root/solar_mqtt_bridge_v43.py
WorkingDirectory=/root
StandardOutput=inherit
StandardError=inherit
Restart=always
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target

```

Enable and start the service:

```
systemctl daemon-reload
systemctl enable --now inverter-bridge

```

## ⚙️ Configuration & Web UI (`:8080`)

On first start, the script generates `/root/bridge_config.json` and starts the Web UI at:
**`http://<bridge-ip>:8080`** (or **`http://<bridge-ip>:8080/?kiosk=1`** for fullscreen Kiosk mode).

You can edit `/root/bridge_config.json` directly from the Web UI form at the bottom of the page (saving automatically reconnects MQTT and hot-reloads device parameters):

```
{
  "mqtt_host": "192.168.0.100",
  "mqtt_port": 1883,
  "mqtt_user": "mqtt_user",
  "mqtt_pass": "********",
  "web_port": 8080,
  "max_battery_charge_amps": 120.0,
  "max_battery_discharge_amps": 120.0,
  "indicators": {
    "show_grid_bar": true,
    "show_solar_bar": true,
    "show_battery_bar": true,
    "show_load_bar": true
  },
  "devices": [
    {
      "id": "anenji_1",
      "name": "Anenji 6200W (Sumry Modbus RTU)",
      "short_name": "Anenji",
      "role": "main",
      "type": "anenji_tcp",
      "listen_port": 18899,
      "rated_va": 6200,
      "rated_watt": 6200,
      "installed_pv_watt": 5200,
      "max_grid_amps": 32.0,
      "min_mppt_volt": 60.0,
      "max_pv_volt": 500.0,
      "max_bus_volt": 495.0,
      "poll_interval": 1.5,
      "energy_file": "/root/inverter_energy.json"
    },
    {
      "id": "easun_1",
      "name": "Easun Inverter (Voltronic PI30)",
      "short_name": "Easun",
      "role": "island",
      "type": "easun_serial",
      "serial_port": "/dev/easun-rs485",
      "baud_rate": 2400,
      "rated_va": 5500,
      "rated_watt": 5500,
      "installed_pv_watt": 5500,
      "min_mppt_volt": 120.0,
      "max_pv_volt": 450.0,
      "max_bus_volt": 460.0,
      "poll_interval": 1.5,
      "energy_file": "/root/easun_energy.json"
    }
  ]
}

```

## 🏠 Home Assistant Integration (MQTT Auto-Discovery)

**No manual `configuration.yaml`, `shell_command`, `netcat`, or sync automations are needed.**

As soon as the bridge connects to your MQTT broker, both inverters appear under **Settings -> Devices & Services -> MQTT**:

* **MQTT Topics:**

  * Availability: `solar/<device_id>/status` (`online` / `offline`)

  * Telemetry JSON State: `solar/<device_id>/state`

  * Command Topic: `solar/<device_id>/set/<property>`

## 📊 Protocol & Register Reference

### 1. Anenji / Sumry (Modbus RTU Holding Registers)

| Register | Parameter | Unit / Mapping | State Key | R/W | 
| ----- | ----- | ----- | ----- | ----- | 
| **100–101** | Hardware Fault Bitmask | 32-bit (`F01`–`F23`, polled when in Fault mode or every 4th cycle) | `fault_code`, `hw_fault_msg` | R | 
| **201** | Operating Mode | `0`: Power On, `1`: Standby, `2`: Line, `3`: Off-Grid, `4`: Bypass, `5`: Charging, `6`: Fault | `device_status_msg` | R | 
| **202** | Grid Voltage | `0.1 V` | `grid_volt` | R | 
| **203** | Grid Frequency | `0.01 Hz` | `grid_freq` | R | 
| **204** | Grid Input Power | `W` (Signed) | `grid_power_watt` | R | 
| **209** | AC Grid Charging Power | `W` (Signed) | `ac_charging_watt` | R | 
| **210** *(fb 205)* | AC Output Voltage | `0.1 V` | `ac_out_volt` | R | 
| **211** | AC Output Current | `0.1 A` | `ac_out_amp` | R | 
| **212** *(fb 207)* | AC Output Frequency | `0.01 Hz` | `ac_out_freq` | R | 
| **213** | Active Load Power | `W` (Signed) | `ac_load_real_watt` | R | 
| **214** | Apparent Load Power | `VA` | `ac_load_va` | R | 
| **215** | Battery Voltage | `0.1 V` | `batt_volt` | R | 
| **218** | HVDC Bus Voltage | `0.1 V` | `bus_volt` | R | 
| **219** | PV Input Voltage | `0.1 V` | `pv_input_volt` | R | 
| **220** | PV Input Current | `0.1 A` (falls back to `p_pv / v_pv` if `0`) | `pv_current` | R | 
| **223** | PV Input Power | `W` | `pv_input_watt` | R | 
| **224** | PV Charging Power | `W` (Signed) | `pv_charging_watt` | R | 
| **225** | Inverter Load % | `%` (falls back to `max(VA, W) / rated_va`) | `ac_load_pct` | R | 
| **226** | DC-DC Temperature | `°C` (Signed) | `temp_dc` | R | 
| **227** | Inverter Temperature | `°C` (Signed) | `temp_inv` | R | 
| **229** | Battery SOC | `%` | `batt_soc` | R | 
| **232** | Net Battery Current | `0.1 A` (Signed: `+` Charging, `-` Discharging) | `batt_current` | R | 
| **301** | Output Source Priority | `0`: UTI, `1`: SOL, `2`: SBU, `3`: SUB, `4`: SUF | `output_mode_str` | R/W | 
| **302** | AC Input Range | `0`: APL, `1`: UPS, `2`: GEN | `ac_input_range_str` | R/W | 
| **303** | Buzzer Mode | `0`: Mute (nd1), `1`: nd2, `2`: nd3, `3`: Fault Only (nd4) | `buzzer_mode_str` | R/W | 
| **305** | LCD Backlight | `0`: OFF, `1`: ON | `backlight_status` | R/W | 
| **306** | Return to Default Screen | `0`: OFF, `1`: ON | `return_to_default` | R/W | 
| **320** | Rated Output Voltage | `2200`: 220V, `2300`: 230V, `2400`: 240V | `rated_out_volt_str` | R/W | 
| **322** | Battery Type | `0`: AGN, `1`: FLD, `2`: USR, `4`: LI2, `6`: LI4, `8`: LIb | `battery_type_str` | R/W | 
| **324** | Bulk Charge Voltage | `0.1 V` (`48.0`–`58.4V`) | `bulk_charge_volt` | R/W | 
| **325** | Float Charge Voltage | `0.1 V` (`48.0`–`58.4V`) | `float_charge_volt` | R/W | 
| **326** | Back to Battery Voltage | `0.1 V` (`0.0`–`58.0V`) | `back_to_batt_volt` | R/W | 
| **327** | Back to Grid Voltage | `0.1 V` (`0.0`–`58.0V`) | `back_to_grid_volt` | R/W | 
| **329** | Low DC Cut-off Voltage | `0.1 V` (`40.0`–`54.0V`) | `low_dc_cutoff_volt` | R/W | 
| **331** | Charger Source Priority | `1`: CSO, `2`: SNU, `3`: OSO *(also drives `grid_charging` switch)* | `charger_priority_str` | R/W | 
| **332** | Max Total Charge Amps | `0.1 A` (`10`–`120A`) | `max_total_amps` | R/W | 
| **333** | Max AC Charge Amps | `0.1 A` (`2`–`80A`) | `max_ac_amps` | R/W | 
| **341–343** | SOC Thresholds (Prg 43–45) | `%` (`soc_back_to_grid`, `soc_back_to_batt`, `soc_cutoff`) | `soc_*` | R/W | 

### 2. Easun / Voltronic (Axpert PI30 Commands)

| Command | Function | Parsed / Controlled Entities | 
| ----- | ----- | ----- | 
| **`QPIGS`** | Live Telemetry (every 1.5s) | `grid_volt`, `grid_freq`, `ac_out_volt`, `ac_out_freq`, `ac_load_va`, `ac_load_real_watt`, `ac_load_pct`, `bus_volt`, `batt_volt`, `batt_current` (with low-current solar compensation), `batt_soc`, `temp_inv`, `pv_current`, `pv_input_volt`, `pv_input_watt`, `charging_status` | 
| **`QPIRI`** | Rated Settings (every 60s) | `rated_out_volt_str`, `hw_rated_va`, `hw_rated_watt`, `back_to_grid_volt`, `low_dc_cutoff_volt`, `bulk_charge_volt`, `float_charge_volt`, `battery_type_str`, `max_ac_amps`, `max_total_amps`, `ac_input_range_str`, `output_mode_str`, `charger_priority_str`, `back_to_batt_volt` | 
| **`QFLAG`** | Feature Flags (every 60s) | `buzzer_alarm` (`a`), `overload_bypass` (`b`), `return_to_default` (`k`), `backlight_status` (`x`) | 
| **`QMOD`** | Operating Mode | `P`: Power On, `S`: Standby, `L`: Line, `B`: Battery, `F`: Fault, `H`: Power Saving, `C`: Charging, `Y`: Bypass | 
| **`QPIWS`** | Hardware Fault Bits | 32-bit fault string (`Bus Over Volt`, `Overload`, `Over Temp`, `Fan Locked`, etc.) | 
| **Write Cmds** | Parameter Writes | `POP00..02` (Output Mode), `PCP00..03` (Charger Priority), `PGR00..01` (AC Range), `PBT00..02` (Battery Type), `V220..240` (Output Volt), `PCVV` (Bulk V), `PBFT` (Float V), `PSDV` (Cutoff V), `PBCV` (Back to Grid V), `PBDV` (Back to Batt V), `MCHGC` / `MNCHGC0` (Max Total Amps), `MUCHGC` (Max AC Amps), `PE`/`PD` + `a/b/k/x` (Switches) | 

## ⚠️ Disclaimer & Electrical Safety

* **🚨 Multi-Inverter AC Wiring ("Parallel" Warning):**

  * **NEVER connect AC Outputs in parallel** unless both inverters are identical models fitted with dedicated parallel phase-sync communication boards/cables. Connecting unsynchronized AC outputs together will cause an immediate out-of-phase short circuit and destroy the output IGBTs on both units. In a mixed setup (like Anenji + Easun), each inverter must feed **completely separate AC load circuits** (hence the **Main + Island** architecture in this bridge) while sharing only the **48V DC battery bus** and separate PV strings.

  * **Sharing AC Grid Input across 48V-linked Inverters:** Depending on internal neutral-ground bonding and non-isolated topologies, connecting AC Grid Input to more than one inverter sharing the same 48V battery bank may or may not work. On the tested Anenji + Easun setup, connecting AC Grid Input to the secondary inverter triggers an immediate hardware fault due to internal current loops—so the secondary unit is run strictly with its AC Grid Input disconnected (`island` role).

* **EEPROM Lifespan:** Changing inverter parameters writes to non-volatile memory inside the inverter DSP. While this bridge enforces rate-limiting and blocks duplicate writes, avoid running high-frequency Home Assistant loops that continuously alter register values every few seconds.

* **Battery Limits:** Always verify your LiFePO4 BMS charging voltage and current ratings before adjusting `bulk_charge_volt`, `float_charge_volt`, or `max_total_amps`.
