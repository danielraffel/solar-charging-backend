"""Main application entry point."""

import logging
import os
import sys
import json
from pathlib import Path
from typing import Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
import yaml

from .models import AppConfig, ScheduleData
from .mqtt import MQTTClient
from .scheduler import ChargingScheduleManager
from .api import health_router, charge_router, device_router
from .notifications import APNsService
from .evcc import EVCCMonitorService


# Configure logging
def setup_logging(level: str = "INFO"):
    """Configure logging for the application."""
    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler(sys.stdout)
        ]
    )


logger = logging.getLogger(__name__)


# Application state
class AppState:
    """Global application state."""

    def __init__(self):
        self.config: Optional[AppConfig] = None
        self.mqtt: Optional[MQTTClient] = None
        self.scheduler: Optional[ChargingScheduleManager] = None
        self.apns: Optional[APNsService] = None
        self.evcc_monitor: Optional[EVCCMonitorService] = None
        self.data_dir = Path("data")
        self.schedule_file = self.data_dir / "schedule.json"

    def save_schedule(self, schedule: ScheduleData):
        """Save schedule to disk."""
        self.data_dir.mkdir(exist_ok=True)
        with open(self.schedule_file, "w") as f:
            json.dump(schedule.model_dump(mode="json"), f, indent=2, default=str)
        logger.debug(f"Schedule saved to {self.schedule_file}")

    def load_schedule(self) -> Optional[ScheduleData]:
        """Load schedule from disk."""
        if not self.schedule_file.exists():
            return None

        try:
            with open(self.schedule_file, "r") as f:
                data = json.load(f)
                return ScheduleData(**data)
        except Exception as e:
            logger.error(f"Failed to load schedule: {e}")
            return None

    def clear_schedule(self):
        """Clear saved schedule."""
        if self.schedule_file.exists():
            self.schedule_file.unlink()
            logger.debug("Schedule file deleted")


app_state = AppState()


# Lifecycle management
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    # Startup
    logger.info("Starting Solar Charging Backend...")

    # Load configuration
    config_path = Path("config.yaml")
    if not config_path.exists():
        logger.error("config.yaml not found! Please create it from config.example.yaml")
        sys.exit(1)

    with open(config_path) as f:
        config_data = yaml.safe_load(f)

    app_state.config = AppConfig(**config_data)

    # Override port from environment variable if set
    if server_port := os.getenv("SERVER_PORT"):
        try:
            app_state.config.server.port = int(server_port)
            logger.info(f"Port overridden by SERVER_PORT environment variable: {server_port}")
        except ValueError:
            logger.warning(f"Invalid SERVER_PORT value '{server_port}', using config file value")

    setup_logging(app_state.config.logging.level)

    logger.info(f"Loaded configuration from {config_path}")
    logger.info(f"Server will run on {app_state.config.server.host}:{app_state.config.server.port}")

    # Initialize MQTT client
    app_state.mqtt = MQTTClient(app_state.config.mqtt)
    if not app_state.mqtt.connect():
        logger.error("Failed to connect to MQTT broker")
        sys.exit(1)

    # Initialize scheduler
    app_state.scheduler = ChargingScheduleManager(
        app_state.mqtt,
        app_state.config.charging
    )
    app_state.scheduler.start()

    # Load and restore any saved schedule
    saved_schedule = app_state.load_schedule()
    if saved_schedule:
        logger.info(f"Restoring saved schedule: {saved_schedule}")
        app_state.scheduler.set_schedule(saved_schedule)

    # Initialize APNs service for push notifications
    app_state.apns = APNsService(app_state.config.apns)
    if app_state.config.apns.enabled:
        if await app_state.apns.initialize():
            # Pass APNs service to scheduler for charge complete notifications
            app_state.scheduler.set_apns_service(app_state.apns)
            logger.info("APNs service initialized and connected to scheduler")
        else:
            logger.warning("APNs service failed to initialize - push notifications disabled")
    else:
        logger.info("APNs service disabled in configuration")

    # Initialize EVCC monitor for EV charging notifications
    if app_state.config.evcc.enabled:
        app_state.evcc_monitor = EVCCMonitorService(
            evcc_url=app_state.config.evcc.url,
            loadpoint_id=app_state.config.evcc.loadpoint_id,
            poll_interval=app_state.config.evcc.poll_interval_seconds,
            apns_service=app_state.apns if app_state.apns and app_state.apns.is_enabled else None
        )
        await app_state.evcc_monitor.start()
        logger.info(f"EVCC monitor started: {app_state.config.evcc.url} (loadpoint {app_state.config.evcc.loadpoint_id})")
    else:
        logger.info("EVCC monitoring disabled in configuration")

    logger.info("Solar Charging Backend started successfully")

    yield

    # Shutdown
    logger.info("Shutting down Solar Charging Backend...")
    if app_state.evcc_monitor:
        await app_state.evcc_monitor.stop()
    app_state.scheduler.stop()
    app_state.mqtt.disconnect()
    logger.info("Shutdown complete")


