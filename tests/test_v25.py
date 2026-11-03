"""Régressions v2.5 : DETX normalisé seulement, VOSTFR originale."""
import shutil
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from easycap_core import (Cue, Engine, Settings, clean_caption, inspect_media, organize_cues,
                          parse_srt, parse_vtt, parse_ass, prepare_original_text_for_burn,
                          prepare_ass_original_for_burn, write_detx)


class DetxVostfrIsolation(unittest.TestCase):
    def test_detx_strips_tags_and_joins_lines_with_spaces(self):
        srt = ('1\n00:00:01,000 --> 00:00:03,000\n'
               '<i>Bonjour</i>\n<font color="#A1B2C3">tout le monde</font> {\\an8}\n')
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / 'Bande_Rythmo.detx'
            write_detx(organize_cues(parse_srt(srt, split_dialogues=True), 0, 4), file)
            root = ET.parse(file).getroot()
            line = root.find('body/line')
            self.assertEqual(line.findtext('text'), 'Bonjour tout le monde')
            self.assertEqual(line.attrib, {'role':'sub', 'track':'0'})
            self.assertEqual(line.find('lipsync').get('timecode'), '01:00:01:00')
            self.assertEqual(root.find('header/last_position').get('timecode'), '01:00:00:00')
            self.assertEqual(root.find('header/cappella').get('version'), '3.7.0')
            self.assertEqual(root.findtext('header/videofile'),str((file.parent/'Cappella_Video.mp4').resolve()))
            self.assertEqual(root.findtext('header/audiofile'),str((file.parent/'Cappella_Voix.mp3').resolve()))
            self.assertNotIn('<i>', file.read_text())

    def test_two_speakers_have_same_times_but_separate_tracks(self):
        for parser, text in ((parse_srt, '1\n00:00:02,000 --> 00:00:04,000\n<i>- Un</i> peu\n— Deux\n'),
                             (parse_vtt, 'WEBVTT\n\n00:00:02.000 --> 00:00:04.000\n- Un peu\n— Deux\n'),
                             (parse_ass, '[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\nDialogue: 0,0:00:02.00,0:00:04.00,A,,0,0,0,,{\\an8}- Un peu\\N— Deux\n')):
            with self.subTest(parser=parser.__name__):
                cues = organize_cues(parser(text, split_dialogues=True), 0, 5)
                self.assertEqual([(c.track,c.text) for c in cues], [(0,'Un peu'),(1,'Deux')])
                with tempfile.TemporaryDirectory() as d:
                    file=Path(d)/'Bande_Rythmo.detx'
                    write_detx(cues,file)
                    lines=ET.parse(file).getroot().findall('body/line')
                    self.assertEqual(lines[0].find('lipsync').get('timecode'),lines[1].find('lipsync').get('timecode'))
                    self.assertEqual(lines[0].findall('lipsync')[1].get('timecode'), lines[1].findall('lipsync')[1].get('timecode'))

    def test_ass_same_style_same_start_end_merge_when_interleaved(self):
        ass = ('[Script Info]\nTitle: Un film\n[Events]\n'
               'Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n'
               'Dialogue: 0,0:00:01.00,0:00:04.00,A,,0,0,0,,Ligne 1\n'
               'Dialogue: 0,0:00:01.00,0:00:04.00,B,,0,0,0,,Deuxième personnage\n'
               'Dialogue: 0,0:00:01.00,0:00:04.00,A,,0,0,0,,Ligne 2\n'
               'Dialogue: 0,0:00:02.00,0:00:05.00,C,,0,0,0,,Chevauche\n')
        cues=organize_cues(parse_ass(ass, split_dialogues=True),0,6)
        self.assertEqual([(c.track,c.text) for c in cues],
                         [(0,'Ligne 1 Ligne 2'),(1,'Deuxième personnage'),(2,'Chevauche')])
        with tempfile.TemporaryDirectory() as d:
            file=Path(d)/'Bande_Rythmo.detx'
            write_detx(cues,file)
            xml=file.read_text()
            self.assertNotIn('Title:',xml)
            self.assertNotIn('Fontname',xml)
            self.assertEqual(len(ET.parse(file).getroot().findall('body/line')),3)

    def test_srt_for_vo_preserves_dashes_italics_linebreaks_and_times(self):
        raw=('1\n00:01:40,000 --> 00:01:43,000\n'
             '- <i>Bonjour</i> !\n— <font color="#11DDFF">Salut</font> !\n\n'
             '2\n00:01:44,000 --> 00:01:45,000\nUn\nautre\nsous-titre\n')
        with tempfile.TemporaryDirectory() as d:
            source,dest=Path(d)/'raw.srt',Path(d)/'vo_burn.srt'
            source.write_text(raw, encoding='utf-8')
            count=prepare_original_text_for_burn(source,dest,100.5,106,'srt')
            self.assertEqual(count,2)
            data=dest.read_text(encoding='utf-8-sig')
            self.assertIn('- <i>Bonjour</i> !\n— <font color="#11DDFF">Salut</font> !',data)
            self.assertIn('Un\nautre\nsous-titre',data)
            self.assertIn('00:00:00,000 --> 00:00:02,500',data)
            self.assertNotIn('01:00:00',data)

    def test_vtt_for_vo_preserves_original_position_and_text(self):
        source_txt=('WEBVTT\n\nNOTE commentaire\n\n'
                    'identifiant\n00:00:02.000 --> 00:00:05.000 line:90% position:50%\n'
                    '<i>Une réplique</i>\n— Encore une\n')
        with tempfile.TemporaryDirectory() as d:
            src,dst=Path(d)/'raw.vtt',Path(d)/'vo_burn.vtt'
            src.write_text(source_txt,encoding='utf-8')
            self.assertEqual(prepare_original_text_for_burn(src,dst,1,6,'vtt'),1)
            result=dst.read_text()
            self.assertIn('WEBVTT',result)
            self.assertIn('NOTE commentaire',result)
            self.assertIn('identifiant\n00:00:01.000 --> 00:00:04.000 line:90% position:50%',result)
            self.assertIn('<i>Une réplique</i>\n— Encore une',result)

    def test_ass_for_vo_original_style_not_cleaned(self):
        original=('[Script Info]\nTitle: Film\n[V4+ Styles]\n'
                  'Format: Name, Fontname, Fontsize\nStyle: Default,Arial,26\n'
                  '[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n'
                  'Dialogue: 0,0:00:02.00,0:00:05.00,Default,,0,0,0,,{\\fs18}<i>- Salut</i>\\N— Bonjour\n')
        with tempfile.TemporaryDirectory() as d:
            src,dst=Path(d)/'raw.ass',Path(d)/'vo_burn.ass'
            src.write_text(original,encoding='utf-8')
            prepare_ass_original_for_burn(src,dst,1,6)
            data=dst.read_text(encoding='utf-8-sig')
            self.assertIn('Style: Default,Arial,26',data)
            self.assertIn(r'{\fs18}<i>- Salut</i>\N— Bonjour',data)
            self.assertIn('0:00:01.00,0:00:04.00',data)

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg absent')
    def test_srt_and_vtt_original_are_burnable_by_ffmpeg(self):
        for fmt,ts in (('srt','00:00:00,000 --> 00:00:01,100'),
                       ('vtt','00:00:00.000 --> 00:00:01.100')):
            with self.subTest(fmt=fmt), tempfile.TemporaryDirectory() as d:
                file=Path(d)/('source.'+fmt)
                header='WEBVTT\n\n' if fmt=='vtt' else '1\n'
                file.write_text(header+ts+'\n- <i>Bonjour</i>\n— Bonsoir\n',encoding='utf-8')
                output=Path(d)/'render.mp4'
                vf=f"subtitles=filename='{file}':force_style='FontName=Arial,FontSize=36,Alignment=2,MarginV=28,Outline=2,Shadow=1'"
                proc=subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-y',
                                      '-f','lavfi','-i','color=size=320x180:rate=24:duration=1.3',
                                      '-vf',vf,'-an','-c:v','mpeg4',str(output)],
                                     capture_output=True,text=True,timeout=30)
                self.assertEqual(proc.returncode,0,proc.stderr)
                self.assertGreater(output.stat().st_size,500)

if __name__ == '__main__':
    unittest.main()
