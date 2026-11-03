"""v2.3 : aucun chapitre dans les exports vidéo, y compris après remux VO."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from easycap_core import Engine, Media, Settings, inspect_media


def chapters(path):
    p = subprocess.run(['ffprobe', '-v', 'error', '-show_chapters', '-of', 'json', str(path)],
                       check=True, capture_output=True, text=True)
    return json.loads(p.stdout).get('chapters', [])


class TestNoChapters(unittest.TestCase):
    def test_commands_drop_chapters_at_every_video_output(self):
        media = Media(Path('/film.mkv'), 3., {'index': 0, 'height': 1080,
                'avg_frame_rate': '24/1'}, [{'index': 1, 'channels': 2}], [], 24, False)
        s = Settings(media, 0, 2., 1, 1, vo_enabled=True, vo_audio_idx=1)
        with tempfile.TemporaryDirectory() as folder:
            dest = Path(folder)
            engine = Engine()
            calls = []
            def mock(args, label, *a, **kw):
                calls.append((label, args))
                if 'assemblage MP4' in label:
                    Path(args[-1]).write_bytes(b'final')
                return 0.01
            engine.ffmpeg = mock
            engine._video_pass(s, dest/'Cappella_Video.mp4', 'format=yuv420p',
                               'mpeg4', 1500, 'Cappella_Video.mp4', dest)
            engine._video_pass(s, dest/'Scène_VOSTFR.mp4', 'format=yuv420p',
                               'libx264', 3000, 'Scène_VOSTFR.mp4', dest,
                               audio_idx=1)
            named = {label: args for label, args in calls}
            for label in ('Cappella_Video.mp4 — passe 2/2',
                          'Scène_VOSTFR.mp4 — passe 2/2',
                          'Scène_VOSTFR.mp4 — assemblage MP4 sans réencodage'):
                self.assertEqual(named[label][named[label].index('-map_chapters')+1], '-1')
            self.assertEqual(len(calls), 6)
            self.assertEqual(named['Scène_VOSTFR.mp4 — assemblage MP4 sans réencodage'][
                                 named['Scène_VOSTFR.mp4 — assemblage MP4 sans réencodage'].index('-c:v')+1], 'copy')
            # Le cache FFV1 ne transmet pas de chapitres non plus.
            engine.preprocess_video(s, dest, None, 1080)
            self.assertEqual(calls[-1][1][calls[-1][1].index('-map_chapters')+1], '-1')

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg absent')
    def test_real_mkv_with_chapters_produces_chapterless_mp4(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            metadata = root/'chap.ffmeta'
            metadata.write_text(''';FFMETADATA1
[CHAPTER]
TIMEBASE=1/1000
START=0
END=900
title=Première scène
[CHAPTER]
TIMEBASE=1/1000
START=900
END=1900
title=Deuxième scène
''', encoding='utf-8')
            source = root/'film_chapitre.mkv'
            cmd = ['ffmpeg', '-hide_banner', '-v', 'error', '-y',
                   '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=24',
                   '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000',
                   '-f', 'ffmetadata', '-i', str(metadata),
                   '-map', '0:v:0', '-map', '1:a:0', '-map_metadata', '2',
                   '-t', '2', '-c:v', 'mpeg4', '-q:v', '12', '-c:a', 'aac', str(source)]
            subprocess.run(cmd, check=True, capture_output=True, timeout=25)
            self.assertEqual(len(chapters(source)), 2, 'La source doit réellement contenir des chapitres')
            media = inspect_media(source)
            audio_idx = media.audios[0]['index']
            settings = Settings(media, 0, 1.8, audio_idx, audio_idx,
                                vo_enabled=True, vo_audio_idx=audio_idx,
                                output_root=root)
            engine = Engine()
            out1, out2 = root/'Cappella_Video.mp4', root/'Scène_VOSTFR.mp4'
            engine._video_pass(settings, out1, 'format=yuv420p', 'mpeg4', 1500,
                               out1.name, root)
            engine._video_pass(settings, out2, 'format=yuv420p', 'libx264', 1500,
                               out2.name, root, audio_idx=audio_idx)
            for output in (out1, out2):
                self.assertTrue(output.is_file() and output.stat().st_size > 0)
                self.assertEqual(chapters(output), [], output.name)
            st = json.loads(subprocess.run(['ffprobe','-v','error','-show_streams',
                '-of','json',str(out2)],check=True,capture_output=True,text=True).stdout)['streams']
            self.assertEqual({x['codec_type'] for x in st}, {'video','audio'})


if __name__ == '__main__':
    unittest.main()
