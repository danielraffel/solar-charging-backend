# Solar Charging Backend

A lightweight, always-on backend service for managing solar battery charging schedules. Solves iOS background task reliability issues by running charging logic on a dedicated server.

## 🎯 What Does This Solve?

iOS background tasks (`BGTaskScheduler`) are unreliable for time-sensitive operations like battery charging:
- iOS throttles/kills background tasks unpredictably
- No guarantees your scheduled charge will run at 2 AM
- Depends on user opening the app as a fallback

**This backend service:**
- ✅ Runs continuously (no iOS limitations)
- ✅ Guaranteed execution at scheduled times
- ✅ Monitors battery SOC in real-time via MQTT
- ✅ Automatically stops charging when target reached
- ✅ Easy to deploy with Docker or standalone

## 🚀 Quick Start (Docker - Recommended)

### Prerequisites
- Docker and Docker Compose installed
- MQTT broker accessible from where you'll run this server
- Your solar dongle's MQTT details

### 3-Step Setup

```bash
# 1. Clone and configure
git clone <repo-url>
cd solar-charging-backend
cp config.example.yaml config.yaml
# Edit config.yaml with your MQTT broker details

# 2. Start the service
docker-compose up -d

# 3. Verify it's running
curl http://localhost:8088/api/health
```

Expected response:
```json
{
  "status": "ok",
  "mqtt_connected": true,
  "version": "1.0.0",
  "uptime_seconds": 12.5
}
```

Done! The service is now running and ready to accept commands from your iOS app.

## 📦 Alternative: Run Without Docker

### Prerequisites
- Python 3.11 or higher
- pip (Python package manager)

### Setup

```bash
# 1. Clone repository
git clone <repo-url>
cd solar-charging-backend

# 2. Create virtual environment
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure
cp config.example.yaml config.yaml
# Edit config.yaml with your settings

# 5. Run
python main.py
```

The service will start on `http://0.0.0.0:8088`.

## ⚙️ Configuration

Edit `config.yaml`:

```yaml
mqtt:
  host: "192.168.1.100"           # Your MQTT broker IP
  port: 1883
  username: "solar"
  password: "your-password"
  dongle_prefix: "dongle-XX:XX:XX:XX:XX:XX"  # Replace with your dongle MAC

server:
  host: "0.0.0.0"
  port: 8088                          # HTTP server port (default changed from 8080)

charging:
  safety_cutoff_hours: 8          # Max charging duration (hours)
  soc_check_interval: 30          # Check SOC every 30 seconds

logging:
  level: "INFO"                   # DEBUG | INFO | WARNING | ERROR

# Optional: Push notifications (see APNs Setup section below)
apns:
  enabled: false
```

### Finding Your Dongle Prefix

Your dongle prefix is the MQTT topic prefix, usually `dongle-XX:XX:XX:XX:XX:XX` where the X's are your dongle's MAC address.

Check your existing iOS app settings or MQTT broker to find it.

### Environment Variable Overrides

You can override port settings using environment variables (useful for deployment):

**Direct execution:**
```bash
SERVER_PORT=9000 python main.py
```

**Docker Compose (.env file):**
Create a `.env` file:
```bash
HOST_PORT=9000
CONTAINER_PORT=9000
```

Or set inline:
```bash
HOST_PORT=9000 docker-compose up -d
```

**Priority:** `SERVER_PORT` environment variable > `config.yaml` > default (8088)

## 📡 API Endpoints

### Health Check

```bash
GET /api/health
```

Response:
```json
{
  "status": "ok",
  "mqtt_connected": true,
  "version": "1.0.0",
  "uptime_seconds": 123.45
}
```

### Create/Update Schedule

```bash
POST /api/charge/schedule
Content-Type: application/json

{
  "target_soc": 85,
  "start_time": "02:30",
  "mode": "recurring",
  "enabled": true
}
```

**Parameters:**
- `target_soc` (int, 10-100): Target battery percentage
- `start_time` (string, HH:MM): When to start charging (24-hour format)
- `mode` (string): `"once"` or `"recurring"`
- `enabled` (bool): Whether schedule is active

