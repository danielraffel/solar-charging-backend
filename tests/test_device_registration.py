"""Tests for device registration API endpoints."""

import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock, patch


# Mock the APNs service before importing the app
@pytest.fixture
def mock_apns():
    """Create a mock APNs service."""
    mock = MagicMock()
    mock.is_enabled = True
    mock.registered_device_count = 0
    mock.register_device.return_value = True
    mock.unregister_device.return_value = True
    return mock


@pytest.fixture
def test_client(mock_apns):
    """Create test client with mocked dependencies."""
    with patch.dict('sys.modules', {'aioapns': MagicMock()}):
        from app.main import app, app_state
        app_state.apns = mock_apns
        client = TestClient(app)
        yield client, mock_apns


class TestDeviceRegistration:
    """Tests for /api/device/register endpoint."""

    def test_register_device_success(self, test_client):
        """Test successful device token registration."""
        client, mock_apns = test_client

        response = client.post(
            "/api/device/register",
            json={"device_token": "abc123def456" * 5}  # 60 character hex token
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "registered" in data["message"].lower()

    def test_register_device_apns_disabled(self, test_client):
        """Test registration when APNs is disabled."""
        client, mock_apns = test_client
        mock_apns.is_enabled = False

        response = client.post(
            "/api/device/register",
            json={"device_token": "abc123def456" * 5}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "not enabled" in data["message"].lower()

    def test_register_device_invalid_token(self, test_client):
        """Test registration with invalid token format."""
        client, mock_apns = test_client
        mock_apns.register_device.return_value = False

        response = client.post(
            "/api/device/register",
            json={"device_token": "invalid-token-xyz"}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False

    def test_register_device_empty_token(self, test_client):
        """Test registration with empty token."""
        client, mock_apns = test_client
        mock_apns.register_device.return_value = False

        response = client.post(
            "/api/device/register",
            json={"device_token": ""}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False


class TestDeviceUnregistration:
    """Tests for /api/device/unregister endpoint."""

    def test_unregister_device_success(self, test_client):
        """Test successful device token unregistration."""
        client, mock_apns = test_client
        mock_apns.unregister_device.return_value = True

        response = client.request(
            "DELETE",
            "/api/device/unregister",
            json={"device_token": "abc123def456" * 5}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "unregistered" in data["message"].lower()

    def test_unregister_device_not_found(self, test_client):
        """Test unregistration of unknown token."""
        client, mock_apns = test_client
        mock_apns.unregister_device.return_value = False

        response = client.request(
            "DELETE",
            "/api/device/unregister",
            json={"device_token": "unknown_token_123"}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert "not found" in data["message"].lower()


class TestDeviceStatus:
    """Tests for /api/device/status endpoint."""

    def test_device_status_enabled(self, test_client):
        """Test status when APNs is enabled."""
        client, mock_apns = test_client
        mock_apns.is_enabled = True
        mock_apns.registered_device_count = 3

        response = client.get("/api/device/status")

        assert response.status_code == 200
        data = response.json()
        assert data["apns_enabled"] is True
        assert data["registered_devices"] == 3

    def test_device_status_disabled(self, test_client):
        """Test status when APNs is disabled."""
        client, mock_apns = test_client
        mock_apns.is_enabled = False
        mock_apns.registered_device_count = 0

        response = client.get("/api/device/status")

        assert response.status_code == 200
        data = response.json()
        assert data["apns_enabled"] is False
        assert data["registered_devices"] == 0

    def test_device_status_no_apns_service(self):
        """Test status when APNs service is not configured."""
        with patch.dict('sys.modules', {'aioapns': MagicMock()}):
            from app.main import app, app_state
            app_state.apns = None
            client = TestClient(app)

            response = client.get("/api/device/status")

            assert response.status_code == 200
            data = response.json()
            assert data["apns_enabled"] is False
            assert data["registered_devices"] == 0
