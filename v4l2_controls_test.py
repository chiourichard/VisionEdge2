"""BRIO V4L2 capabilities and control validation without changing a camera."""

from unittest.mock import patch

import v4l2_controls as controls


BRIO = """
User Controls
                     brightness 0x00980900 (int)    : min=0 max=255 step=1 default=128 value=128
        white_balance_automatic 0x0098090c (bool)   : default=1 value=1
           power_line_frequency 0x00980918 (menu)   : min=0 max=2 default=2 value=2 (60 Hz)
                0: Disabled
                1: 50 Hz
                2: 60 Hz
      white_balance_temperature 0x0098091a (int)    : min=2000 max=7500 step=10 default=4000 value=5460 flags=inactive

Camera Controls
                  auto_exposure 0x009a0901 (menu)   : min=0 max=3 default=3 value=3 (Aperture Priority Mode)
                1: Manual Mode
                3: Aperture Priority Mode
                 focus_absolute 0x009a090a (int)    : min=0 max=255 step=5 default=0 value=10 flags=inactive
     focus_automatic_continuous 0x009a090c (bool)   : default=1 value=1
"""


def main():
    found = controls.parse_controls(BRIO)
    assert found['auto_exposure']['current'] == 3
    assert [item['value'] for item in found['auto_exposure']['choices']] == [1, 3]
    assert found['focus_absolute']['step'] == 5
    assert 'inactive' in found['focus_absolute']['flags']
    assert found['white_balance_automatic']['max'] == 1

    with patch.object(controls, 'query', return_value={'available': True, 'properties': found}):
        assert controls.validate('/dev/video0', 'manual', {'auto_exposure': 1})[1] == {'auto_exposure': 1}
        for values in ({'auto_exposure': 2}, {'focus_absolute': 12}, {'unknown': 1}):
            try:
                controls.validate('/dev/video0', 'manual', values)
            except ValueError:
                pass
            else:
                raise AssertionError(f'invalid control accepted: {values}')
    print('V4L2 CONTROLS PASS: BRIO menus, inactive controls, ranges and steps')


if __name__ == '__main__':
    main()
