"""Where the server keeps things and how big they are, so a person can find the folder or clear it from the web page.
The container does the deleting, because data/ belongs to root on the host."""
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

# Files the server makes again by itself the next time they are asked for (or, for a cut, when the user cuts again).
DERIVED = re.compile(r"^(panorama\.jpg|panorama\.json|quality\.json|features\.json|colours\.json|model\.usdz|"
                     r"model_part\d+\.stl|model_parts\.3mf|dowels\.stl)$")
BUSY = ("capturing", "building")        # a scan the phone or the server is still writing: left alone except by 'delete all'
TMP_MIN_AGE = 600                       # seconds: a temp entry younger than this may still be in use (a zip being sent, a build)

# The server's own temp folder, so clearing it never touches other programs' files when it runs outside Docker.
TMP = Path(os.environ.get("ROOMSCAN_TMP") or Path(tempfile.gettempdir()) / "roomscan")


def use_own_tmp():
    """Make every tempfile call in this process go to TMP."""
    TMP.mkdir(parents=True, exist_ok=True)
    tempfile.tempdir = str(TMP)


def _size(p: Path) -> tuple[int, int]:
    """(bytes, files) under p, not following links."""
    if p.is_symlink() or p.is_file():
        try:
            return p.lstat().st_size, 1
        except OSError:
            return 0, 0
    total = n = 0
    for root, _dirs, files in os.walk(p):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
                n += 1
            except OSError:
                pass
    return total, n


def _rm(p: Path):
    if p.is_dir() and not p.is_symlink():
        shutil.rmtree(p, ignore_errors=True)
    else:
        p.unlink(missing_ok=True)


def derived_files(scans: list[tuple[Path, dict]]) -> list[Path]:
    return [f for d, m in scans if m.get("status") not in BUSY for f in d.iterdir() if f.is_file() and DERIVED.match(f.name)]


def old_temp(now: float | None = None) -> list[Path]:
    if not TMP.is_dir():
        return []
    now = time.time() if now is None else now
    out = []
    for p in TMP.iterdir():
        try:
            if now - p.lstat().st_mtime >= TMP_MIN_AGE:
                out.append(p)
        except OSError:
            pass
    return out


def report(data: Path, apps: Path, scans: list[tuple[Path, dict]]) -> dict:
    """data = the scans folder. Paths as the server sees them and as the host computer sees them (DATA_HOST_PATH)."""
    host = os.environ.get("DATA_HOST_PATH", "").rstrip("/")
    s_bytes = s_files = 0
    for d, _ in scans:
        b, n = _size(d)
        s_bytes += b; s_files += n
    der = derived_files(scans)
    a_bytes, a_files = _size(apps) if apps.exists() else (0, 0)
    t_bytes, t_files = _size(TMP) if TMP.exists() else (0, 0)
    old = old_temp()
    out = {
        "data_dir": str(data.parent), "scans_dir": str(data), "apps_dir": str(apps), "temp_dir": str(TMP),
        "host_path": host or str(data.parent),
        "host_scans_path": f"{host}/{data.name}" if host else str(data),
        "scans": {"count": len(scans), "bytes": s_bytes, "files": s_files,
                  "busy": sum(1 for _, m in scans if m.get("status") in BUSY)},
        "derived": {"bytes": sum(f.stat().st_size for f in der), "files": len(der)},
        "apps": {"bytes": a_bytes, "files": a_files},
        "temp": {"bytes": t_bytes, "files": t_files, "removable": len(old), "removable_bytes": sum(_size(p)[0] for p in old)},
    }
    if not host:
        out["note"] = ("DATA_HOST_PATH is not set, so these are the paths inside the server. deploy.sh sets it to the data folder "
                       "next to docker-compose.yml.")
    return out


def delete_temp() -> dict:
    old = old_temp()
    freed = sum(_size(p)[0] for p in old)
    for p in old:
        _rm(p)
    return {"deleted": len(old), "bytes": freed}


def delete_derived(scans: list[tuple[Path, dict]]) -> dict:
    files = derived_files(scans)
    freed = 0
    for f in files:
        try:
            freed += f.stat().st_size
            f.unlink()
        except OSError:
            pass
    return {"deleted": len(files), "bytes": freed}
