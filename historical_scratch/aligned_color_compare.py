from pathlib import Path

import cv2
import numpy as np


SOURCE = Path(r"D:\工作内容\数字人\姜\2.80\测试1.mp4")
RAW = Path(r"D:\工作内容\轻盈健\数字人\.codex_tmp\ltx_color_05454\ltx.mp4")
RESTORED = Path(r"D:\工作内容\轻盈健\数字人\.codex_tmp\ltx_color_05454\restore_test\ltx-restored-100.mp4")
OUT = Path(r"D:\工作内容\轻盈健\数字人\.codex_tmp\ltx_color_05454\aligned-color-compare.png")
TIMES = (3.0, 8.0, 13.0, 18.0)


def read_frame(path: Path, seconds: float) -> np.ndarray:
    cap = cv2.VideoCapture(str(path))
    cap.set(cv2.CAP_PROP_POS_MSEC, seconds * 1000.0)
    ok, frame = cap.read()
    cap.release()
    if not ok:
        raise RuntimeError(f"Cannot read {path} at {seconds}s")
    return frame


def fit_source(frame: np.ndarray) -> np.ndarray:
    # 1080x1920 -> the exact centered 1056x1920 geometry used by the LTX output.
    left = (frame.shape[1] - 1056) // 2
    return frame[:, left : left + 1056]


def labelled(frame: np.ndarray, title: str) -> np.ndarray:
    frame = cv2.resize(frame, (396, 720), interpolation=cv2.INTER_AREA)
    cv2.rectangle(frame, (0, 0), (396, 42), (20, 20, 20), -1)
    cv2.putText(frame, title, (10, 29), cv2.FONT_HERSHEY_SIMPLEX, 0.67, (255, 255, 255), 2, cv2.LINE_AA)
    return frame


rows = []
for seconds in TIMES:
    source = fit_source(read_frame(SOURCE, seconds))
    raw = read_frame(RAW, seconds)
    restored = read_frame(RESTORED, seconds)
    rows.append(
        np.hstack(
            [
                labelled(source, f"SOURCE {seconds:.0f}s"),
                labelled(raw, f"RAW LTX {seconds:.0f}s"),
                labelled(restored, f"RESTORED {seconds:.0f}s"),
            ]
        )
    )

OUT.parent.mkdir(parents=True, exist_ok=True)
ok, encoded = cv2.imencode(".png", np.vstack(rows))
if not ok:
    raise RuntimeError("PNG encoding failed")
encoded.tofile(OUT)
print(OUT)
