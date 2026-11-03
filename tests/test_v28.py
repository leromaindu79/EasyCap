"""EasyCap v2.8 : sous-titres SRT/VTT homogènes et retrait de 480p."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from easycap_core import (Settings, Media, Engine, EasyCapError,
                          VOSTFR_TEXT_FONT_SIZE, VOSTFR_TEXT_FONTS,
                          vo_subtitle_font_size, vostfr_text_force_style)


class VostfrV28(unittest.TestCase):
    def test_same_logical_font_at_every_resolution(self):
        self.assertEqual(VOSTFR_TEXT_FONT_SIZE, 20)
        self.assertEqual(VOSTFR_TEXT_FONTS, {1080: 20, 720: 20, 576: 20})
        for selected in (1080, 720, 576):
            for source in (2160, 1080, 720, 576, 480):
                with self.subTest(selected=selected, source=source):
                    self.assertEqual(vo_subtitle_font_size(selected, source), 20)

    def test_thin_border_no_shadow_and_bottom_alignment(self):
        style = vostfr_text_force_style()
        for pair in ('FontSize=20', 'Outline=0.9', 'MarginV=15',
                     'Shadow=0', 'Alignment=2', 'FontName=Arial'):
            self.assertIn(pair, style)
        self.assertNotIn('Outline=2', style)
        self.assertIn('OutlineColour=&H00000000', style)

    @staticmethod
    def media():
        return Media(Path('/film.mkv'), 10,
                     {'index': 0, 'width': 1920, 'height': 1080},
                     [{'index': 1, 'codec_name': 'aac', 'channels': 2}],
                     [{'index': 2, 'codec_name': 'subrip'}], 24, False)

    def test_gui_removed_480p_from_accepted_output(self):
        media = self.media()
        for h in (576, 720, 1080):
            Settings(media, 0, 3, 1, 1, vo_height=h).validate()
        with self.assertRaisesRegex(EasyCapError, 'Résolution'):
            Settings(media, 0, 3, 1, 1, vo_height=480).validate()
        gui = Path(__file__).resolve().parents[1] / 'easycap_gui.py'
        text = gui.read_text(encoding='utf-8')
        self.assertIn("values=['576p','720p','1080p']", text)
        self.assertNotIn("values=['480p'", text)

    def test_srt_vtt_use_same_style_but_ass_original_untouched(self):
        with TemporaryDirectory() as d:
            folder = Path(d)
            media = self.media()
            # mocks arrêtent l'encodage mais laissent _run_impl assembler
            # réellement les chaînes de filtres vidéo.
            for codec, ext in (('subrip', 'srt'), ('webvtt', 'vtt'), ('ass', 'ass')):
                with self.subTest(codec=codec):
                    media.subtitles[0]['codec_name'] = codec
                    subtitle = folder / ('vo_burn.' + ext)
                    subtitle.write_text('test', encoding='utf-8')
                    config = Settings(media, 0, 3, 1, 1,
                                      vo_enabled=True, vo_audio_idx=1,
                                      vo_sub_idx=2, vo_height=720,
                                      optimize_4k=False, output_root=folder)
                    engine = Engine()
                    passes = []
                    with patch.object(engine, 'detect_crop', return_value=None), \
                         patch.object(engine, '_audio'), \
                         patch.object(engine, 'prepare_subtitles'), \
                         patch.object(engine, 'prepare_vo_sub', return_value=subtitle), \
                         patch.object(engine, '_video_pass', side_effect=lambda *a, **k: passes.append(a)):
                        engine._run_impl(config)
                    self.assertEqual(len(passes), 2)
                    vostfr_chain = passes[1][2]
                    if codec == 'ass':
                        self.assertIn('ass=filename=', vostfr_chain)
                        self.assertNotIn('force_style', vostfr_chain)
                    else:
                        self.assertIn('subtitles=filename=', vostfr_chain)
                        self.assertIn('force_style=', vostfr_chain)
                        self.assertIn(vostfr_text_force_style(), vostfr_chain)

    @unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg absent')
    def test_real_ffmpeg_ratio_is_uniform_and_two_lines_remain(self):
        try:
            from PIL import Image
            import numpy as np
        except ImportError:
            self.skipTest('Pillow ou NumPy absent')
        with TemporaryDirectory() as d:
            folder = Path(d)
            subs = folder/'deux_repliques.srt'
            subs.write_text('1\n00:00:00,000 --> 00:00:03,000\n'
                            'way of using magic in order\n'
                            'to be able to express himself.\n', encoding='utf-8')
            ratios = []
            for height in (1080, 720, 576):
                width = round(height * 16/9)//2*2
                out = folder/f'preview_{height}.png'
                vf = (f"subtitles=filename='{subs.as_posix()}':"
                      f"force_style='{vostfr_text_force_style()}'")
                cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
                       '-f', 'lavfi', '-i',
                       f'color=c=black:size={width}x{height}:rate=1:duration=1',
                       '-vf', vf, '-frames:v', '1', str(out)]
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                pixels = np.asarray(Image.open(out).convert('RGB'))
                # Fond noir, lettres blanches ; pas d'OCR ni de police système imposée.
                mask = np.all(pixels > 150, axis=2)
                ys, xs = np.nonzero(mask)
                self.assertTrue(len(ys), 'Les sous-titres doivent apparaître')
                ratio = ((xs.max()-xs.min()+1)/width,
                         (ys.max()-ys.min()+1)/height,
                         (height-ys.max()-1)/height)
                ratios.append(ratio)
                row = np.any(mask, axis=1)
                starts = np.flatnonzero(row & ~np.r_[False, row[:-1]])
                self.assertGreaterEqual(len(starts), 2, 'Deux lignes de texte doivent être visibles')
            for metric in range(3):
                values = [triple[metric] for triple in ratios]
                self.assertLess(max(values)-min(values), 0.006,
                                f'Rendu non homogène pour la métrique {metric}: {ratios}')
            # Vérification d'un texte relativement proche de la capture de référence.
            self.assertLess(max(r[0] for r in ratios), 0.55)
            self.assertLess(max(r[1] for r in ratios), 0.16)


if __name__ == '__main__':
    unittest.main()