# Create FastAPI app
app = FastAPI(
    title="Solar Charging Backend",
    description="Backend service for managing solar battery charging schedules",
    version="1.0.0",
    lifespan=lifespan
)

# Add CORS middleware (allow iOS app to connect)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, restrict to iOS app's origin
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routers
app.include_router(health_router, prefix="/api", tags=["health"])
app.include_router(charge_router, prefix="/api", tags=["charging"])
app.include_router(device_router, prefix="/api", tags=["device"])


@app.get("/")
async def root():
    """Root endpoint with status dashboard."""
    # Get current charging status
    is_charging = app_state.scheduler.is_charging if app_state.scheduler else False
    current_soc = app_state.mqtt.current_soc if app_state.mqtt else None

    # Get current schedule info
    schedule = app_state.scheduler.current_schedule if app_state.scheduler else None
    schedule_info = None
    if schedule:
        schedule_info = {
            "enabled": schedule.enabled,
            "target_soc": schedule.target_soc,
            "start_time": schedule.start_time,
            "mode": schedule.mode,
            "next_run": schedule.next_run.isoformat() if schedule.next_run else None,
        }

    return {
        "name": "Solar Charging Backend",
        "version": "1.0.0",
        "status": "running",
        "mqtt_connected": app_state.mqtt.connected if app_state.mqtt else False,
        "charging": {
            "is_charging": is_charging,
            "current_soc": current_soc,
            "started_at": app_state.scheduler.charge_started_at.isoformat() if (app_state.scheduler and app_state.scheduler.charge_started_at) else None,
        },
        "schedule": schedule_info,
        "docs": "/docs",
        "api_endpoints": {
            "health": "/api/health",
            "status": "/api/charge/status",
            "schedule": "/api/charge/schedule"
        }
    }


