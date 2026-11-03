"""EasyCap v1.7: second-pass x264/TrueHD separation + watchdog policy."""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from easycap_core import Engine, Settings, Media, inspect_media, video_stall_timeout
import test_v14


class TestV17(unittest.TestCase):
    def test_watchdog_no_longer_uses_fixed_120_second_timeout(self):
        self.assertEqual(video_stall_timeout(), 600.)
        self.assertEqual(video_stall_timeout(233.2), 600.)
        self.assertEqual(video_stall_timeout(600.), 1500.)
        self.assertEqual(video_stall_timeout(3000.), 3600.)

    def test_audio_not_part_of_x264_pass_two_and_mux_copies_streams(self):
        media = Media(Path('/film.mkv'), 1000, {
            'index': 0, 'height': 2160, 'avg_frame_rate': '24000/1001'
        }, [{'index': 3, 'codec_name': 'truehd', 'channels': 8}], [], 23.976, False)
        st = Settings(media, 100., 105., 3, 3, vo_enabled=True, vo_audio_idx=3)
        calls = []
        en = Engine()
        def fake(args, label, seconds=0, progress_span=(0,0), stall_timeout=None):
            calls.append((args, label, stall_timeout))
            if 'assemblage MP4' in label:
                Path(args[-1]).write_bytes(b'fake')
            if 'passe 1/2' in label:
                return 233.2
            return 0.2
        en.ffmpeg = fake
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / 'Scène_VO.mp4'
            en._video_pass(st, dest, 'format=yuv420p', 'libx264', 3000,
                           'Scène_VO.mp4', Path(tmp), audio_idx=3)
        self.assertEqual(len(calls), 4)
        first, second, audio, mux = [x[0] for x in calls]
        for number, args in enumerate((first, second), 1):
            self.assertIn('-an', args)
            self.assertIn('-sn', args)
            self.assertEqual(args[args.index('-pass') + 1], str(number))
            self.assertEqual(args[args.index('-preset') + 1], 'medium')
            self.assertNotIn('-c:a', args)
            self.assertNotIn('0:3', args)
        self.assertEqual(calls[1][2], 600.)
        self.assertEqual(audio[audio.index('-map')+1], '0:3')
        self.assertEqual(audio[audio.index('-c:a')+1], 'aac')
        self.assertEqual(mux[mux.index('-c:a')+1], 'copy')
        self.assertEqual(mux[mux.index('-c:v')+1], 'copy')
        self.assertIn('-shortest', mux)

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg absent')
    def test_real_truehd_srt_and_pgs_full_scene_exports(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            def ff(*args):
                subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
                               *map(str, args)], capture_output=True, check=True, timeout=40)
            src = root / 'truehd.mkv'
            ff('-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=24:duration=2.4',
               '-f', 'lavfi', '-i', 'anullsrc=channel_layout=5.1:sample_rate=48000',
               '-t', '2.4', '-map', '0:v', '-map', '1:a', '-c:v', 'libx264',
               '-preset', 'ultrafast', '-c:a', 'truehd', '-strict', '-2', src)
            srt = root/'test.srt'
            srt.write_text('1\n00:00:00,250 --> 00:00:01,600\nBonjour\n', encoding='utf-8')
            pgs = root / 'test.sup'
            pgs.write_bytes(test_v14.TestV14.generate_sup())
            for kind in ('srt', 'pgs'):
                output = root / f'scene_{kind}.mkv'
                ff('-i', src, '-f', kind if kind == 'srt' else 'sup',
                   '-i', srt if kind == 'srt' else pgs,
                   '-map', '0', '-map', '1:s', '-c', 'copy', output)
                media = inspect_media(output)
                self.assertEqual(media.audios[0]['codec_name'], 'truehd')
                self.assertEqual(media.subtitles[0]['codec_name'],
                                 'subrip' if kind == 'srt' else 'hdmv_pgs_subtitle')
                settings = Settings(media, 0, 2.2, 1, 1, vo_enabled=True,
                                    vo_audio_idx=1, vo_sub_idx=2,
                                    convert_detx=(kind == 'srt'),
                                    detx_sub_idx=2 if kind == 'srt' else None,
                                    output_root=root/'out', optimize_4k=False)
                logs = []
                final = Engine(log=logs.append).run(settings)
                scene = final/'Scène_VOSTFR.mp4'
                self.assertTrue(scene.is_file())
                probe = subprocess.check_output(['ffprobe', '-v', 'error',
                    '-show_entries', 'stream=codec_name,channels',
                    '-of', 'csv=p=0', str(scene)], text=True)
                self.assertIn('h264', probe)
                self.assertIn('aac,2', probe)
                self.assertTrue(any('audio AAC indépendant' in l for l in logs))
                self.assertTrue(any('assemblage MP4 sans réencodage' in l for l in logs))
                self.assertTrue((final/'Cappella_Video.mp4').exists())
                if kind == 'srt':
                    self.assertTrue((final/'Bande_Rythmo.detx').exists())


if __name__ == '__main__':
    unittest.main()
