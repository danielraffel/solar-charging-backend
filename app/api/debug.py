"""Debug endpoints for remote introspection."""

import logging
import os
import subprocess
from datetime import datetime, timedelta
from typing import Optional
from pathlib import Path

from fastapi import APIRouter, Query
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/debug", tags=["debug"])


class LogsResponse(BaseModel):
    """Response for logs endpoint."""
    lines: list[str]
    total_lines: int
    container: str
    timestamp: str


class ConfigResponse(BaseModel):
    """Response for config endpoint."""
    mqtt_host: str
    mqtt_port: int
    mqtt_connected: bool
    charging_safety_cutoff_hours: int
    charging_soc_check_interval: int
    apns_enabled: bool
    apns_sandbox: bool
    evcc_enabled: bool
    evcc_url: Optional[str]
    server_port: int
    log_level: str


class StateResponse(BaseModel):
    """Response for application state."""
    uptime_seconds: float
    mqtt_connected: bool
    mqtt_current_soc: Optional[int]
    mqtt_battery_power: Optional[float]
    scheduler_running: bool
    scheduler_is_charging: bool
    scheduler_charge_started_at: Optional[str]
    schedule_enabled: bool
    schedule_target_soc: Optional[int]
    schedule_start_time: Optional[str]
    schedule_mode: Optional[str]
    schedule_next_run: Optional[str]
    apns_enabled: bool
    apns_connected: bool
    evcc_monitor_running: bool


class MQTTDebugResponse(BaseModel):
    """Response for MQTT debug info."""
    connected: bool
    broker: str
    port: int
    client_id: Optional[str]
    subscribed_topics: list[str]
    last_soc_update: Optional[str]
    current_soc: Optional[int]
    battery_power: Optional[float]
    recent_publishes: list[dict]


@router.get("/logs", response_model=LogsResponse)
async def get_logs(
    lines: int = Query(default=100, ge=10, le=500, description="Number of log lines to return"),
    filter: Optional[str] = Query(default=None, description="Filter logs containing this string")
):
    """Get recent container logs."""
    try:
        # Get logs from docker
        cmd = ["docker", "logs", "--tail", str(lines * 2), "solar-charging-backend"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)

        all_lines = (result.stdout + result.stderr).strip().split("\n")

        # Filter if requested
        if filter:
            all_lines = [l for l in all_lines if filter.lower() in l.lower()]

        # Take last N lines
        output_lines = all_lines[-lines:] if len(all_lines) > lines else all_lines

        return LogsResponse(
            lines=output_lines,
            total_lines=len(output_lines),
            container="solar-charging-backend",
            timestamp=datetime.now().isoformat()
        )
    except Exception as e:
        logger.error(f"Failed to get logs: {e}")
        return LogsResponse(
            lines=[f"Error getting logs: {str(e)}"],
            total_lines=1,
            container="solar-charging-backend",
            timestamp=datetime.now().isoformat()
        )


@router.get("/config", response_model=ConfigResponse)
async def get_config():
    """Get current configuration (sanitized - no secrets)."""
    from ..main import app_state

    config = app_state.config
    return ConfigResponse(
        mqtt_host=config.mqtt.host,
        mqtt_port=config.mqtt.port,
        mqtt_connected=app_state.mqtt.connected if app_state.mqtt else False,
        charging_safety_cutoff_hours=config.charging.safety_cutoff_hours,
        charging_soc_check_interval=config.charging.soc_check_interval,
        apns_enabled=config.apns.enabled,
        apns_sandbox=config.apns.use_sandbox,
        evcc_enabled=config.evcc.enabled,
        evcc_url=config.evcc.url if config.evcc.enabled else None,
        server_port=config.server.port,
        log_level=config.logging.level
    )


@router.get("/state", response_model=StateResponse)
async def get_state():
    """Get current application state."""
    from ..main import app_state
    import time

    # Calculate uptime (use a module-level start time)
    uptime = 0  # Will be set properly when we track start time

    # Get schedule info
    schedule = app_state.scheduler.current_schedule if app_state.scheduler else None

    return StateResponse(
        uptime_seconds=uptime,
        mqtt_connected=app_state.mqtt.connected if app_state.mqtt else False,
        mqtt_current_soc=app_state.mqtt.current_soc if app_state.mqtt else None,
        mqtt_battery_power=app_state.mqtt.battery_power if app_state.mqtt else None,
        scheduler_running=app_state.scheduler is not None,
        scheduler_is_charging=app_state.scheduler.is_charging if app_state.scheduler else False,
        scheduler_charge_started_at=app_state.scheduler.charge_started_at.isoformat() if (app_state.scheduler and app_state.scheduler.charge_started_at) else None,
        schedule_enabled=schedule.enabled if schedule else False,
        schedule_target_soc=schedule.target_soc if schedule else None,
        schedule_start_time=schedule.start_time if schedule else None,
        schedule_mode=schedule.mode if schedule else None,
        schedule_next_run=schedule.next_run.isoformat() if (schedule and schedule.next_run) else None,
        apns_enabled=app_state.apns.is_enabled if app_state.apns else False,
        apns_connected=app_state.apns._client is not None if (app_state.apns and hasattr(app_state.apns, "_client")) else False,
        evcc_monitor_running=app_state.evcc_monitor is not None
    )


