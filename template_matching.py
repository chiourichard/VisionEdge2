"""CPU/CUDA template matching with one grayscale upload per inspected frame."""

from __future__ import annotations

import cv2
import numpy as np


class TemplateMatcher:
    def __init__(self, device: str = 'cpu', method: int = cv2.TM_CCOEFF_NORMED):
        self.device = str(device).lower()
        self.method = method
        self._templates = {}
        self._scaled_templates = {}
        self._cuda_matcher = None
        if self.device not in ('cpu', 'cuda'):
            raise ValueError(f'unsupported inference device: {device}')
        if self.device == 'cuda':
            cuda = getattr(cv2, 'cuda', None)
            if (cuda is None or not hasattr(cuda, 'createTemplateMatching')
                    or not hasattr(cuda, 'minMaxLoc')
                    or cuda.getCudaEnabledDeviceCount() < 1):
                raise RuntimeError('CUDA template matching is unavailable in this OpenCV build')
            self._cuda_matcher = cuda.createTemplateMatching(cv2.CV_8UC1, method)

    def prepare(self, templates) -> None:
        if self.device == 'cuda':
            for template in templates:
                self._gpu_template(template)

    def _gpu_template(self, template: np.ndarray):
        key = id(template)
        cached = self._templates.get(key)
        if cached is not None and cached[0] is template:
            return cached[1]
        gpu = cv2.cuda_GpuMat()
        gpu.upload(template)
        self._templates[key] = (template, gpu)
        return gpu

    def _template_at_size(self, template: np.ndarray, width: int, height: int) -> np.ndarray:
        if template.shape[:2] == (height, width):
            return template
        key = (id(template), width, height)
        cached = self._scaled_templates.get(key)
        if cached is not None and cached[0] is template:
            return cached[1]
        interpolation = cv2.INTER_AREA if width < template.shape[1] or height < template.shape[0] else cv2.INTER_LINEAR
        scaled = cv2.resize(template, (width, height), interpolation=interpolation)
        self._scaled_templates[key] = (template, scaled)
        return scaled

    def begin(self, frame: np.ndarray) -> 'MatchFrame':
        return self.begin_gray(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))

    def begin_gray(self, gray: np.ndarray) -> 'MatchFrame':
        return MatchFrame(self, gray)


class MatchFrame:
    def __init__(self, matcher: TemplateMatcher, gray: np.ndarray):
        self.matcher = matcher
        self.gray = gray
        self.gpu_gray = None
        if matcher.device == 'cuda':
            self.gpu_gray = cv2.cuda_GpuMat()
            self.gpu_gray.upload(gray)

    def match(self, reg: dict, small_roi_fallback: bool = False) -> dict:
        sh, sw = self.gray.shape[:2]
        label = reg['label']
        threshold = reg['threshold']
        x, y, w, h = reg['x'], reg['y'], reg['w'], reg['h']
        margin = reg.get('search_margin') or 0
        source_w = int(reg.get('source_width') or 0)
        source_h = int(reg.get('source_height') or 0)
        if source_w > 0 and source_h > 0:
            ratio_error = abs(sw * source_h - sh * source_w) / max(sw * source_h, sh * source_w)
            if ratio_error > .01:
                return {'id': reg['id'], 'label': label, 'threshold': threshold,
                        'score': None, 'pass': False, 'error': '相機與樣板來源長寬比不同，請重新標註'}
            scale_x, scale_y = sw / source_w, sh / source_h
            x, y = round(x * scale_x), round(y * scale_y)
            w = max(1, round((reg['x'] + w) * scale_x) - x)
            h = max(1, round((reg['y'] + h) * scale_y) - y)
            margin = round(margin * (scale_x + scale_y) / 2)
            tw = max(1, round(reg['tw'] * scale_x))
            th = max(1, round(reg['th'] * scale_y))
            if reg['tw'] == reg['w']:
                tw = w
            if reg['th'] == reg['h']:
                th = h
        else:
            tw, th = reg['tw'], reg['th']
        base = {'id': reg['id'], 'label': label, 'threshold': threshold,
                'search_margin': margin, 'x': x, 'y': y, 'w': w, 'h': h}
        if x < 0 or y < 0 or x + w > sw or y + h > sh:
            return {**base, 'score': None, 'pass': False,
                    'error': '樣板框超出目前影像；請確認原始標註解析度'}
        if th > sh or tw > sw:
            return {**base, 'score': None, 'pass': False, 'error': '樣板大於影像'}

        if margin > 0:
            x1, y1 = max(0, x - margin), max(0, y - margin)
            x2, y2 = min(sw, x + w + margin), min(sh, y + h + margin)
        else:
            x1, y1, x2, y2 = 0, 0, sw, sh
        if y2 - y1 < th or x2 - x1 < tw:
            if not small_roi_fallback:
                return {**base, 'score': None, 'pass': False,
                        'error': '限定搜尋區域小於樣板，請重新設定位置'}
            x1, y1, x2, y2 = 0, 0, sw, sh

        template = self.matcher._template_at_size(reg['tpl_gray'], tw, th)
        if self.matcher.device == 'cuda':
            roi = self.gpu_gray.rowRange(y1, y2).colRange(x1, x2)
            result_map = self.matcher._cuda_matcher.match(
                roi, self.matcher._gpu_template(template))
            minimum, maximum, min_loc, max_loc = cv2.cuda.minMaxLoc(result_map)
        else:
            roi = self.gray[y1:y2, x1:x2]
            result_map = cv2.matchTemplate(roi, template, self.matcher.method)
            minimum, maximum, min_loc, max_loc = cv2.minMaxLoc(result_map)
        if self.matcher.method == cv2.TM_SQDIFF_NORMED:
            score, loc = 1.0 - float(minimum), min_loc
        else:
            score, loc = float(maximum), max_loc
        top_left = [loc[0] + x1, loc[1] + y1]
        return {**base, 'score': round(score, 4), 'pass': score >= threshold,
                'match_loc': top_left, 'match_size': [tw, th]}
