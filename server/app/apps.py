"""Hand out the phone apps from this server: the Android APK and the iPhone IPA live in data/apps/ (put there by tools/get-apps.sh)."""
import re
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

router = APIRouter(tags=["apps"])
APPS = Path(__file__).resolve().parent.parent / "data" / "apps"   # /app/data/apps in Docker
TYPES = {".apk": "application/vnd.android.package-archive", ".ipa": "application/octet-stream"}


@router.get("/api/apps")
def list_apps():
    """The app files this server can hand out, newest first."""
    out = []
    for p in sorted(APPS.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True) if APPS.is_dir() else []:
        if p.suffix.lower() in TYPES and p.is_file():
            st = p.stat()
            out.append({"name": p.name, "kind": "android" if p.suffix.lower() == ".apk" else "ios", "size": st.st_size,
                        "modified": st.st_mtime, "age_hours": round((time.time() - st.st_mtime) / 3600, 1)})
    return out


@router.get("/apps/{name}")
def download_app(name: str):
    p = APPS / name
    if not re.fullmatch(r"[\w.-]+", name) or p.suffix.lower() not in TYPES or not p.is_file():
        raise HTTPException(404, "no such app file")
    return FileResponse(p, media_type=TYPES[p.suffix.lower()], filename=p.name)