@router.get("/mqtt", response_model=MQTTDebugResponse)
async def get_mqtt_debug():
    """Get MQTT connection debug info."""
    from ..main import app_state

    mqtt = app_state.mqtt
    if not mqtt:
        return MQTTDebugResponse(
            connected=False,
            broker="",
            port=0,
            client_id=None,
            subscribed_topics=[],
            last_soc_update=None,
            current_soc=None,
            battery_power=None,
            recent_publishes=[]
        )

    # Get recent publishes if tracked
    recent_publishes = getattr(mqtt, "recent_publishes", [])

    return MQTTDebugResponse(
        connected=mqtt.connected,
        broker=app_state.config.mqtt.host,
        port=app_state.config.mqtt.port,
        client_id=mqtt.client._client_id.decode() if (mqtt.client and mqtt.client._client_id) else None,
        subscribed_topics=list(getattr(mqtt, "subscribed_topics", [])),
        last_soc_update=getattr(mqtt, "last_soc_update", None),
        current_soc=mqtt.current_soc,
        battery_power=mqtt.battery_power,
        recent_publishes=recent_publishes[-10:] if recent_publishes else []
    )


@router.get("/files")
async def list_files(path: str = Query(default="app", description="Relative path to list")):
    """List files in the application directory."""
    base_path = Path("/app")
    target_path = base_path / path

    # Security: ensure we stay within /app
    try:
        target_path = target_path.resolve()
        if not str(target_path).startswith("/app"):
            return {"error": "Access denied - path outside /app"}
    except Exception:
        return {"error": "Invalid path"}

    if not target_path.exists():
        return {"error": f"Path not found: {path}"}

    if target_path.is_file():
        # Return file info
        stat = target_path.stat()
        return {
            "type": "file",
            "path": str(target_path.relative_to(base_path)),
            "size": stat.st_size,
            "modified": datetime.fromtimestamp(stat.st_mtime).isoformat()
        }

    # List directory
    files = []
    for item in sorted(target_path.iterdir()):
        stat = item.stat()
        files.append({
            "name": item.name,
            "type": "dir" if item.is_dir() else "file",
            "size": stat.st_size if item.is_file() else None,
            "modified": datetime.fromtimestamp(stat.st_mtime).isoformat()
        })

    return {
        "type": "directory",
        "path": str(target_path.relative_to(base_path)),
        "files": files
    }


@router.get("/file")
async def read_file(path: str = Query(..., description="Relative path to file")):
    """Read a file from the application directory."""
    base_path = Path("/app")
    target_path = base_path / path

    # Security: ensure we stay within /app
    try:
        target_path = target_path.resolve()
        if not str(target_path).startswith("/app"):
            return {"error": "Access denied - path outside /app"}
    except Exception:
        return {"error": "Invalid path"}

    if not target_path.exists():
        return {"error": f"File not found: {path}"}

    if not target_path.is_file():
        return {"error": f"Not a file: {path}"}

    # Limit file size
    if target_path.stat().st_size > 100000:  # 100KB max
        return {"error": "File too large (max 100KB)"}

    try:
        content = target_path.read_text()
        return {
            "path": str(target_path.relative_to(base_path)),
            "content": content,
            "size": len(content)
        }
    except Exception as e:
        return {"error": f"Failed to read file: {str(e)}"}


@router.post("/restart-hint")
async def restart_hint():
    """Returns instructions for restarting the backend (does not actually restart)."""
    return {
        "message": "To restart the backend, run these commands:",
        "commands": [
            "ssh teslaproxy 'cd /opt/solar-charging-backend && docker compose restart'",
            "ssh teslaproxy 'cd /opt/solar-charging-backend && docker compose down && docker compose build --no-cache && docker compose up -d'"
        ],
        "note": "This endpoint does not restart automatically for safety"
    }