Response:
```json
{
  "target_soc": 85,
  "start_time": "02:30",
  "mode": "recurring",
  "enabled": true,
  "next_run": "2025-12-27T02:30:00",
  "is_charging": false,
  "current_soc": 45
}
```

### Get Current Schedule

```bash
GET /api/charge/schedule
```

Returns current schedule if one exists, or `null`.

### Cancel Schedule

```bash
DELETE /api/charge/schedule
```

Cancels the active schedule and stops any ongoing charging.

### Get Charging Status

```bash
GET /api/charge/status
```

Response:
```json
{
  "is_charging": false,
  "current_soc": 45,
  "target_soc": 85,
  "charging_power": 0,
  "scheduled_next": "2025-12-27T02:30:00",
  "last_updated": "2025-12-26T10:30:00"
}
```

## 🔄 How It Works

### Scheduling Flow

1. **iOS app sends schedule** → `POST /api/charge/schedule`
2. **Backend calculates next run**
   - If start time is 2:30 AM and it's 10 PM → schedules for 2:30 AM tonight
   - If start time is 2:30 AM and it's 3 AM → schedules for 2:30 AM tomorrow
3. **Backend saves schedule** → Persists to `data/schedule.json`
4. **APScheduler manages job** → Guaranteed execution at scheduled time

### Charging Execution

When scheduled time arrives:

1. **Check current SOC** via MQTT
2. **If SOC < target:**
   - Publish `ACCharge=1` (enable charging)
   - Publish time window (start time + 8 hours safety cutoff)
   - Publish `ACChgSOCLimit` (target percentage)
3. **Monitor SOC every 30 seconds**
4. **Stop when:**
   - SOC ≥ target, OR
   - 8 hours elapsed (safety cutoff)
5. **If mode = recurring:** Reschedule for tomorrow

### Safety Features

- **8-hour cutoff**: Prevents infinite charging if SOC sensor fails
- **Automatic reconnection**: MQTT client auto-reconnects if connection drops
- **State persistence**: Schedule survives server restarts
- **Dual-stop mechanism**: Both server AND dongle enforce SOC limit

## 🏠 Running on Mac Mini

Perfect for a Mac mini that's always on:

```bash
# Set up to run on boot (macOS)
# Create ~/Library/LaunchAgents/com.solar.charging-backend.plist

<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.solar.charging-backend</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/local/bin/docker-compose</string>
        <string>-f</string>
        <string>/Users/youruser/solar-charging-backend/docker-compose.yml</string>
        <string>up</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
</dict>
</plist>
```

Then:
```bash
launchctl load ~/Library/LaunchAgents/com.solar.charging-backend.plist
```

## 🐛 Troubleshooting

### "Failed to connect to MQTT"

1. Check MQTT broker is running: `ping <mqtt-host>`
2. Verify credentials in `config.yaml`
3. Check firewall rules (port 1883)
4. Test with MQTT client: `mosquitto_sub -h <host> -u <user> -P <pass> -t "#"`

### "Schedule not executing"

1. Check logs: `docker-compose logs -f solar-backend`
2. Verify system time is correct
3. Check schedule is enabled: `GET /api/charge/schedule`

### "SOC not updating"

1. Verify dongle prefix in config matches actual MQTT topics
2. Check MQTT messages: `mosquitto_sub -h <host> -u <user> -P <pass> -t "dongle-+/inputbank1"`
3. Enable debug logging: Set `logging.level: DEBUG` in config

### View Logs

```bash
# Docker
docker-compose logs -f

# Standalone
# Logs go to stdout (terminal)
```

## 🔐 Security Considerations

**Local Network Only (Default):**
- No authentication on API endpoints
- Relies on network-level security
- Only expose on trusted local network

**For Internet Exposure:**
- Add reverse proxy (nginx/Traefik) with HTTPS
- Implement API key authentication
- Use firewall rules to restrict access

## 📊 System Requirements

**Minimal:**
- 100MB RAM
- 100MB disk space
- Python 3.11+ OR Docker

**Tested On:**
- macOS (Mac mini)
- Ubuntu 22.04 LTS
- Raspberry Pi 4 (Debian)
- Docker Desktop

## 🛠️ Development

### Running Tests

