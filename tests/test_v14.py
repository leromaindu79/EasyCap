"""Régressions EasyCap 1.4 : tailles, cache, PGS/VobSub graphique."""
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from easycap_core import (Engine, Settings, Media, Cue, write_ass, vo_subtitle_font_size,
                          inspect_media)


class TestV14(unittest.TestCase):
    def test_font_profiles(self):
        for chosen, source, expected in ((1080,2160,20),(1080,720,20),
                                         (720,2160,20),(720,480,20),
                                         (576,2160,20)):
            self.assertEqual(vo_subtitle_font_size(chosen,source), expected)
        for font, height in ((20,1080),(20,720),(20,576)):
            with tempfile.TemporaryDirectory() as d:
                path=Path(d)/'burn.ass'
                write_ass([Cue(0,1,'Salut')],path,'burn',font_size=font,play_res_y=height)
                content=path.read_text(encoding='utf-8-sig')
                self.assertIn('PlayResY: '+str(height),content)
                self.assertIn(f'Style: Default,Arial,{font},',content)

    def test_cache_source_video_vs_original_audio_mapping(self):
        media=Media(Path('/hypothetique.mkv'),8,{'index':0,'height':2160,
                    'avg_frame_rate':'24000/1001'},[{'index':1}],[],23.976,False)
        s=Settings(media,1,4,1,1,vo_audio_idx=1)
        engine=Engine()
        calls=[]
        engine.ffmpeg=lambda args,label,duration,rng,stall_timeout=None:(calls.append(args), Path(args[-1]).write_bytes(b'fake') if 'assemblage MP4' in label else None)
        with tempfile.TemporaryDirectory() as d:
            engine._video_pass(s, Path(d)/'scene.mp4','format=yuv420p',
                               'libx264',3000,'scene',Path(d),audio_idx=1,
                               video_source=Path(d)/'cache.mkv')
        self.assertEqual(len(calls),4)
        for cmd in calls[:2]:
            self.assertIn('cache.mkv',' '.join(cmd))
            self.assertNotIn('/hypothetique.mkv',' '.join(cmd))
            self.assertEqual(cmd[cmd.index('-r')+1],'24000/1001')
        self.assertIn('0:1',calls[2])  # Extraction AAC depuis le fichier original.
        self.assertEqual(calls[1][calls[1].index('-pass')+1],'2')

    @staticmethod
    def generate_sup() -> bytes:
        u16=lambda x:struct.pack('>H',x)
        u32=lambda x:struct.pack('>I',x)
        def segment(t,payload,ts):
            return b'PG'+u32(ts)+u32(ts)+bytes([t])+u16(len(payload))+payload
        def pcs(idx, visible):
            return (u16(640)+u16(360)+b'\x10'+u16(idx)+b'\0\0\0'+bytes([int(visible)])+
                    (u16(0)+b'\0\0'+u16(190)+u16(296) if visible else b''))
        wds=b'\x01\x00'+u16(0)+u16(0)+u16(640)+u16(360)
        pds=b'\0\0'+b'\0\x10\x80\x80\0'+b'\x01\xeb\x80\x80\xff'
        rle=bytes([0,0xC0,250,1,0,0])*36
        ods=u16(0)+b'\0\xc0'+(len(rle)+4).to_bytes(3,'big')+u16(250)+u16(36)+rle
        data=(segment(0x16,pcs(1,True),45000)+segment(0x17,wds,45000)+
              segment(0x14,pds,45000)+segment(0x15,ods,45000)+segment(0x80,b'',45000))
        return data+segment(0x16,pcs(2,False),144000)+segment(0x80,b'',144000)

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg absent')
    def test_graphic_subtitle_alpha_bbox_and_two_pass_export(self):
        with tempfile.TemporaryDirectory() as td:
            d=Path(td)
            (d/'subs.sup').write_bytes(self.generate_sup())
            def ff(*args):
                subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-y',*map(str,args)],
                               capture_output=True, check=True, timeout=30)
            src=d/'src.mp4';pgs=d/'pgs.mkv';vob=d/'vob.mkv'
            ff('-f','lavfi','-i','testsrc2=size=640x360:rate=24:duration=2',
               '-f','lavfi','-i','sine=frequency=440:sample_rate=48000:duration=2',
               '-c:v','libx264','-preset','ultrafast','-c:a','aac',src)
            ff('-i',src,'-f','sup','-i',d/'subs.sup','-map','0','-map','1:s','-c','copy',pgs)
            ff('-i',pgs,'-map','0','-c','copy','-c:s','dvdsub',vob)
            for filename in (pgs,vob):
                media=inspect_media(filename)
                self.assertEqual(len(media.subtitles),1)
                st=Settings(media,0,1.8,1,1,vo_enabled=True,vo_audio_idx=1,vo_sub_idx=2)
                en=Engine()
                self.assertEqual(en.bitmap_subtitle_offset(st,'crop=640:344:0:0'),(0,True))
                self.assertEqual(en.bitmap_subtitle_offset(st,'crop=640:320:0:0'),(19,True))
                output=d/(filename.stem+'_out.mp4')
                en._video_pass(st,output,'crop=640:320:0:0,format=yuv420p',
                               'libx264',1500,'VOSTFR',d,audio_idx=1,
                               bitmap_sub_idx=2,bitmap_shift=19)
                self.assertTrue(output.is_file())

if __name__=='__main__':unittest.main()
