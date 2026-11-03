"""EasyCap v2.7 : absence de perte d'une deuxième ligne et VOSTFR originale."""
import shutil
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from easycap_core import (Cue, EasyCapError, Engine, Media, Settings, organize_cues,
                          parse_ass, parse_srt, parse_vtt, prepare_original_text_for_burn,
                          write_detx, inspect_media, vostfr_text_force_style)


class DeuxiemeLigne(unittest.TestCase):
    def test_srt_duo_with_blank_between_lines_is_not_truncated(self):
        srt = ('1\n00:00:01,000 --> 00:00:04,000\n'
               '- <i>Bonjour monsieur</i>\n\n— <font color="#F0F000">Bonsoir madame</font>\n\n'
               '2\n00:00:05,000 --> 00:00:06,000\nUne\nphrase simple\n')
        cues = organize_cues(parse_srt(srt, split_dialogues=True), 0, 8)
        self.assertEqual([(c.track,c.text) for c in cues],
                         [(0,'Bonjour monsieur'),(1,'Bonsoir madame'),(0,'Une phrase simple')])
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)/'Bande_Rythmo.detx'
            write_detx(cues,out)
            xml = ET.parse(out).getroot()
            lines = xml.findall('body/line')
            self.assertEqual([x.attrib['track'] for x in lines], ['0','1','0'])
            self.assertEqual([x.findtext('text') for x in lines],
                             ['Bonjour monsieur','Bonsoir madame','Une phrase simple'])
            self.assertEqual(lines[0].findall('lipsync')[0].attrib,
                             lines[1].findall('lipsync')[0].attrib)
            self.assertEqual(lines[0].findall('lipsync')[1].attrib,
                             lines[1].findall('lipsync')[1].attrib)

    def test_overlapping_duos_are_on_four_distinct_tracks(self):
        srt = ('1\n00:00:01,000 --> 00:00:05,000\n- A\n— B\n\n'
               '2\n00:00:02,000 --> 00:00:04,000\n- C\n— D\n')
        cues=organize_cues(parse_srt(srt, split_dialogues=True),0,6)
        self.assertEqual([(c.track,c.text) for c in cues],[(0,'A'),(1,'B'),(2,'C'),(3,'D')])
        with tempfile.TemporaryDirectory() as d:
            file=Path(d)/'Bande_Rythmo.detx'
            write_detx(cues,file)
            self.assertEqual(len(ET.parse(file).getroot().findall('body/line')),4)

    def test_five_simultaneous_lines_raise_instead_of_silently_overwriting(self):
        cues=[Cue(1,4,x,style=str(i)) for i,x in enumerate('ABCDE')]
        with self.assertRaisesRegex(EasyCapError,'quatre'):
            organize_cues(cues,0,5)

    def test_srt_distinct_events_same_times_stay_distinct(self):
        data=('1\n00:00:01,000 --> 00:00:02,000\nPersonnage A\n\n'
              '2\n00:00:01,000 --> 00:00:02,000\nPersonnage B\n')
        cues=organize_cues(parse_srt(data,True),0,3)
        self.assertEqual([(c.track,c.text) for c in cues], [(0,'Personnage A'),(1,'Personnage B')])

    def test_ass_same_style_same_times_fused_and_different_styles_separated(self):
        ass=('[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n'
             'Dialogue: 0,0:00:01.00,0:00:03.00,A,,0,0,0,,Ligne une\n'
             'Dialogue: 0,0:00:01.00,0:00:03.00,B,,0,0,0,,Intervenant B\n'
             'Dialogue: 0,0:00:01.00,0:00:03.00,A,,0,0,0,,Ligne deux\n')
        cues=organize_cues(parse_ass(ass,True),0,4)
        self.assertEqual([(x.track,x.text) for x in cues],
                         [(0,'Ligne une Ligne deux'),(1,'Intervenant B')])

    def test_detx_does_not_modify_the_srt_sidecar_or_vostfr(self):
        raw=('1\n00:00:01,000 --> 00:00:04,000\n'
             '- <i>Bonjour</i>\n— <font color="#112233">Salut</font>\n')
        with tempfile.TemporaryDirectory() as d:
            folder=Path(d)
            extracted=folder/'source.srt'
            extracted.write_text(raw, encoding='utf-8')
            media=Media(folder/'source.mkv',10,{'index':0,'height':720},
                        [{'index':1,'codec_name':'aac','channels':2}],
                        [{'index':2,'codec_name':'subrip'}],24,False)
            config=Settings(media,0,5,1,1,convert_detx=True,detx_sub_idx=2,
                            vo_enabled=True,vo_audio_idx=1,vo_sub_idx=2,output_root=folder)
            engine=Engine()
            with patch.object(engine,'extract_sub',return_value=(extracted,'subrip','srt')):
                engine.prepare_subtitles(config,folder,folder)
                burn=engine.prepare_vo_sub(config,folder)
            for subtitle in (folder/'Bande_Rythmo.srt',burn):
                text=subtitle.read_text(encoding='utf-8-sig')
                self.assertIn('- <i>Bonjour</i>\n— <font color="#112233">Salut</font>', text)
            lines=ET.parse(folder/'Bande_Rythmo.detx').getroot().findall('body/line')
            self.assertEqual([(x.get('track'),x.findtext('text')) for x in lines],
                             [('0','Bonjour'),('1','Salut')])

    def test_vtt_sidecar_original_linebreaks_and_italics(self):
        text='WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n- <i>Un</i>\n— Deux\n'
        with tempfile.TemporaryDirectory() as d:
            src,dest=Path(d)/'from.vtt', Path(d)/'to.vtt'
            src.write_text(text)
            self.assertEqual(prepare_original_text_for_burn(src,dest,0,3,'vtt'),1)
            self.assertIn('- <i>Un</i>\n— Deux',dest.read_text())
            self.assertEqual([(c.track,c.text) for c in organize_cues(parse_vtt(text,True),0,3)],
                             [(0,'Un'),(1,'Deux')])

    def test_ass_vostfr_keeps_full_frame_if_crop_would_hide_lines(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)
            media=Media(path/'fake.mkv',4,{'index':0,'height':1080,'width':1920},
                        [{'index':1,'codec_name':'aac','channels':2}],
                        [{'index':2,'codec_name':'ass'}],24,False)
            s=Settings(media,0,3,1,1,vo_enabled=True,vo_audio_idx=1,
                       vo_sub_idx=2,output_root=path,optimize_4k=False)
            e=Engine()
            calls=[]
            with patch.object(e,'detect_crop',return_value='crop=1920:800:0:140'), \
                 patch.object(e,'_audio'), patch.object(e,'prepare_subtitles'), \
                 patch.object(e,'prepare_vo_sub',return_value=path/'original.ass'), \
                 patch.object(e,'_video_pass',side_effect=lambda *args,**kwargs: calls.append((args,kwargs))):
                e._run_impl(s)
            self.assertEqual(len(calls),2)
            self.assertIn('crop=1920:800:0:140',calls[0][0][2])
            self.assertNotIn('crop=1920:800:0:140',calls[1][0][2])
            self.assertIn('ass=filename=',calls[1][0][2])

    @unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg absent')
    def test_two_srt_lines_are_both_visible_on_rendered_frame(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest('Pillow absent')
        with tempfile.TemporaryDirectory() as d:
            folder=Path(d)
            subs=folder/'dialogues.srt'
            subs.write_text('1\n00:00:00,000 --> 00:00:02,000\n'
                            '- Premier personnage\n— Deuxième personnage\n',encoding='utf-8')
            image=folder/'image.png'
            vf=(f"subtitles=filename='{subs}':"
                f"force_style='{vostfr_text_force_style()}'")
            proc=subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-y',
                '-f','lavfi','-i','color=c=black:size=640x360:rate=24:duration=1',
                '-vf',vf,'-frames:v','1','-update','1',str(image)],
                capture_output=True,text=True,timeout=20)
            self.assertEqual(proc.returncode,0,proc.stderr)
            frame=Image.open(image).convert('RGB')
            rows=[]
            for y in range(frame.height):
                light=sum(max(frame.getpixel((x,y)))>130 for x in range(frame.width))
                if light>10:
                    rows.append(y)
            bands=[]
            for y in rows:
                if not bands or y>bands[-1][-1]+1:
                    bands.append([y])
                else:
                    bands[-1].append(y)
            self.assertGreaterEqual(len(bands),2,'La deuxième rangée de texte n’apparaît pas dans la vidéo')

    @unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg absent')
    def test_ass_extract_uses_native_packet_copy(self):
        # Contrôle de la commande pour éviter une normalisation destructive
        # des styles avant la préparation ASS de la scène.
        with tempfile.TemporaryDirectory() as d:
            folder=Path(d)
            media=Media(folder/'clip.mkv',6,{'index':0,'height':720,'width':1280},
                        [{'index':1,'codec_name':'aac','channels':2}],
                        [{'index':2,'codec_name':'ass'}],24,False)
            s=Settings(media,0,4,1,1,vo_enabled=True,vo_audio_idx=1,vo_sub_idx=2)
            e=Engine()
            captures=[]
            def fake(args,label,*rest,**kwargs):
                captures.append((args,label))
                Path(args[-1]).write_text('[Events]\n',encoding='utf-8')
                return 0.1
            with patch.object(e,'ffmpeg',side_effect=fake):
                e.extract_sub(s,2,folder,'source')
            self.assertEqual(len(captures),1)
            self.assertEqual(captures[0][0][captures[0][0].index('-c:s')+1],'copy')

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg absent')
    def test_end_to_end_two_srt_lines_in_both_detx_and_vostfr(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)
            sub=p/'source.srt'
            sub.write_text('1\n00:00:00,500 --> 00:00:02,500\n- <i>Bonjour</i>\n— Salut\n',encoding='utf-8')
            mkv=p/'film.mkv'
            make=['ffmpeg','-hide_banner','-loglevel','error','-y',
                  '-f','lavfi','-i','color=c=black:size=640x360:rate=24:duration=3.2',
                  '-f','lavfi','-i','sine=frequency=440:sample_rate=48000:duration=3.2',
                  '-i',str(sub),'-map','0:v:0','-map','1:a:0','-map','2:s:0',
                  '-c:v','libx264','-preset','ultrafast','-crf','32',
                  '-c:a','aac','-c:s','srt','-shortest',str(mkv)]
            proc=subprocess.run(make,capture_output=True,text=True,timeout=30)
            self.assertEqual(proc.returncode,0,proc.stderr)
            media=inspect_media(mkv)
            ai=media.audios[0]['index']; si=media.subtitles[0]['index']
            s=Settings(media,0,min(2.5, media.duration-0.1),ai,ai,convert_detx=True,detx_sub_idx=si,
                       vo_enabled=True,vo_audio_idx=ai,vo_sub_idx=si,
                       vo_height=720,output_root=p)
            e=Engine()
            output=e.run(s)
            lines=ET.parse(output/'Bande_Rythmo.detx').getroot().findall('body/line')
            self.assertEqual([(x.get('track'),x.findtext('text')) for x in lines],
                             [('0','Bonjour'),('1','Salut')])
            self.assertIn('- <i>Bonjour</i>\n— Salut',(output/'Bande_Rythmo.srt').read_text(encoding='utf-8-sig'))
            self.assertTrue((output/'Scène_VOSTFR.mp4').is_file())
            self.assertGreater((output/'Scène_VOSTFR.mp4').stat().st_size,8000)
            # Contrôler le rendu du vrai fichier de sortie, pas seulement
            # la présence des deux blocs DETX / de la source SRT.
            try:
                from PIL import Image
            except ImportError:
                pass
            else:
                png = p/'vostfr_1s.png'
                snapshot=subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-y',
                    '-ss','1.0','-i',str(output/'Scène_VOSTFR.mp4'),
                    '-frames:v','1','-update','1',str(png)],
                    capture_output=True,text=True,timeout=20)
                self.assertEqual(snapshot.returncode,0,snapshot.stderr)
                frame=Image.open(png).convert('RGB')
                y_rows=[]
                for y in range(frame.height):
                    bright=sum(max(frame.getpixel((x,y)))>130 for x in range(frame.width))
                    if bright>5:
                        y_rows.append(y)
                bands=[]
                for y in y_rows:
                    if not bands or y>bands[-1][-1]+1:
                        bands.append([y])
                    else:
                        bands[-1].append(y)
                self.assertGreaterEqual(len(bands),2,
                     'Deuxième ligne VOSTFR absente de la vidéo x264 finale')
            self.assertNotIn('ERREUR', (output/'EasyCap_Journal.txt').read_text())

if __name__ == '__main__':
    unittest.main()