```bash
# Install dev dependencies
pip install -r requirements-dev.txt  # (if exists)

# Run tests
pytest
```

### Code Structure

```
app/
├── api/          # FastAPI endpoints
│   ├── health.py # Health check
│   ├── charge.py # Charging endpoints
│   └── device.py # Device registration (push notifications)
├── mqtt/         # MQTT client
│   └── client.py # Connection & publishing
├── scheduler/    # Job scheduling
│   └── manager.py # APScheduler integration
├── notifications/ # Push notifications
│   └── apns.py   # Apple Push Notification service
├── models/       # Pydantic models
│   ├── config.py # Configuration
│   └── schedule.py # Schedule/response models
└── main.py       # FastAPI app setup
```

## 📱 Push Notifications (APNs) Setup

Push notifications allow the backend to notify the iOS app when charging is complete, even if the app is backgrounded or closed. This enables the Live Activity to end properly.

### Push Notifications Are Optional

**The backend works without push notifications.** If you don't configure APNs:
- Scheduled charging still works perfectly
- SOC monitoring and auto-stop still work
- Live Activities work when the app is in foreground
- Only limitation: Live Activities won't end when the app is fully backgrounded

This is fine for most users. Push notifications are an enhancement, not a requirement.

### For Open Source / Self-Hosted Users

If you're building this app from source for your own use:

1. **Same bundle ID as original app**: You cannot generate an APNs key for someone else's bundle ID. You'd need to obtain their key (with permission) or skip push notifications.

2. **Your own bundle ID**: If you change the bundle ID to your own (e.g., `com.yourname.SolarApp`), you can generate your own APNs key that will work with your build.

The APNs key authenticates the **backend** to send notifications to devices running apps with a **matching bundle ID**. This is an Apple security requirement.

### Prerequisites

1. **Apple Developer Account** ($99/year) with APNs enabled
2. **APNs Key (.p8 file)** from Apple Developer Portal
3. **Key ID** and **Team ID** from your Apple Developer account
4. **Bundle ID** that matches your iOS app build

### Step 1: Create APNs Key

1. Go to [Apple Developer Portal - Keys](https://developer.apple.com/account/resources/authkeys/list)
2. Click "+" to create a new key
3. Enter a name (e.g., "Solar App APNs Key")
4. Check "Apple Push Notifications service (APNs)"
5. Click "Continue", then "Register"
6. **Download the .p8 file** (you can only download once!)
7. Note the **Key ID** (10 characters, e.g., "K4QVR32WLW")

### Step 2: Find Your Team ID

1. Go to [Apple Developer Account](https://developer.apple.com/account)
2. Your Team ID is displayed in the membership section (10 characters, e.g., "95CX6P84C4")

### Step 3: Configure Backend

1. Copy the .p8 key file to your server (e.g., `/opt/solar-charging-backend/keys/`)
2. **IMPORTANT:** Never commit the .p8 file to version control!
3. Update `config.yaml`:

```yaml
apns:
  enabled: true
  key_path: "/opt/solar-charging-backend/keys/AuthKey_XXXXXXXX.p8"
  key_id: "XXXXXXXXXX"        # Your Key ID from Step 1
  team_id: "XXXXXXXXXX"       # Your Team ID from Step 2
  bundle_id: "com.generouscorp.Solar"
  use_sandbox: false          # false for TestFlight/Production
```

### Step 4: Install Dependency

```bash
pip install aioapns==3.2
# or update via: pip install -r requirements.txt
```

### Step 5: Restart Service

```bash
docker-compose restart
# or: systemctl restart solar-backend
```

### Verification

Check the logs for successful initialization:
```
INFO - APNs service initialized (sandbox=False)
INFO - APNs service connected to scheduler
```

### Security Note

The APNs key file (`.p8`) is sensitive and should never be committed to version control. The `.gitignore` already excludes `.p8` files, but always verify before committing.

## 📝 License

MIT License - See [LICENSE](LICENSE) file for details.

---

**Next Steps:**
1. ✅ Get backend running
2. Configure iOS app to use backend (see iOS integration guide)
3. Test scheduling from iOS app
4. Enjoy reliable charging! ⚡
