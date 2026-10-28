import os
import pytest
from fastapi.testclient import TestClient
from app.config import Settings
from app.main import create_app

@pytest.mark.skipif(not os.getenv("SERVICE_INTEGRATION"), reason="requires PostgreSQL and Redis")
def test_real_services_ready():
    with TestClient(create_app(Settings())) as client:
        result=client.get("/health/ready")
        assert result.status_code == 200
        assert result.json() == {"database": True, "redis": True}
