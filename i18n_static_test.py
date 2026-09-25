"""Dependency-free checks for VisionEdge 2.2.1 multilingual UI."""
from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
I18N = STATIC / "visionedge_i18n.js"
PAGES = [STATIC / "edge_dashboard.html", STATIC / "flow_studio.html", STATIC / "template_workspace.html", STATIC / "camera_settings.html"]


class VisibleChinese(HTMLParser):
    def __init__(self):
        super().__init__()
        self.skip = 0
        self.values: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        if not self.skip:
            for key, value in attrs:
                if key in ("placeholder", "title", "aria-label", "alt") and value and re.search(r"[\u4e00-\u9fff]", value):
                    self.values.append(" ".join(value.split()))

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if self.skip:
            return
        value = " ".join(data.split())
        if value and re.search(r"[\u4e00-\u9fff]", value):
            self.values.append(value)


def translation_dictionary(js: str) -> dict:
    exact_match = re.search(r"const EXACT=(\{.*?\});\nconst PATTERNS=", js, re.S)
    extra_match = re.search(r"const EXTRA=(\{.*?\});\nconst EXTRA_PATTERNS=", js, re.S)
    assert exact_match, "EXACT dictionary not found"
    assert extra_match, "EXTRA dictionary not found"
    out = json.loads(exact_match.group(1))
    out.update(json.loads(extra_match.group(1)))
    return out


def main():
    js = I18N.read_text(encoding="utf-8")
    exact = translation_dictionary(js)
    assert "visionedge.language" in js
    assert "MutationObserver" in js
    assert "localStorage.setItem" in js
    for code in ("zh-Hant", "zh-Hans", "en", "es"):
        assert code in js, code

    ignored = {"繁體中文", "简体中文", "Language / 語言"}
    for page in PAGES:
        html = page.read_text(encoding="utf-8")
        assert "/static/visionedge_i18n.js?v=" in html, page.name
        assert "data-i18n-language" in html, page.name
        parser = VisibleChinese()
        parser.feed(html)
        missing = sorted({v for v in parser.values if v not in exact and v not in ignored})
        assert not missing, f"{page.name} untranslated visible strings: {missing}"

    # Dynamic snapshot/history strings live in inspection.js rather than HTML.
    inspection = (STATIC / "inspection.js").read_text(encoding="utf-8")
    dynamic_strings = [
        "不合格", "待確認 · 請調整後重拍", "請掃描序號，再按拍照判定",
        "正在判定與保存…", "重新拍照判定",
        "目前產品已鎖定。重拍會保留每次紀錄；完成後按「結束此件」。",
        "無法讀取檢測狀態，請重新整理後再操作。", "檢測失敗",
        "已保存檢測紀錄", "未填序號", "原圖", "結果", "明細",
        "還沒有拍照判定紀錄。完成第一次檢測後會出現在這裡。",
    ]
    for value in dynamic_strings:
        assert value in inspection, value
        assert value in exact, f"inspection.js untranslated dynamic string: {value}"
    assert r'^第 (\\d+) 頁$' in js, "history page number translation pattern missing"
    assert "data-i18n-skip" in js and "data-i18n-skip" in inspection

    # No account/login gate is reintroduced by the multilingual work.
    runtime_text = (ROOT / "visionedge_server.py").read_text(encoding="utf-8") + (ROOT / "server.py").read_text(encoding="utf-8")
    assert "/api/backend-login" not in runtime_text
    assert "admin / 1234" not in runtime_text
    assert "admin:1234" not in runtime_text

    print("I18N_STATIC_TEST PASS: 4 languages, visible-string coverage, persistence, no login")


if __name__ == "__main__":
    main()

