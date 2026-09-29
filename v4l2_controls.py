"""Discover and apply USB camera controls without opening a second video stream."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path


# Only expose image controls with understood, integer V4L2 semantics.
LABELS = {
    'white_balance_automatic': '自動白平衡',
    'white_balance_temperature': '白平衡色溫',
    'auto_exposure': '曝光模式',
    'exposure_time_absolute': '曝光時間',
    'exposure_dynamic_framerate': '動態影格率',
    'focus_automatic_continuous': '自動對焦',
    'focus_absolute': '手動對焦',
    'brightness': '亮度',
    'contrast': '對比',
    'saturation': '飽和度',
    'sharpness': '銳利度',
    'gain': '增益',
    'backlight_compensation': '背光補償',
    'power_line_frequency': '抗閃爍頻率',
    'zoom_absolute': '相機變焦',
    'pan_absolute': '水平平移',
    'tilt_absolute': '垂直平移',
}
AUTO_FIRST = ('white_balance_automatic', 'auto_exposure',
              'focus_automatic_continuous', 'exposure_dynamic_framerate')
CONTROL_LINE = re.compile(r'^\s*([a-z][a-z0-9_]*)\s+0x[0-9a-f]+\s+\((int|bool|menu)\)\s*:\s*(.*)$', re.I)
CHOICE_LINE = re.compile(r'^\s*(-?\d+):\s*(.+)$')
PROPERTY = re.compile(r'(\w+)=(-?\d+|[\w,-]+)')


def device_path(source):
    if not sys.platform.startswith('linux'):
        return None
    source = str(source).strip()
    if source.isdigit():
        source = f'/dev/video{source}'
    if not source.startswith(('/dev/video', '/dev/v4l/')):
        return None
    return source if Path(source).exists() else None


def _run(device, argument):
    binary = shutil.which('v4l2-ctl')
    if not binary:
        raise RuntimeError('未安裝 v4l2-ctl（v4l-utils）')
    result = subprocess.run([binary, '-d', device, argument], capture_output=True,
                            text=True, timeout=8, check=False)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip() or 'V4L2 控制失敗')
    return result.stdout


def parse_controls(output):
    controls = {}
    current = None
    for line in output.splitlines():
        match = CONTROL_LINE.match(line)
        if match:
            name, kind, tail = match.groups()
            current = name if name in LABELS else None
            if current is None:
                continue
            properties = {key: int(value) for key, value in PROPERTY.findall(tail)
                          if key in ('min', 'max', 'step', 'default', 'value')}
            flags = re.search(r'\bflags=([^\s]+)', tail)
            controls[name] = {
                'label': LABELS[name], 'type': kind.lower(),
                'supported': True, 'min': properties.get('min', 0),
                'max': properties.get('max', 1 if kind.lower() == 'bool' else 0),
                'step': properties.get('step', 1),
                'default': properties.get('default'), 'current': properties.get('value'),
                'flags': flags.group(1).split(',') if flags else [], 'choices': [],
            }
            continue
        choice = CHOICE_LINE.match(line)
        if current and choice and controls[current]['type'] == 'menu':
            controls[current]['choices'].append({'value': int(choice.group(1)),
                                                  'label': choice.group(2).strip()})
    return controls


def query(source):
    device = device_path(source)
    if not device:
        return {'available': False, 'device': None, 'properties': {},
                'error': '目前相機來源不是可用的 Linux V4L2 裝置'}
    try:
        properties = parse_controls(_run(device, '--list-ctrls-menus'))
    except (OSError, subprocess.TimeoutExpired, RuntimeError) as exc:
        return {'available': False, 'device': device, 'properties': {}, 'error': str(exc)}
    return {'available': True, 'device': device, 'properties': properties}


def normalize(mode, values):
    if mode not in ('off', 'manual'):
        raise ValueError('相機控制模式無效')
    if isinstance(values, str):
        values = json.loads(values)
    if not isinstance(values, dict) or set(values) - set(LABELS):
        raise ValueError('不支援的相機控制項目')
    clean = {}
    for name, raw in values.items():
        number = int(raw)
        if number != float(raw):
            raise ValueError(f'相機控制值必須是整數：{name}')
        clean[name] = number
    return mode, clean


def validate(source, mode, values):
    mode, values = normalize(mode, values)
    report = query(source)
    if not report['available']:
        raise ValueError(report['error'])
    for name, value in values.items():
        item = report['properties'].get(name)
        if item is None or item['current'] is None:
            raise ValueError(f'設備未提供可讀的控制：{name}')
        if any(flag in item['flags'] for flag in ('disabled', 'read-only', 'grabbed')):
            raise ValueError(f'設備目前不允許修改：{name}')
        if item['type'] == 'menu' and value not in {x['value'] for x in item['choices']}:
            raise ValueError(f'設備不支援此選項：{name}')
        if not item['min'] <= value <= item['max'] or (value - item['min']) % item['step']:
            raise ValueError(f'相機控制值超出設備範圍：{name}')
    return mode, values


def apply(source, mode, values):
    mode, values = validate(source, mode, values)
    report = {'mode': mode, 'applied': {}, 'errors': {}, 'verified': True}
    if mode == 'off':
        return report
    ordered = [name for name in AUTO_FIRST if name in values]
    ordered += [name for name in values if name not in AUTO_FIRST]
    device = device_path(source)
    for name in ordered:
        try:
            _run(device, f'--set-ctrl={name}={values[name]}')
            current = query(source)['properties'].get(name, {}).get('current')
            if current != values[name]:
                raise RuntimeError(f'讀回值 {current} 與設定值 {values[name]} 不同')
            report['applied'][name] = current
        except (OSError, subprocess.TimeoutExpired, RuntimeError) as exc:
            report['errors'][name] = str(exc)
    report['verified'] = not report['errors']
    return report
