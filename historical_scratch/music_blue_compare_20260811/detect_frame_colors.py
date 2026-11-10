from pathlib import Path

import cv2
import numpy as np


MEDIA = Path(r"D:\工作内容\轻盈健\数字人\.codex_tmp\music_blue_compare_20260811\xlsx_unpacked\xl\media")

for path in sorted(MEDIA.glob("image*.*"), key=lambda value: int("".join(filter(str.isdigit, value.stem)))):
    image = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    cyan_mask = cv2.inRange(hsv, np.array([75, 90, 90]), np.array([105, 255, 255]))
    red_mask = cv2.bitwise_or(
        cv2.inRange(hsv, np.array([0, 100, 100]), np.array([10, 255, 255])),
        cv2.inRange(hsv, np.array([170, 100, 100]), np.array([179, 255, 255])),
    )

    def wide_components(mask):
        joined = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8), iterations=1)
        contours, _ = cv2.findContours(joined, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        boxes = []
        for contour in contours:
            x, y, width, height = cv2.boundingRect(contour)
            if width >= 150 and height >= 45:
                boxes.append((width * height, x, y, width, height))
        return sorted(boxes, reverse=True)

    cyan_boxes = wide_components(cyan_mask)
    red_boxes = wide_components(red_mask)
    if cyan_boxes and not red_boxes:
        dominant = "BLUE"
    elif red_boxes and not cyan_boxes:
        dominant = "RED"
    elif cyan_boxes and red_boxes:
        dominant = "BLUE" if cyan_boxes[0][0] >= red_boxes[0][0] else "RED"
    else:
        dominant = "NONE"
    print(f"{path.name}|{dominant}|blue_boxes={cyan_boxes[:2]}|red_boxes={red_boxes[:2]}")
