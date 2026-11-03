"""EasyCap v2.2 : interface simplifiée et profils audio stéréo fixes."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from easycap_core import Engine, Media, Settings, STEREO_PRESETS, pan_filter, stereo_voice_filter


class TestV22(unittest.TestCase):
    def setUp(self):
        self.media = Media(Path('/un_film.mkv'), 120.0, {'index': 0, 'height': 1080}, [
            {'index': 1, 'channels': 2, 'sample_rate': '48000',
             'codec_name': 'flac', 'bits_per_raw_sample': '16'},
            {'index': 2, 'channels': 6, 'channel_layout': '5.1', 'codec_name': 'ac3'},
        ], [], 24.0, False)

    def test_exactly_four_profiles_with_standard_as_default(self):
        self.assertEqual([p['label'] for p in STEREO_PRESETS.values()], [
            'Standard', "Intérieur d'une pièce",
            "Intérieur d'une pièce + Réverbération", 'Souvenirs / Voix intérieurs'])
        s = Settings(self.media, 1, 3, 1, 1)
        self.assertEqual(s.stereo_preset, 'standard')
        s.validate()
        self.assertEqual(stereo_voice_filter(s.stereo_preset), pan_filter(2))

    def test_no_user_strength_setting_and_gui_has_no_unwanted_controls(self):
        self.assertNotIn('stereo_strength', Settings.__dataclass_fields__)
        gui = (ROOT / 'easycap_gui.py').read_text(encoding='utf8')
        for forbidden in ('stereo_slider', 'stereo_strength_var',
                          'optim'+'ize_4k_var', 'cache_chk', 'tk.Scale(',
                          'Optimiser le traitement 4K/HDR avec'):
            self.assertNotIn(forbidden, gui)
        self.assertIn('STEREO_PRESETS[\'standard\']', gui)
        self.assertIn('STEREO_PRESETS.items()', gui)

    def test_cache_still_automatic_in_engine(self):
        s = Settings(self.media, 1, 3, 1, 1, vo_enabled=True, vo_audio_idx=1)
        self.assertTrue(s.optimize_4k)
        self.assertIn('s.optimize_4k and s.vo_enabled',
                      (ROOT/'easycap_core.py').read_text(encoding='utf8'))

    def test_each_preset_wires_real_filter_and_multichannel_is_untouched(self):
        engine = Engine()
        captured = []
        engine.ffmpeg = lambda args, label, *a, **kw: captured.append((label, args))
        for name in STEREO_PRESETS:
            s = Settings(self.media, 0, 1, 1, 1, stereo_preset=name)
            engine._audio(s, Path('/tmp'))
            a = [a for label, a in captured if label=='Sans_Voix.flac'][-1]
            self.assertEqual(a[a.index('-af')+1], stereo_voice_filter(name))
        captured.clear()
        s = Settings(self.media, 0, 1, 1, 2)
        engine._audio(s, Path('/tmp'))
        a = [a for label, a in captured if label=='Sans_Voix.ac3'][0]
        self.assertEqual(a[a.index('-af')+1], pan_filter(6, '5.1'))


if __name__ == '__main__':
    unittest.main()
