"""Measure from a photo. Put an object of known size (a bank card, a sheet of paper) on the same flat surface as the thing to
measure and photograph both. The reference's four corners give the plane's perspective, so any two points on that plane can
then be turned into millimetres. No LiDAR needed: any phone camera works, so this is also the route for Android."""
import json
from typing import Annotated

import cv2
import numpy as np
from fastapi import APIRouter, File, Form, HTTPException, Response, UploadFile
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/photo", tags=["photo"])

# Long side first, mm.
REFS = {
    "card": (85.60, 53.98),     # bank card, ID-1 (ISO 7810)
    "a4": (297.0, 210.0),
    "a5": (210.0, 148.0),
    "letter": (279.4, 215.9),
}
MAX_SIDE = 1200   # detection runs on a reduced copy


def reference_size(ref: str, w_mm: float | None = None, h_mm: float | None = None) -> tuple[float, float]:
    """(long, short) side in mm of the named reference or a custom one."""
    if ref == "custom":
        if not w_mm or not h_mm or w_mm <= 0 or h_mm <= 0:
            raise ValueError("custom reference needs a width and a height in mm")
        return (max(w_mm, h_mm), min(w_mm, h_mm))
    if ref not in REFS:
        raise ValueError(f"reference must be one of {', '.join(REFS)} or custom")
    return REFS[ref]


def _order(pts: np.ndarray) -> np.ndarray:
    """Four corners clockwise (image y down) starting at the top-left one."""
    c = pts.mean(axis=0)
    ang = np.arctan2(pts[:, 1] - c[1], pts[:, 0] - c[0])
    pts = pts[np.argsort(ang)]                 # clockwise on screen, starting left of centre going up
    s = pts.sum(axis=1)
    return np.roll(pts, -int(np.argmin(s)), axis=0)


def _long_side_first(quad: np.ndarray) -> np.ndarray:
    """Rotate the corner order so that corner 0 to 1 is the long side."""
    a = np.linalg.norm(quad[1] - quad[0]) + np.linalg.norm(quad[2] - quad[3])
    b = np.linalg.norm(quad[2] - quad[1]) + np.linalg.norm(quad[3] - quad[0])
    return quad if a >= b else np.roll(quad, -1, axis=0)


def _candidates(gray: np.ndarray):
    """Contours that could be the reference, from edges and from a light/dark split."""
    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.dilate(cv2.Canny(blur, 40, 120), np.ones((3, 3), np.uint8))
    _, otsu = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    for mask in (edges, otsu, 255 - otsu):
        cs, _ = cv2.findContours(mask, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        yield from cs


def detect_reference(img: np.ndarray, long_mm: float, short_mm: float) -> np.ndarray | None:
    """The reference's four corners in image pixels (corner 0 to 1 along its long side), or None.
    Looks for a convex four-sided shape of the right proportions, preferring the biggest."""
    h, w = img.shape[:2]
    k = min(1.0, MAX_SIDE / max(h, w))
    small = cv2.resize(img, None, fx=k, fy=k, interpolation=cv2.INTER_AREA) if k < 1 else img
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    area_img = gray.shape[0] * gray.shape[1]
    target = long_mm / short_mm
    # A card cannot fill most of the picture; a sheet of paper can. This keeps a table edge from being taken for the card.
    max_frac = 0.35 if long_mm < 120 else 0.85
    best, best_score = None, 0.0
    for c in _candidates(gray):
        area = cv2.contourArea(c)
        if area < 0.004 * area_img or area > max_frac * area_img:
            continue
        approx = cv2.approxPolyDP(c, 0.03 * cv2.arcLength(c, True), True)
        if len(approx) != 4 or not cv2.isContourConvex(approx):
            continue
        quad = _long_side_first(_order(approx.reshape(4, 2).astype(float)))
        a = (np.linalg.norm(quad[1] - quad[0]) + np.linalg.norm(quad[2] - quad[3])) / 2
        b = (np.linalg.norm(quad[2] - quad[1]) + np.linalg.norm(quad[3] - quad[0])) / 2
        if min(a, b) < 8:
            continue
        ratio = max(a, b) / min(a, b)
        off = abs(np.log(ratio / target))
        if off > 0.35:                          # perspective bends the proportions a bit, not this much
            continue
        score = np.sqrt(area) * np.exp(-6 * off)   # proportions matter more than size
        if score > best_score:
            best, best_score = quad, score
    return None if best is None else best / k


def homography(corners: np.ndarray, long_mm: float, short_mm: float) -> np.ndarray:
    """3x3 matrix taking image pixels to millimetres on the reference's plane (x along its long side)."""
    quad = _long_side_first(_order(np.asarray(corners, dtype=float).reshape(4, 2)))
    q32 = quad.astype(np.float32)
    if cv2.contourArea(q32) < 1 or not cv2.isContourConvex(q32):   # OpenCV returns a meaningless matrix for these, it does not refuse
        raise ValueError("those four points do not make a four-sided shape")
    dst = np.array([[0, 0], [long_mm, 0], [long_mm, short_mm], [0, short_mm]], dtype=np.float32)
    return cv2.getPerspectiveTransform(q32, dst)


def to_mm(H: np.ndarray, p) -> np.ndarray:
    v = H @ np.array([p[0], p[1], 1.0])
    return v[:2] / v[2]


def distance_mm(H: np.ndarray, p, q) -> float:
    return float(np.linalg.norm(to_mm(H, p) - to_mm(H, q)))


def tilt_warning(corners: np.ndarray) -> str | None:
    """How much the reference is foreshortened: opposite sides of different length mean the camera was not straight above."""
    q = _long_side_first(_order(np.asarray(corners, dtype=float).reshape(4, 2)))
    sides = [np.linalg.norm(q[(i + 1) % 4] - q[i]) for i in range(4)]
    worst = max(max(sides[0], sides[2]) / min(sides[0], sides[2]), max(sides[1], sides[3]) / min(sides[1], sides[3]))
    if worst > 1.3:
        return "The photo was taken at a steep angle, so far-away parts are less exact. Hold the phone straight above the surface."
    return None


def rectify(img: np.ndarray, H: np.ndarray, long_mm: float, short_mm: float, px_per_mm: float = 4.0):
    """A top-down view of the plane, centred on the reference with room round it. Returns (image, px_per_mm used)."""
    pad = 1.5 * long_mm
    x0, y0, x1, y1 = -pad, -pad, long_mm + pad, short_mm + pad
    s = min(px_per_mm, 2400 / (x1 - x0), 2400 / (y1 - y0))
    T = np.array([[s, 0, -x0 * s], [0, s, -y0 * s], [0, 0, 1]])
    out = cv2.warpPerspective(img, T @ H, (int((x1 - x0) * s), int((y1 - y0) * s)), flags=cv2.INTER_AREA)
    return out, s


def _decode(data: bytes) -> np.ndarray:
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)   # applies the phone's EXIF rotation
    if img is None:
        raise HTTPException(400, "that file is not a picture")
    return img


