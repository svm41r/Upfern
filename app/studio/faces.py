"""Finds the speaker's face in a stretch of video, so the crop can be framed on it.

The detector is YuNet (a small model OpenCV supports; fetched once into the data folder). If it cannot be
fetched, OpenCV's older built-in detectors are used instead."""
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import config
from .media import NO_WINDOW

YUNET = "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx"
_yn = {"tried": False, "path": None}
_haar = []


def _yunet_file():
    if not _yn["tried"]:
        _yn["tried"] = True
        f = Path(config.DATA_DIR) / "face_yunet.onnx"
        try:
            if not f.is_file() or f.stat().st_size < 100_000:
                import httpx
                r = httpx.get(YUNET, follow_redirects=True, timeout=30)
                if r.status_code == 200 and len(r.content) > 100_000:
                    f.write_bytes(r.content)
            _yn["path"] = str(f) if f.is_file() else None
        except Exception:  # noqa: BLE001
            _yn["path"] = None
    return _yn["path"]


def _faces(img):
    """Every face in one picture as (x, y, w, h, how sure) in pixels."""
    import cv2
    ih, iw = img.shape[:2]
    model = _yunet_file()
    if model and hasattr(cv2, "FaceDetectorYN"):
        det = cv2.FaceDetectorYN.create(model, "", (iw, ih), 0.6, 0.3, 50)
        _, got = det.detect(img)
        return [(float(f[0]), float(f[1]), float(f[2]), float(f[3]), float(f[-1])) for f in (got if got is not None else [])]
    if not _haar:
        _haar.extend(cv2.CascadeClassifier(cv2.data.haarcascades + n) for n in
                     ("haarcascade_frontalface_default.xml", "haarcascade_profileface.xml"))
    grey = cv2.equalizeHist(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY))
    out = []
    for det, pic, flip in ((_haar[0], grey, False), (_haar[1], grey, False), (_haar[1], cv2.flip(grey, 1), True)):
        for x, y, w, h in det.detectMultiScale(pic, scaleFactor=1.1, minNeighbors=5, minSize=(iw // 16, iw // 16)):
            out.append((float(iw - x - w if flip else x), float(y), float(w), float(h), 0.7))
        if out:
            break
    return out


def track(src: Path, start: float, end: float, max_samples: int = 90) -> list:
    """Where the speaker's face is across [start, end]: [[seconds from start, centre x as a fraction of the width], ...].
    The picture is looked at about twice a second. The face followed is the one nearest to where it was a moment
    ago, so a second person walking through does not pull the frame away. The path is smoothed, and only the
    points where it really moves are kept, so the crop glides instead of shaking. Empty when no face is seen."""
    import cv2
    work = Path(tempfile.mkdtemp(prefix="ft"))
    try:
        dur = end - start
        step = max(0.4, dur / max_samples)
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{max(0.0, start):.2f}", "-t", f"{dur:.2f}", "-i", str(src),
                        "-vf", f"fps=1/{step:.3f},scale=640:-2", "-q:v", "4", str(work / "f%03d.jpg")],
                       capture_output=True, creationflags=NO_WINDOW, timeout=300)
        seen = []                                   # per frame: [(centre x, size)] of every sure face
        for f in sorted(work.glob("f*.jpg")):
            img = cv2.imread(str(f))
            if img is None:
                seen.append([])
                continue
            ih, iw = img.shape[:2]
            seen.append([((x + w / 2) / iw, w * h * s) for x, y, w, h, s in _faces(img) if w >= iw * 0.035])
        biggest = sorted(max(hits, key=lambda b: b[1])[0] for hits in seen if hits)
        if not biggest:
            return []
        pos = biggest[len(biggest) // 2]            # start from where the main face usually is
        xs = []
        for hits in seen:
            near = [h for h in hits if abs(h[0] - pos) < 0.28]
            if near:
                pos = min(near, key=lambda h: abs(h[0] - pos))[0]
            xs.append(pos)
        smooth = [sum(xs[max(0, i - 2): i + 3]) / len(xs[max(0, i - 2): i + 3]) for i in range(len(xs))]
        slack = 0.025
        while True:                                 # keep only the turns; widen the slack until there are few enough points
            keys = [[0.0, smooth[0]]]
            for i, x in enumerate(smooth[1:], 1):
                if abs(x - keys[-1][1]) > slack:
                    if keys[-1][0] < (i - 1) * step - 0.01:
                        keys.append([(i - 1) * step, keys[-1][1]])       # hold still until just before the move
                    keys.append([i * step, x])
            if len(keys) <= 48:
                break
            slack += 0.015
        if keys[-1][0] < dur:
            keys.append([dur, keys[-1][1]])
        return [[round(t, 2), round(min(1.0, max(0.0, x)), 4)] for t, x in keys]
    finally:
        shutil.rmtree(work, ignore_errors=True)


def find(src: Path, start: float, end: float, samples: int = 12):
    """The speaker's usual face box over [start, end] as {'x','y','w','h'} in fractions of the picture, or None.
    Several frames are looked at. In each, the biggest sure face is the speaker; the middle value over the frames
    is used, so one odd frame does not move the crop."""
    import cv2
    work = Path(tempfile.mkdtemp(prefix="fc"))
    try:
        step = max(0.4, (end - start) / samples)
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{max(0.0, start):.2f}", "-t", f"{end - start:.2f}", "-i", str(src),
                        "-vf", f"fps=1/{step:.3f},scale=960:-2", "-frames:v", str(samples), "-q:v", "3", str(work / "f%02d.jpg")],
                       capture_output=True, creationflags=NO_WINDOW, timeout=180)
        found = []
        for f in sorted(work.glob("f*.jpg")):
            img = cv2.imread(str(f))
            if img is None:
                continue
            ih, iw = img.shape[:2]
            hits = [h for h in _faces(img) if h[2] >= iw * 0.035]
            if hits:
                x, y, w, h, _ = max(hits, key=lambda b: b[2] * b[3] * b[4])
                found.append((max(0.0, x) / iw, max(0.0, y) / ih, w / iw, h / ih))
        if not found:
            return None
        # keep the frames that agree with the middle one, so a second person on screen for a moment is ignored
        cxs = sorted(b[0] + b[2] / 2 for b in found)
        mid_cx = cxs[len(cxs) // 2]
        near = [b for b in found if abs(b[0] + b[2] / 2 - mid_cx) < 0.18] or found
        mid = lambda i: float(sorted(b[i] for b in near)[len(near) // 2])     # noqa: E731
        return {"x": round(mid(0), 4), "y": round(mid(1), 4), "w": round(mid(2), 4), "h": round(mid(3), 4),
                "seen": len(found)}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _find_lines_1d(strength, thresh: float = 0.50, min_gap: int = 8) -> list[int]:
    """Local peak positions in a 1D line-strength curve, always including boundary 0 and N-1."""
    n = len(strength)
    pts = [0, n - 1]
    peaks = []
    for i in range(2, n - 2):
        v = float(strength[i])
        if v < thresh:
            continue
        win = strength[max(0, i - 3):min(n, i + 4)]
        if v >= float(win.max()):
            peaks.append((v, i))
    # Greedy non-maximum suppression sorted by strongest line first
    chosen = []
    for _, idx in sorted(peaks, reverse=True):
        if all(abs(idx - c) >= min_gap for c in chosen):
            chosen.append(idx)
    return sorted(set(pts + chosen))


def find_cam_box(src: Path, start: float, end: float, samples: int = 8):
    """Detect the static OBS streamer facecam cutout rectangle [x, y, w, h] (0..1 fractions).

    Combines persistent straight-edge geometry (so the entire rectangular webcam overlay is selected
    cleanly) with multi-scale face localization (including zoomed corner/edge crops for small cams).
    Returns {"box": [x, y, w, h], "method": "rect"|"face_pad", "score": float} or None. Never tracks.
    """
    import cv2
    import numpy as np

    work = Path(tempfile.mkdtemp(prefix="fcbox"))
    try:
        dur = max(1.0, float(end) - float(start))
        n_samp = max(4, min(10, int(samples)))
        step = max(0.5, dur / n_samp)
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-ss", f"{max(0.0, float(start)):.2f}", "-t", f"{dur:.2f}",
             "-i", str(src), "-vf", f"fps=1/{step:.3f},scale=640:-2", "-frames:v", str(n_samp),
             "-q:v", "3", str(work / "f%02d.jpg")],
            capture_output=True, creationflags=NO_WINDOW, timeout=120,
        )
        imgs = [cv2.imread(str(f)) for f in sorted(work.glob("f*.jpg"))]
        imgs = [im for im in imgs if im is not None]
        if not imgs:
            return None
        H, W = imgs[0].shape[:2]

        # 1. Persistent straight borders + motion energy
        grays = [cv2.cvtColor(im, cv2.COLOR_BGR2GRAY).astype(np.float32) for im in imgs]
        gxs = [np.abs(cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3)) for g in grays]
        gys = [np.abs(cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3)) for g in grays]
        med_gx = np.median(gxs, axis=0)
        med_gy = np.median(gys, axis=0)

        if len(grays) >= 2:
            diffs = [np.abs(grays[i + 1] - grays[i]) for i in range(len(grays) - 1)]
            motion = np.mean(diffs, axis=0)
        else:
            motion = np.zeros_like(grays[0])

        # Local segment detectors: find straight edge runs >= 45px vertical / >= 55px horizontal anywhere
        vx_bin = (med_gx > 20).astype(np.float32)
        hy_bin = (med_gy > 20).astype(np.float32)
        vx_run = cv2.boxFilter(vx_bin, -1, (3, 45), normalize=True)
        hy_run = cv2.boxFilter(hy_bin, -1, (55, 3), normalize=True)
        x_lines = _find_lines_1d(vx_run.max(axis=0), thresh=0.48, min_gap=8)
        y_lines = _find_lines_1d(hy_run.max(axis=1), thresh=0.48, min_gap=8)

        # 2. Multi-scale face detection (full frame + 2x zoomed corner/edge quadrants for small cams)
        face_hits = []
        rois = [
            (0.0, 0.0, 1.0, 1.0, 1),
            (0.0, 0.0, 0.55, 0.65, 2),
            (0.45, 0.0, 0.55, 0.65, 2),
            (0.0, 0.35, 0.55, 0.65, 2),
            (0.45, 0.35, 0.55, 0.65, 2),
            (0.0, 0.22, 0.48, 0.58, 2),
            (0.52, 0.22, 0.48, 0.58, 2),
        ]
        sample_imgs = imgs[:4] if len(imgs) > 4 else imgs
        for im in sample_imgs:
            found_this = False
            for rx, ry, rw, rh, zoom in rois:
                px0, py0 = int(rx * W), int(ry * H)
                px1, py1 = int((rx + rw) * W), int((ry + rh) * H)
                crop = im[py0:py1, px0:px1]
                if zoom > 1:
                    crop = cv2.resize(crop, (crop.shape[1] * zoom, crop.shape[0] * zoom), interpolation=cv2.INTER_LINEAR)
                cw, ch = crop.shape[1], crop.shape[0]
                for fx, fy, fw, fh, conf in _faces(crop):
                    fw_full = (fw / zoom) / W
                    fh_full = (fh / zoom) / H
                    if not (0.015 <= fw_full <= 0.22):
                        continue
                    fcx = (px0 + (fx + fw / 2) / zoom) / W
                    fcy = (py0 + (fy + fh / 2) / zoom) / H
                    face_hits.append((fcx, fcy, fw_full, fh_full, conf))
                    found_this = True
                if zoom == 1 and found_this:
                    break

        face = None
        if face_hits:
            # Keep cluster near median center so one false detection doesn't skew
            cxs = sorted(f[0] for f in face_hits)
            cys = sorted(f[1] for f in face_hits)
            mcx, mcy = cxs[len(cxs) // 2], cys[len(cys) // 2]
            near = [f for f in face_hits if abs(f[0] - mcx) < 0.14 and abs(f[1] - mcy) < 0.14] or face_hits
            med = lambda k: float(sorted(f[k] for f in near)[len(near) // 2])
            face = (med(0), med(1), med(2), med(3))

        # 3. Evaluate candidate rectangles formed by persistent OBS edges
        def _v_support(col: int, y1: int, y2: int) -> float:
            if col <= 3 or col >= W - 4:
                return 1.0  # flush with screen boundary
            band = med_gx[y1:y2, max(0, col - 2):min(W, col + 3)].max(axis=1)
            return float(np.mean(band > 18)) if len(band) else 0.0

        def _h_support(row: int, x1: int, x2: int) -> float:
            if row <= 3 or row >= H - 4:
                return 1.0
            band = med_gy[max(0, row - 2):min(H, row + 3), x1:x2].max(axis=0)
            return float(np.mean(band > 18)) if len(band) else 0.0

        best_rect = None
        best_score = -1e9

        for i, x1 in enumerate(x_lines):
            for x2 in x_lines[i + 1:]:
                rw_px = x2 - x1
                w_frac = rw_px / W
                if not (0.12 <= w_frac <= 0.48):
                    continue
                for j, y1 in enumerate(y_lines):
                    for y2 in y_lines[j + 1:]:
                        rh_px = y2 - y1
                        h_frac = rh_px / H
                        if not (0.14 <= h_frac <= 0.56):
                            continue
                        aspect = rw_px / max(1, rh_px)
                        if not (0.85 <= aspect <= 2.35):
                            continue

                        sL = _v_support(x1, y1, y2)
                        sR = _v_support(x2, y1, y2)
                        sT = _h_support(y1, x1, x2)
                        sB = _h_support(y2, x1, x2)
                        min_edge = min(sL, sR, sT, sB)
                        avg_edge = (sL + sR + sT + sB) / 4.0
                        if min_edge < 0.45 or avg_edge < 0.65:
                            continue

                        # Must have at least 2 interior straight borders (not just whole corner without edges)
                        interior = int(x1 > 3) + int(x2 < W - 4) + int(y1 > 3) + int(y2 < H - 4)
                        if interior < 2:
                            continue

                        score = avg_edge * 60.0 + min_edge * 30.0
                        # Standard OBS widescreen webcam cutout (~16:9 = 1.78, range 1.45..1.95)
                        score += max(0.0, 14.0 - abs(aspect - 1.72) * 18.0)
                        # Screen edge anchor bonus
                        if x1 <= 3 or x2 >= W - 4:
                            score += 12.0
                        if y1 <= 3 or y2 >= H - 4:
                            score += 6.0

                        # Motion inside box (live webcam vs static HUD/menu)
                        in_mot = float(np.mean(motion[y1:y2, x1:x2]))
                        score += min(18.0, in_mot * 1.4)

                        if face is not None:
                            fcx, fcy, fw_f, fh_f = face
                            rx0, ry0, rx1, ry1 = x1 / W, y1 / H, x2 / W, y2 / H
                            head_top = max(0.0, fcy - fh_f * 0.72)
                            if rx0 <= fcx <= rx1 and ry0 <= head_top and fcy <= ry1:
                                score += 120.0
                                x_rel = (fcx - rx0) / max(0.01, w_frac)
                                y_rel = (fcy - ry0) / max(0.01, h_frac)
                                # Streamers sit centered inside their OBS webcam frame
                                if 0.15 <= x_rel <= 0.85 and 0.18 <= y_rel <= 0.75:
                                    score += 20.0 + max(0.0, 12.0 * (1.0 - abs(x_rel - 0.5) * 2.2))
                                rel_fw = fw_f / max(0.05, w_frac)
                                if 0.10 <= rel_fw <= 0.52:
                                    score += 16.0
                                area = w_frac * h_frac
                                if area > 0.14:
                                    score -= (area - 0.14) * 120.0
                            else:
                                score -= 90.0
                        else:
                            # Without detected face, reject likely low-motion game UI boxes
                            if in_mot < 2.0:
                                continue

                        if score > best_score:
                            best_score = score
                            best_rect = [
                                round(x1 / W, 4),
                                round(y1 / H, 4),
                                round(w_frac, 4),
                                round(h_frac, 4),
                            ]

        if best_rect and best_score >= 95.0:
            return {"box": best_rect, "method": "rect", "score": round(best_score, 1)}

        # 4. Borderless webcam fallback: clean fixed 16:9 cutout centered around face + shoulders
        if face is not None:
            fcx, fcy, fw_f, fh_f = face
            cw = max(0.20, min(0.40, fw_f * 4.5))
            ch = max(0.18, min(0.46, cw * (9.0 / 16.0) * (W / H)))
            cx0 = max(0.0, min(1.0 - cw, fcx - cw * 0.5))
            cy0 = max(0.0, min(1.0 - ch, fcy - ch * 0.36))
            return {
                "box": [round(cx0, 4), round(cy0, 4), round(cw, 4), round(ch, 4)],
                "method": "face_pad",
                "score": round(max(0.0, best_score), 1),
            }

        return None
    finally:
        shutil.rmtree(work, ignore_errors=True)
