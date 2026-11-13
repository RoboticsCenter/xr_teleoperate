"""Render a read-only distance board for the original PICO VideoBridge.

The caller supplies DistanceMeter.sample() output. No camera, tracker, robot,
network or calibration actions run here. Pillow is the only image dependency;
the bridge handles JPEG -> H.264 and repeats this mono image for both eyes.
"""
from functools import lru_cache
from io import BytesIO
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


SIZE = (1280, 720)
BACKGROUND = '#0b1424'
PANEL = '#15243a'
TEXT = '#f1f6ff'
MUTED = '#a9b8cf'
LIVE = '#72edc5'
WAIT = '#ffd386'


@lru_cache(maxsize=1)
def _font_family():
    cjk = Path('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc')
    if cjk.is_file():
        return str(cjk), True
    fallback = Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')
    return str(fallback) if fallback.is_file() else None, False


@lru_cache(maxsize=24)
def _font(size):
    path, cjk = _font_family()
    if path:
        return ImageFont.truetype(path, size, index=2 if cjk else 0)
    return ImageFont.load_default(size=size)


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _number(value, *, signed=False):
    if not _finite(value):
        return '--'
    # Avoid displaying a negative zero after rounding near the origin.
    value = 0. if abs(value) < .05 else value
    return format(value, '+.1f' if signed else '.1f')


def _fit_text(draw, text, xy, width, size, fill=TEXT):
    while size > 14 and draw.textlength(text, font=_font(size)) > width:
        size -= 2
    draw.text(xy, text, font=_font(size), fill=fill)


def _lines(draw, text, xy, width, size, fill, limit=2):
    """Wrap both CJK and English text without allowing it outside the board."""
    line, rows = '', []
    for character in str(text).replace('\n', ' ')[:500]:
        if line and draw.textlength(line+character, font=_font(size)) > width:
            rows.append(line)
            line = ''
            if len(rows) == limit:
                break
        line += character
    if line and len(rows) < limit:
        rows.append(line)
    for index, row in enumerate(rows):
        draw.text((xy[0], xy[1]+index*(size+5)), row, font=_font(size), fill=fill)


def render_distance_jpeg(status):
    """Return a 1280x720 JPEG, hiding invalid/stale/uncaptured measurements.

    Per-side live values remain visible during a loss of the other wrist. This
    display does not impose an additional freshness threshold or change units.
    """
    status = status if isinstance(status, dict) else {}
    chinese = _font_family()[1]
    image = Image.new('RGB', SIZE, BACKGROUND)
    draw = ImageDraw.Draw(image)
    title = 'PICO 原始读数 · 仅供参考' if chinese else 'PICO RAW REFERENCE / NOT PHYSICAL DISTANCE'
    draw.text((36, 24), title, font=_font(35), fill=TEXT)
    subtitle = ('距本页起点的 Tracker 位移 · 非实测距离 · 非累计路程'
                if chinese else 'Tracker displacement from page zero / not measured physical distance / not path length')
    _fit_text(draw, subtitle, (38, 78), 1204, 24, MUTED)

    pending = status.get('zero_pending') is True
    usable_origin = (status.get('has_origin') is True and
                     status.get('needs_zero') is False and not pending)
    calibration_error = status.get('calibration_error')
    if pending:
        detail = ('等待起点：回到 XRoboToolkit，保持同一物理原点，收到有效输入后置零。'
                  if chinese else 'Zero pending: return to XRoboToolkit and hold the same physical origin for valid input.')
    elif calibration_error:
        detail = (str(calibration_error) if chinese else 'Calibration status unavailable; check the laptop dashboard.')
    elif not usable_origin:
        detail = ('尚无有效测量起点。请先回到标定的同一物理原点，再设置本页起点。'
                  if chinese else 'No valid measurement zero. Return to the SAME physical calibration origin, then set page zero.')
    else:
        detail = (status.get('detail') or '正在测量。') if chinese else (
            'Live displacement from page zero.' if status.get('ready') is True else
            'Waiting for valid wrist input; unavailable measurements are hidden.')
    draw.rounded_rectangle((36, 120, 1244, 198), radius=12, fill=PANEL)
    _lines(draw, detail, (54, 130), 1170, 24,
           LIVE if status.get('ready') is True and not pending and not calibration_error else WAIT)

    selected = status.get('selected_sides', [])
    selected = selected if isinstance(selected, (list, tuple)) else []
    sides = status.get('sides', {})
    sides = sides if isinstance(sides, dict) else {}
    for side, x in (('left', 36), ('right', 656)):
        record = sides.get(side, {})
        record = record if isinstance(record, dict) else {}
        enabled = side in selected
        valid = enabled and usable_origin and record.get('live') is True
        draw.rounded_rectangle((x, 218, x+588, 573), radius=18, fill=PANEL)
        name = ('左腕 / LEFT' if side == 'left' else '右腕 / RIGHT') if chinese else side.upper()+' WRIST'
        draw.text((x+26, 232), name, font=_font(30), fill=TEXT)
        label = ('实时' if valid else '未选择' if not enabled else '等待输入 / 起点') if chinese else (
            'LIVE' if valid else 'NOT SELECTED' if not enabled else 'WAITING')
        draw.text((x+340, 240), label, font=_font(20), fill=LIVE if valid else WAIT)
        value = _number(record.get('displacement_cm') if valid else None)
        _fit_text(draw, value+' cm', (x+26, 275), 536, 83, LIVE if valid else MUTED)
        for index, (key, zh, en) in enumerate((('forward_cm', '向前', 'FORWARD'),
                                               ('left_cm', '向左', 'LEFT'),
                                               ('up_cm', '向上', 'UP'))):
            y = 399+index*48
            draw.text((x+28, y), zh if chinese else en, font=_font(27), fill=MUTED)
            value = _number(record.get(key) if valid else None, signed=True)+' cm'
            _fit_text(draw, value, (x+260, y), 300, 31, TEXT if valid else MUTED)

    stages = {'origin': '原点', 'forward': '向前', 'left': '向左', 'up': '向上', 'complete': '已采集全部方向'}
    stage = status.get('current_stage')
    stage = (stages.get(stage, '等待') if chinese else
             str(stage).upper() if stage in stages else 'WAITING')
    target = _number(status.get('target_cm'))
    instruction = ('校准步骤：'+stage+'    设定的实际距离：'+target+' cm' if chinese else
                   'CALIBRATION: '+stage+'    ENTERED PHYSICAL DISTANCE: '+target+' cm')
    _fit_text(draw, instruction, (38, 598), 1204, 30)
    footer = ('校准以你输入的实际方向和距离为准；本页读数不会否决你的输入。' if chinese else
              'Calibration trusts your entered physical direction and distance, not these PICO reference readings.')
    _fit_text(draw, footer, (38, 652), 1204, 24, MUTED)
    output = BytesIO()
    image.save(output, format='JPEG', quality=88, subsampling=0)
    return output.getvalue()
