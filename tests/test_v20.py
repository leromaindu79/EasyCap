"""v2.0 : origine Cappella 01:00:00:00, 24 i/s CFR sans changement de vitesse."""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from easycap_core import (Cue, CAPPELLA_FPS, DETX_FPS, Settings, Engine,
                          detx_time, frame_time, inspect_media, write_detx)


class TestV20(unittest.TestCase):
    def test_exact_origin_and_24_fps_timing(self):
        self.assertEqual(CAPPELLA_FPS, 24)
        self.assertEqual(DETX_FPS, 24)
        self.assertEqual(detx_time(0), '01:00:00:00')
        self.assertEqual(detx_time(1/24), '01:00:00:01')
        self.assertEqual(detx_time(23/24), '01:00:00:23')
        self.assertEqual(detx_time(1), '01:00:01:00')
        self.assertEqual(detx_time(120), '01:02:00:00')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'Bande_Rythmo.detx'
            write_detx([Cue(0,1,'Bonjour'), Cue(1,1.5,'Salut',track=1)],path)
            root = ET.parse(path).getroot()
            lines = root.findall('body/line')
            self.assertEqual(lines[0].findall('lipsync')[0].get('timecode'),'01:00:00:00')
            self.assertEqual(lines[0].findall('lipsync')[1].get('timecode'),'01:00:01:00')
            self.assertEqual(lines[1].findall('lipsync')[0].get('timecode'),'01:00:01:00')
            self.assertEqual(root.find('header/last_position').get('timecode'),'01:00:00:00')

    def test_gui_has_no_subtitle_framerate_option(self):
        gui = (ROOT / 'easycap_gui.py').read_text(encoding='utf-8')
        for forbidden in ('fps_combo','fps_var', 'fps_detx', 'FPS DETX'):
            self.assertNotIn(forbidden, gui)
        self.assertRegex(gui, r'EasyCap v2\.\d+')
        self.assertIn('01:00:00:00',gui)
        self.assertNotIn('01:00:00:01',gui)
        self.assertNotIn('fps_detx', Settings.__dataclass_fields__)

    def test_mpeg4_both_passes_cfr24_x264_unchanged(self):
        with tempfile.TemporaryDirectory() as folder:
            from easycap_core import Media
            media = Media(Path('/dummy.mkv'), 5, {'index':0, 'avg_frame_rate':'30000/1001'},
                          [{'index':1}], [], 29.97, False)
            settings=Settings(media,0,2,1,1)
            calls=[]
            e=Engine()
            e.ffmpeg=lambda args,*pos,**kw: calls.append(args)
            e._video_pass(settings, Path(folder)/'Cappella_Video.mp4', 'scale=320:180',
                          'mpeg4',1500,'Cappella_Video.mp4',Path(folder))
            self.assertEqual(len(calls),2)
            for n,cmd in enumerate(calls,1):
                self.assertEqual(cmd[cmd.index('-r')+1], '24')
                self.assertEqual(cmd[cmd.index('-fps_mode')+1], 'cfr')
                self.assertIn(',fps=fps=24:round=near',cmd[cmd.index('-vf')+1])
                self.assertEqual(cmd[cmd.index('-pass')+1],str(n))
                self.assertNotIn('setpts',cmd[cmd.index('-vf')+1])
            calls.clear()
            e._video_pass(settings, Path(folder)/'Scène_VO.mp4', 'scale=320:180',
                          'libx264',1500,'Scène_VO.mp4',Path(folder))
            self.assertEqual(len(calls),2)
            self.assertEqual(calls[0][calls[0].index('-r')+1], '30000/1001')
            self.assertNotIn('fps=fps=24',calls[0][calls[0].index('-vf')+1])

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg requis')
    def test_real_conversion_25fps_to24_without_speedup(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder)
            src=base/'source25.mkv'
            subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-y',
                '-f','lavfi','-i','testsrc2=size=160x90:rate=25:duration=2.4',
                '-f','lavfi','-i','sine=frequency=440:duration=2.4',
                '-map','0:v','-map','1:a','-c:v','mpeg4','-q:v','10','-c:a','aac',str(src)],
                check=True, capture_output=True, timeout=40)
            media=inspect_media(src)
            out=base/'Cappella_Video.mp4'
            s=Settings(media, 0, 2.4, media.audios[0]['index'],media.audios[0]['index'])
            e=Engine()
            e._video_pass(s,out, e.video_filter(s,576,None),'mpeg4',1500,
                          'Cappella_Video.mp4',base)
            probe=json.loads(subprocess.run(['ffprobe','-v','error','-show_streams',
               '-show_format','-of','json',str(out)],capture_output=True,text=True,
                check=True,timeout=30).stdout)
            video=next(st for st in probe['streams'] if st['codec_type']=='video')
            self.assertEqual(video['avg_frame_rate'],'24/1')
            self.assertEqual(video['nb_frames'],'58')
            self.assertAlmostEqual(float(video['duration']),2.4,delta=1/24+0.002)

if __name__=='__main__': unittest.main()
