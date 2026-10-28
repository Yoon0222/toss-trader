from fastapi.testclient import TestClient
from app.main import create_app
from app.config import Settings

def test_dashboard_and_assets():
    settings=Settings(_env_file=None,database_url="sqlite+aiosqlite://",redis_url="redis://127.0.0.1:1/0")
    with TestClient(create_app(settings)) as client:
        response=client.get("/")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert 'lang="ko"' in response.text
        for asset,kind in [("styles.css","text/css"),("dashboard.js","javascript")]:
            result=client.get("/static/"+asset)
            assert result.status_code == 200
            assert kind in result.headers["content-type"]
        assert client.get("/api/status").json()["live_orders_available"] is False
        assert client.get("/docs").status_code == 200