@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    """Simple HTML admin page for managing charging schedules."""
    return """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Solar Charging Admin</title>
    <style>
        * { box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            max-width: 600px;
            margin: 0 auto;
            padding: 20px;
            background: #f5f5f5;
            color: #333;
        }
        h1 { color: #2c3e50; margin-bottom: 5px; }
        .subtitle { color: #666; margin-bottom: 20px; }
        .card {
            background: white;
            border-radius: 12px;
            padding: 20px;
            margin-bottom: 20px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
        }
        .status-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 15px;
        }
        .status-badge {
            padding: 6px 12px;
            border-radius: 20px;
            font-size: 14px;
            font-weight: 500;
        }
        .status-active { background: #d4edda; color: #155724; }
        .status-scheduled { background: #cce5ff; color: #004085; }
        .status-inactive { background: #e2e3e5; color: #383d41; }
        .status-charging { background: #28a745; color: white; }
        .info-row {
            display: flex;
            justify-content: space-between;
            padding: 10px 0;
            border-bottom: 1px solid #eee;
        }
        .info-row:last-child { border-bottom: none; }
        .info-label { color: #666; }
        .info-value { font-weight: 500; }
        .form-group { margin-bottom: 15px; }
        label { display: block; margin-bottom: 5px; color: #666; font-size: 14px; }
        input, select {
            width: 100%;
            padding: 10px;
            border: 1px solid #ddd;
            border-radius: 8px;
            font-size: 16px;
        }
        input:focus, select:focus {
            outline: none;
            border-color: #007bff;
        }
        .btn {
            padding: 12px 24px;
            border: none;
            border-radius: 8px;
            font-size: 16px;
            cursor: pointer;
            width: 100%;
            margin-bottom: 10px;
        }
        .btn-primary { background: #007bff; color: white; }
        .btn-primary:hover { background: #0056b3; }
        .btn-danger { background: #dc3545; color: white; }
        .btn-danger:hover { background: #c82333; }
        .btn-secondary { background: #6c757d; color: white; }
        .btn-secondary:hover { background: #545b62; }
        .btn:disabled { opacity: 0.6; cursor: not-allowed; }
        .message {
            padding: 12px;
            border-radius: 8px;
            margin-bottom: 15px;
            display: none;
        }
        .message-success { background: #d4edda; color: #155724; }
        .message-error { background: #f8d7da; color: #721c24; }
        .mqtt-status {
            display: inline-block;
            width: 10px;
            height: 10px;
            border-radius: 50%;
            margin-right: 5px;
        }
        .mqtt-connected { background: #28a745; }
        .mqtt-disconnected { background: #dc3545; }
        .no-schedule { text-align: center; color: #666; padding: 20px; }
    </style>
</head>
<body>
    <h1>Solar Charging</h1>
    <p class="subtitle"><span id="mqtt-indicator" class="mqtt-status"></span><span id="mqtt-text">Checking...</span></p>

    <div id="message" class="message"></div>

    <!-- Current Status -->
    <div class="card">
        <div class="status-header">
            <h3 style="margin:0">Current Schedule</h3>
            <span id="status-badge" class="status-badge status-inactive">No Schedule</span>
        </div>
        <div id="schedule-info">
            <div class="no-schedule">No active schedule</div>
        </div>
    </div>

    <!-- Edit/Create Schedule -->
    <div class="card">
        <h3 style="margin-top:0">Manage Schedule</h3>
        <form id="schedule-form">
            <div class="form-group">
                <label for="target_soc">Target SOC (%)</label>
                <input type="number" id="target_soc" min="10" max="100" value="80" required>
            </div>
            <div class="form-group">
                <label for="start_time">Start Time</label>
                <input type="time" id="start_time" value="18:00" required>
            </div>
            <div class="form-group">
                <label for="mode">Mode</label>
                <select id="mode">
                    <option value="once">One Time</option>
                    <option value="recurring">Recurring (Daily)</option>
                </select>
            </div>
            <button type="submit" class="btn btn-primary" id="save-btn">Save Schedule</button>
        </form>
        <button class="btn btn-danger" id="delete-btn" style="display:none">Delete Schedule</button>
    </div>

    <!-- Quick Actions -->
    <div class="card">
        <h3 style="margin-top:0">Quick Actions</h3>
        <button class="btn btn-secondary" id="refresh-btn">Refresh Status</button>
    </div>

    <script>
        const API_BASE = '';

        function showMessage(text, isError = false) {
            const msg = document.getElementById('message');
            msg.textContent = text;
            msg.className = 'message ' + (isError ? 'message-error' : 'message-success');
            msg.style.display = 'block';
            setTimeout(() => msg.style.display = 'none', 4000);
        }

        async function fetchStatus() {
            try {
                const resp = await fetch(API_BASE + '/api/charge/schedule');
                const mqttResp = await fetch(API_BASE + '/');
                const rootData = await mqttResp.json();

                // Update MQTT status
                const mqttIndicator = document.getElementById('mqtt-indicator');
                const mqttText = document.getElementById('mqtt-text');
                if (rootData.mqtt_connected) {
                    mqttIndicator.className = 'mqtt-status mqtt-connected';
                    mqttText.textContent = 'MQTT Connected';
                } else {
                    mqttIndicator.className = 'mqtt-status mqtt-disconnected';
                    mqttText.textContent = 'MQTT Disconnected';
                }

                const badge = document.getElementById('status-badge');
                const info = document.getElementById('schedule-info');
                const deleteBtn = document.getElementById('delete-btn');

                if (!resp.ok || resp.status === 204) {
                    badge.textContent = 'No Schedule';
                    badge.className = 'status-badge status-inactive';
                    info.innerHTML = '<div class="no-schedule">No active schedule</div>';
                    deleteBtn.style.display = 'none';
                    return;
                }

                const data = await resp.json();
                if (!data) {
                    badge.textContent = 'No Schedule';
                    badge.className = 'status-badge status-inactive';
                    info.innerHTML = '<div class="no-schedule">No active schedule</div>';
                    deleteBtn.style.display = 'none';
                    return;
                }

                // Update badge
                if (data.is_charging) {
                    badge.textContent = 'Charging';
                    badge.className = 'status-badge status-charging';
                } else if (data.enabled) {
                    badge.textContent = data.mode === 'recurring' ? 'Recurring' : 'Scheduled';
                    badge.className = 'status-badge status-scheduled';
                } else {
                    badge.textContent = 'Disabled';
                    badge.className = 'status-badge status-inactive';
                }

                // Format next run
                let nextRun = 'Not scheduled';
                if (data.next_run) {
                    const d = new Date(data.next_run);
                    nextRun = d.toLocaleString();
                }

                // Update info
                info.innerHTML = `
                    <div class="info-row">
                        <span class="info-label">Target SOC</span>
                        <span class="info-value">${data.target_soc}%</span>
                    </div>
                    <div class="info-row">
                        <span class="info-label">Start Time</span>
                        <span class="info-value">${data.start_time}</span>
                    </div>
                    <div class="info-row">
                        <span class="info-label">Mode</span>
                        <span class="info-value">${data.mode === 'recurring' ? 'Recurring (Daily)' : 'One Time'}</span>
                    </div>
                    <div class="info-row">
                        <span class="info-label">Next Run</span>
                        <span class="info-value">${nextRun}</span>
                    </div>
                    <div class="info-row">
                        <span class="info-label">Current SOC</span>
                        <span class="info-value">${data.current_soc !== null ? data.current_soc + '%' : 'Unknown'}</span>
                    </div>
                `;

                // Pre-fill form
                document.getElementById('target_soc').value = data.target_soc;
                document.getElementById('start_time').value = data.start_time;
                document.getElementById('mode').value = data.mode;

                deleteBtn.style.display = 'block';

            } catch (err) {
                console.error('Error fetching status:', err);
                showMessage('Failed to fetch status: ' + err.message, true);
            }
        }

        document.getElementById('schedule-form').addEventListener('submit', async (e) => {
            e.preventDefault();
            const btn = document.getElementById('save-btn');
            btn.disabled = true;
            btn.textContent = 'Saving...';

            try {
                const resp = await fetch(API_BASE + '/api/charge/schedule', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        target_soc: parseInt(document.getElementById('target_soc').value),
                        start_time: document.getElementById('start_time').value,
                        mode: document.getElementById('mode').value,
                        enabled: true
                    })
                });

                if (resp.ok) {
                    showMessage('Schedule saved successfully!');
                    fetchStatus();
                } else {
                    const err = await resp.json();
                    showMessage('Failed to save: ' + (err.detail || 'Unknown error'), true);
                }
            } catch (err) {
                showMessage('Error: ' + err.message, true);
            } finally {
                btn.disabled = false;
                btn.textContent = 'Save Schedule';
            }
        });

        document.getElementById('delete-btn').addEventListener('click', async () => {
            if (!confirm('Are you sure you want to delete this schedule?')) return;

            const btn = document.getElementById('delete-btn');
            btn.disabled = true;
            btn.textContent = 'Deleting...';

            try {
                const resp = await fetch(API_BASE + '/api/charge/schedule', {
                    method: 'DELETE'
                });

                if (resp.ok) {
                    showMessage('Schedule deleted!');
                    fetchStatus();
                } else {
                    showMessage('Failed to delete schedule', true);
                }
            } catch (err) {
                showMessage('Error: ' + err.message, true);
            } finally {
                btn.disabled = false;
                btn.textContent = 'Delete Schedule';
            }
        });

        document.getElementById('refresh-btn').addEventListener('click', () => {
            fetchStatus();
            showMessage('Status refreshed');
        });

        // Initial load
        fetchStatus();
        // Auto-refresh every 30 seconds
        setInterval(fetchStatus, 30000);
    </script>
</body>
</html>"""


if __name__ == "__main__":
    import uvicorn
    import os

    # Load config to get port (fallback entry point)
    config_path = Path("config.yaml")
    if config_path.exists():
        with open(config_path) as f:
            config_data = yaml.safe_load(f)
        config = AppConfig(**config_data)
        port = int(os.getenv("SERVER_PORT", config.server.port))
        host = os.getenv("SERVER_HOST", config.server.host)
    else:
        port = int(os.getenv("SERVER_PORT", 8088))
        host = os.getenv("SERVER_HOST", "0.0.0.0")
        print("WARNING: config.yaml not found, using defaults")

    uvicorn.run("app.main:app", host=host, port=port, log_level="info")