def _ref(ref: str, w_mm: float | None, h_mm: float | None) -> tuple[float, float]:
    try:
        return reference_size(ref, w_mm, h_mm)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/references")
def references():
    return {k: {"long_mm": v[0], "short_mm": v[1]} for k, v in REFS.items()}


@router.post("/detect")
async def detect(file: UploadFile = File(...), ref: str = Form("card"), w_mm: float | None = Form(None), h_mm: float | None = Form(None)):
    """Find the reference in a photo. Returns its corners (image pixels) and the pixel-to-mm matrix, or `found: false`
    so the page can ask the user to drag the four corners themselves."""
    long_mm, short_mm = _ref(ref, w_mm, h_mm)
    img = _decode(await file.read())
    quad = detect_reference(img, long_mm, short_mm)
    out = {"found": quad is not None, "width": img.shape[1], "height": img.shape[0], "ref_mm": [long_mm, short_mm]}
    if quad is not None:
        out["corners"] = np.round(quad, 1).tolist()
        out["H"] = homography(quad, long_mm, short_mm).ravel().tolist()
        out["warning"] = tilt_warning(quad)
    return out


class Corners(BaseModel):
    corners: list[Annotated[list[float], Field(min_length=2, max_length=2)]] = Field(min_length=4, max_length=4)
    ref: str = "card"
    w_mm: float | None = None
    h_mm: float | None = None


@router.post("/homography")
def homography_for(req: Corners):
    """The pixel-to-mm matrix for four corners the user placed by hand."""
    long_mm, short_mm = _ref(req.ref, req.w_mm, req.h_mm)
    try:
        H = homography(np.array(req.corners), long_mm, short_mm)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"H": H.ravel().tolist(), "ref_mm": [long_mm, short_mm], "warning": tilt_warning(np.array(req.corners))}


@router.post("/rectify")
async def rectify_photo(file: UploadFile = File(...), corners: str = Form(...), ref: str = Form("card"),
                        w_mm: float | None = Form(None), h_mm: float | None = Form(None)):
    """The photo straightened into a top-down, to-scale picture (PNG). `corners` is JSON [[x,y] x4] in the photo's pixels."""
    long_mm, short_mm = _ref(ref, w_mm, h_mm)
    try:
        pts = np.array(json.loads(corners), dtype=float).reshape(4, 2)
    except (ValueError, TypeError):
        raise HTTPException(400, "corners must be JSON [[x,y],[x,y],[x,y],[x,y]]")
    try:
        H = homography(pts, long_mm, short_mm)
    except ValueError as e:
        raise HTTPException(400, str(e))
    img = _decode(await file.read())
    out, s = rectify(img, H, long_mm, short_mm)
    _, png = cv2.imencode(".png", out)
    return Response(png.tobytes(), media_type="image/png", headers={"X-Px-Per-Mm": f"{s:.4f}"})
