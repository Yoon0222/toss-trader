"""Windows Task Scheduler entrypoint; no secret values in args or logs."""
import os
import sys
from pathlib import Path
root=Path(__file__).resolve().parents[1]
os.chdir(root)
sys.path.insert(0,str(root))
(root/"work").mkdir(exist_ok=True)
sys.stdout=(root/"work/service.out.log").open("a",encoding="utf-8",buffering=1)
sys.stderr=(root/"work/service.err.log").open("a",encoding="utf-8",buffering=1)
from app.paper_runner import WorkerLock
lock=WorkerLock(root/"work/service.lock")
if not lock.acquire():sys.exit(0)
try:
    import httpx
    try:
        response=httpx.get("http://127.0.0.1:8000/api/status",timeout=2)
        if response.status_code==200 and response.json().get("paper_validation_enabled") is True:sys.exit(0)
    except (httpx.HTTPError,ValueError):pass
    import uvicorn
    uvicorn.run("app.main:app",host="127.0.0.1",port=8000,workers=1,log_level="warning")
finally:lock.close()
