import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from easycap_core import (Cue, clean_caption, parse_srt, parse_ass, organize_cues,
                          write_detx, frame_time, source_depth, time_to_seconds,
                          audio_extension, pan_filter, Engine, Media, Settings, EasyCapError,
                          write_ass, prepare_ass_original_for_burn, vo_bitrate_kbps)


class TestEasyCap(unittest.TestCase):
    def test_times(self):
        self.assertAlmostEqual(time_to_seconds('00:01:46.727'), 106.727)
        self.assertEqual(frame_time(106.727,23.976), '00:01:46:17')
        self.assertEqual(frame_time(133.415,25), '00:02:13:10')

    def test_clean(self):
        self.assertEqual(clean_caption('<i>Bonjour</i>\n<font color="#FFFFFF">toi</font> {\\an8}'), 'Bonjour toi')

    def test_srt_detx_model(self):
        source=Path(__file__).resolve().parents[1] / 'exemples' / 'BR.srt'
        cues=parse_srt(source.read_text(encoding='utf-8-sig'))
        self.assertEqual(len(cues),1164)
        prepared=organize_cues(cues, 0, 500)
        with tempfile.TemporaryDirectory() as d:
            out=Path(d)/'BR.detx'
            write_detx(prepared,out,25)
            tree=ET.parse(out).getroot()
            self.assertEqual(tree.tag,'detx')
            self.assertEqual(tree.find('header/cappella').attrib['version'],'3.7.0')
            lines=tree.findall('body/line')
            self.assertEqual(lines[0].find('text').text,"It's time.")
            self.assertEqual(lines[0].find('lipsync').get('type'),'in_open')

    def test_ass_styles_overlap(self):
        s='''[Script Info]\nScriptType: v4.00+\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:01.00,0:00:03.00,PersonA,,0,0,0,,Bonjour\\Ntoi
Dialogue: 0,0:00:01.00,0:00:03.00,PersonA,,0,0,0,,suite
Dialogue: 0,0:00:02.00,0:00:04.00,PersonB,,0,0,0,,Autre
Dialogue: 0,0:00:03.00,0:00:04.00,PersonA,,0,0,0,,A
'''
        cues=organize_cues(parse_ass(s),0,10)
        self.assertEqual(len(cues),3)
        self.assertIn('Bonjour toi suite', cues[0].text)
        self.assertEqual(cues[0].track,0)
        self.assertEqual(cues[1].track,1)
        self.assertNotEqual(cues[2].track,cues[1].track)

    def test_detx_one_hour_without_extra_frame_on_all_timecodes(self):
        # 25 fps : départ exact à 01:00:00:00, image 24 avant seconde suivante.
        cues = [Cue(0, 0.96, 'Au début', track=0),
                Cue(59.96, 60.04, 'Après une minute', track=1)]
        with tempfile.TemporaryDirectory() as d:
            output = Path(d) / 'Bande_Rythmo.detx'
            write_detx(cues, output, 25)
            root = ET.parse(output).getroot()
            lines = root.findall('body/line')
            self.assertEqual(lines[0].findall('lipsync')[0].get('timecode'), '01:00:00:00')
            self.assertEqual(lines[0].findall('lipsync')[1].get('timecode'), '01:00:00:24')
            self.assertEqual(lines[1].findall('lipsync')[0].get('timecode'), '01:00:59:24')
            self.assertEqual(lines[1].get('track'), '1')
            self.assertEqual(root.find('header/last_position').get('timecode'),
                             '01:00:00:00')

    def test_detx_fractional_framerate_rollover(self):
        # 23.976 i/s, image 23 suivie de l'image 00 à la seconde suivante.
        self.assertEqual(frame_time(0, 23.976, offset_frames=1), '00:00:00:01')
        self.assertEqual(frame_time(.96, 23.976, offset_frames=1), '00:00:01:00')
        self.assertEqual(frame_time(59.96, 23.976, offset_frames=1), '00:01:00:00')
        self.assertEqual(frame_time(0.96663, 29.97, offset_frames=1), '00:00:01:00')
        # Le moteur de formatage conserve son comportement par défaut.
        self.assertEqual(frame_time(0, 25), '00:00:00:00')

    def test_x264_medium_two_passes(self):
        stream = {'index': 0, 'height': 1080}
        media = Media(Path('/faux.mkv'), 5.0, stream, [{'index': 1}], [], 25, False)
        settings = Settings(media, 0, 2, 1, 1, vo_audio_idx=1)
        logs, calls, labels = [], [], []
        engine = Engine(log=logs.append)
        engine.ffmpeg = lambda args, label, duration, progress, stall_timeout=None: (calls.append(args), labels.append(label), Path(args[-1]).write_bytes(b'fake') if 'assemblage MP4' in label else None)
        with tempfile.TemporaryDirectory() as d:
            engine._video_pass(settings, Path(d) / 'Scène_VOSTFR.mp4', 'format=yuv420p',
                               'libx264', 3000, 'Scène_VOSTFR.mp4', Path(d), audio_idx=1)
        self.assertEqual(len(calls), 4)
        for n, cmd in enumerate(calls[:2], 1):
            self.assertEqual(cmd[cmd.index('-preset')+1], 'medium')
            self.assertEqual(cmd[cmd.index('-b:v')+1], '3000k')
            self.assertEqual(cmd[cmd.index('-pass')+1], str(n))
            self.assertIn('-passlogfile', cmd)
            self.assertIn('passe '+str(n)+'/2', labels[n-1])
        self.assertEqual(calls[0][calls[0].index('-f')+1], 'null')
        self.assertIn('-an', calls[0])
        self.assertNotIn('-c:a', calls[0])
        self.assertIn('-an', calls[1])  # Vidéo seule, aussi à la passe 2.
        self.assertEqual(calls[2][calls[2].index('-c:a')+1], 'aac')
        self.assertEqual(calls[2][calls[2].index('-b:a')+1], '192k')
        self.assertEqual(calls[3][calls[3].index('-c:v')+1], 'copy')
        self.assertEqual(calls[3][calls[3].index('-c:a')+1], 'copy')
        self.assertNotIn('-f', calls[1])
        self.assertTrue(any('2 passes' in message for message in logs))

    def test_vo_rates_without_upscale(self):
        self.assertEqual(vo_bitrate_kbps(1080,2160),3000)
        self.assertEqual(vo_bitrate_kbps(1080,1080),3000)
        self.assertEqual(vo_bitrate_kbps(1080,720),2000)
        self.assertEqual(vo_bitrate_kbps(720,2160),2000)
        self.assertEqual(vo_bitrate_kbps(720,576),1500)
        self.assertEqual(vo_bitrate_kbps(576,2160),1500)
        self.assertEqual(vo_bitrate_kbps(1080,480),1500)

    def test_bitmap_subtitles_supported_or_rejected_appropriately(self):
        stream = {'index': 0, 'height': 1080}
        for codec in ('hdmv_pgs_subtitle', 'dvd_subtitle', 'dvb_subtitle', 'xsub'):
            media = Media(Path('/faux.mkv'), 5.0, stream, [{'index': 1}],
                          [{'index': 2, 'codec_name': codec}], 25, False)
            settings = Settings(media, 0, 2, 1, 1, vo_enabled=True,
                                vo_audio_idx=1, vo_sub_idx=2)
            if codec in ('hdmv_pgs_subtitle', 'dvd_subtitle'):
                settings.validate()
            else:
                with self.assertRaises(EasyCapError):
                    settings.validate()
            settings.vo_sub_idx = None
            settings.validate()

    def test_vostfr_font_1080(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'vo_burn.ass'
            write_ass([Cue(0,1,'Bonjour')], path, 'burn')
            data = path.read_text(encoding='utf-8-sig')
            self.assertIn('PlayResY: 1080', data)
            self.assertIn('Style: Default,Arial,20,', data)

    def test_ass_style_and_inline_font_are_preserved(self):
        original = r'''[Script Info]
ScriptType: v4.00+
PlayResY: 1080
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour
Style: Default,Arial,36,&HFFFFFF
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,{\fs28}Bonjour {\fscx110}toi
'''
        with tempfile.TemporaryDirectory() as d:
            inp, out = Path(d)/'in.ass', Path(d)/'burn.ass'
            inp.write_text(original, encoding='utf-8')
            prepare_ass_original_for_burn(inp, out, 1, 4)
            data = out.read_text(encoding='utf-8-sig')
            self.assertIn('Style: Default,Arial,36,', data)
            self.assertIn(r'{\fs28}', data)
            self.assertIn(r'\fscx110', data)
            self.assertIn('0:00:00.00,0:00:02.00', data)

    def test_mpeg4_still_two_passes(self):
        stream = {'index': 0}
        media = Media(Path('/faux.mkv'), 5.0, stream, [{'index': 1}], [], 25, False)
        settings = Settings(media, 0, 2, 1, 1)
        calls = []
        engine = Engine()
        engine.ffmpeg = lambda args, label, duration, progress, stall_timeout=None: calls.append(args)
        with tempfile.TemporaryDirectory() as d:
            engine._video_pass(settings, Path(d) / 'Cappella_Video.mp4', 'format=yuv420p',
                               'mpeg4', 3000, 'Cappella_Video.mp4', Path(d))
        self.assertEqual(len(calls), 2)
        for n, cmd in enumerate(calls, 1):
            self.assertEqual(cmd[cmd.index('-c:v')+1], 'mpeg4')
            self.assertEqual(cmd[cmd.index('-pass')+1], str(n))
            self.assertIn('-passlogfile', cmd)
            self.assertNotIn('-preset', cmd)
        self.assertEqual(calls[0][calls[0].index('-f')+1], 'null')
        self.assertNotIn('-f', calls[1])

        self.assertIn('c2=0*c2',pan_filter(6))
        self.assertIn('0.5*c0-0.5*c1',pan_filter(2))

if __name__=='__main__':
    unittest.main()
