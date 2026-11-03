"""Régressions v1.6 : extraction SRT/ASS bornée et réutilisée."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from easycap_core import (Engine, Settings, Media, subtitle_window, inspect_media,
                          extract_text)


class TestV16(unittest.TestCase):
    def settings(self, start=100, end=108):
        media = Media(Path('/film_4k.mkv'), 7200, {'index': 0, 'height': 2160},
                      [{'index': 1}], [{'index': 7, 'codec_name': 'subrip'}], 24, False)
        return Settings(media, start, end, 1, 1, convert_detx=True, detx_sub_idx=7,
                        vo_enabled=True, vo_audio_idx=1, vo_sub_idx=7)

    def test_window_limits_reads_and_preserves_preroll(self):
        self.assertEqual(subtitle_window(self.settings()), (40., 68.5, 60., 68.))
        self.assertEqual(subtitle_window(self.settings(0, 120)), (0., 120.5, 0., 120.))

    def test_extract_reads_only_scene_and_not_full_movie(self):
        s = self.settings()
        engine = Engine()
        calls = []
        def fake(args, label, seconds=0, progress_span=(0,0)):
            calls.append((args, label, seconds))
            Path(args[-1]).write_text('1\n00:01:00,000 --> 00:01:01,000\nSalut\n', encoding='utf-8')
        engine.ffmpeg = fake
        with tempfile.TemporaryDirectory() as d:
            raw, codec, extension = engine.extract_sub(s,7,Path(d),'detx')
            second = engine.extract_sub(s,7,Path(d),'vo')
            self.assertEqual((raw, codec, extension), second)
            self.assertEqual(len(calls),1)
            cmd,label,seconds = calls[0]
            self.assertEqual(cmd[:6], ['-ss','40.000','-t','68.500','-i','/film_4k.mkv'])
            self.assertEqual(cmd[cmd.index('-map')+1],'0:7')
            self.assertIn('-vn',cmd)
            self.assertEqual(seconds,68.5)

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg absent')
    def test_srt_detx_and_vostfr_with_seek_and_preroll(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            srt=root/'src.srt'
            srt.write_text('1\n00:01:38,000 --> 00:01:41,000\nUn dialogue commencé avant\n\n'
                           '2\n00:01:42,000 --> 00:01:44,000\nDans la scène\n\n'
                           '3\n00:01:50,000 --> 00:01:52,000\nAprès la scène\n',encoding='utf-8')
            media=root/'src.mkv'
            cmd=['ffmpeg','-hide_banner','-loglevel','error','-y',
                 '-f','lavfi','-i','color=c=black:size=160x90:rate=2:duration=115',
                 '-f','lavfi','-i','sine=sample_rate=8000:duration=115',
                 '-i',str(srt),'-map','0:v','-map','1:a','-map','2:s',
                 '-c:v','mpeg4','-q:v','8','-c:a','aac','-c:s','srt',str(media)]
            subprocess.run(cmd,check=True,timeout=30,capture_output=True)
            m=inspect_media(media)
            options=Settings(m,100,108,1,1,vo_enabled=True,vo_audio_idx=1,
                             vo_sub_idx=2,convert_detx=True,detx_sub_idx=2,vo_height=576)
            eng=Engine()
            eng.prepare_subtitles(options,root,root)
            eng.prepare_vo_sub(options,root)
            cues=extract_text(root/'Bande_Rythmo.srt','subrip')
            self.assertEqual(len(cues),2)
            self.assertEqual(cues[0].text,'Un dialogue commencé avant')
            self.assertAlmostEqual(cues[0].start,0,delta=.05)
            self.assertAlmostEqual(cues[1].start,2,delta=.3)
            self.assertTrue((root/'Bande_Rythmo.detx').exists())
            self.assertEqual(len(eng._subtitle_cache),1)
            self.assertTrue((root/'vo_burn.srt').exists())

if __name__=='__main__': unittest.main()
