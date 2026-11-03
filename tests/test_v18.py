"""Regression: tailles SRT/VTT uniquement et styles ASS/SSA inchangés."""
import tempfile
import unittest
from pathlib import Path
from easycap_core import (Cue, prepare_ass_original_for_burn, vo_subtitle_font_size,
                          write_ass)


class TestV18(unittest.TestCase):
    def test_fonts_without_upscaling(self):
        for target, source, expected in (
            (1080, 2160, 20), (1080, 1080, 20),
            (1080, 720, 20), (720, 2160, 20),
            (1080, 576, 20), (576, 2160, 20),
            (720, 480, 20), (576, 2160, 20),
        ):
            with self.subTest(target=target, source=source):
                self.assertEqual(vo_subtitle_font_size(target, source), expected)

    def test_style_in_generated_srt_vtt_ass(self):
        for height, font in ((1080, 20), (720, 20), (576, 20)):
            with tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / 'sous_titres.ass'
                write_ass([Cue(0.5, 2, 'Bonjour')], target, style='burn',
                          font_size=font, play_res_y=height)
                data = target.read_text(encoding='utf-8-sig')
                self.assertIn(f'Style: Default,Arial,{font},', data)
                self.assertIn(f'PlayResY: {height}', data)

    def test_ass_ssa_no_size_change(self):
        original = r'''[Script Info]
ScriptType: v4.00+
PlayResY: 1080
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour
Style: Default,Arial,38,&HFFFFFF
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:02.00,0:00:04.00,Default,,0,0,0,,{\fs30}Bonjour
'''
        with tempfile.TemporaryDirectory() as directory:
            source, dest = Path(directory) / 'input.ass', Path(directory) / 'burn.ass'
            source.write_text(original, encoding='utf-8')
            prepare_ass_original_for_burn(source, dest, 1, 5)
            output = dest.read_text(encoding='utf-8-sig')
            self.assertIn('Style: Default,Arial,38,', output)
            self.assertIn(r'{\fs30}', output)
            self.assertIn('0:00:01.00,0:00:03.00', output)


if __name__ == '__main__':
    unittest.main()
