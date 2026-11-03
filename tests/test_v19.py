"""Régressions v1.9 et v2.0 : +1h sans image ajoutée, duo sur pistes 0/1."""
import sys
import shutil
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from easycap_core import (Cue, Engine, Settings, inspect_media, split_two_speakers, parse_srt, parse_ass,
                          parse_vtt, extract_text, organize_cues, write_detx, detx_time)

class TestV19(unittest.TestCase):
    def _render(self, cues, fps=25):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'Bande_Rythmo.detx'
            write_detx(organize_cues(cues,0,max(100, max((c.end for c in cues), default=0)+1)),path,fps)
            root=ET.parse(path).getroot()
            return root

    def _lines(self, root):
        return [(line.get('track'),line.find('text').text,
                 line.findall('lipsync')[0].get('timecode'),
                 line.findall('lipsync')[1].get('timecode'))
                for line in root.findall('body/line')]

    def test_hour_offset_including_last_position_and_frame_rollover(self):
        root=self._render([Cue(0, .96, 'Début'), Cue(59.96, 60.04, 'Fin')])
        lines=self._lines(root)
        self.assertEqual(lines[0][2:4],('01:00:00:00','01:00:00:24'))
        self.assertEqual(lines[1][2:4],('01:00:59:24','01:01:00:01'))
        self.assertEqual(root.find('header/last_position').get('timecode'),'01:00:00:00')
        self.assertEqual(detx_time(0, 23.976),'01:00:00:00')
        self.assertEqual(detx_time(.96, 23.976),'01:00:00:23')
        self.assertEqual(detx_time(0.96663, 29.97),'01:00:00:29')

    def test_srt_two_lines_mixed_dashes(self):
        srt='1\n00:00:02,000 --> 00:00:05,000\n<i>- Bonjour.</i>\n— Salut.\n'
        root=self._render(parse_srt(srt,split_dialogues=True))
        lines=self._lines(root)
        self.assertEqual(lines,[('0','Bonjour.','01:00:02:00','01:00:05:00'),
                                ('1','Salut.','01:00:02:00','01:00:05:00')])

    def test_srt_one_line_inline_dialogue_from_user_example(self):
        srt='1\n00:07:17,383 --> 00:07:19,431\n- Hey, pass it! - Yeah!\n'
        root=self._render(parse_srt(srt,split_dialogues=True), 23.976)
        lines=self._lines(root)
        self.assertEqual([(track,text) for track,text,*_ in lines], [('0','Hey, pass it!'),('1','Yeah!')])
        self.assertEqual(lines[0][2:],lines[1][2:])
        self.assertEqual(lines[0][2],'01:07:17:09')

    def test_ass_split_preserves_styles_and_other_lines(self):
        ass='''[Script Info]
ScriptType: v4.00+
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:02.00,0:00:04.00,Actor,,0,0,0,,{\\an8}- Oui\\N— Non
Dialogue: 0,0:00:03.00,0:00:04.00,Extra,,0,0,0,,Autre personnage
'''
        cues=organize_cues(parse_ass(ass,split_dialogues=True),0,10)
        self.assertEqual([(c.track,c.text,c.style) for c in cues[:2]],
                         [(0,'Oui','Actor'),(1,'Non','Actor')])
        self.assertEqual(cues[2].track,2)
        self.assertEqual(cues[2].text,'Autre personnage')

    def test_vtt_like_srt(self):
        vtt='WEBVTT\n\n00:00:00.000 --> 00:00:01.000\n— Bonjour\n— Bonsoir\n'
        cues=parse_vtt(vtt,split_dialogues=True)
        self.assertEqual([c.forced_track for c in cues],[0,1])
        self.assertEqual([c.text for c in cues],['Bonjour','Bonsoir'])

    def test_vostfr_default_parser_unchanged(self):
        srt='1\n00:00:02,000 --> 00:00:05,000\n- Bonjour.\n- Salut.\n'
        normal=parse_srt(srt)
        self.assertEqual(len(normal),1)
        self.assertEqual(normal[0].text,'- Bonjour. - Salut.')
        self.assertIsNone(normal[0].forced_track)
        # Les tirets de texte normal ne sont pas des séparateurs forcés.
        self.assertIsNone(split_two_speakers('Ligne ordinaire - avec un tiret.'))

    def test_real_user_reference_inline_dialogue_is_recognized(self):
        root=Path(__file__).resolve().parents[1]
        cues=parse_srt((root/'exemples'/'BR.srt').read_text(encoding='utf-8-sig'),split_dialogues=True)
        paired=[c for c in cues if c.forced_track is not None]
        self.assertGreaterEqual(len(paired),10)
        self.assertEqual([x.text for x in paired[:2]],['Hey, pass it!','Yeah!'])

    def test_empty_detx_last_position_from_hour(self):
        root=self._render([])
        self.assertEqual(root.find('header/last_position').get('timecode'),'01:00:00:00')

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg absent')
    def test_real_mkv_extraction_detx_split_without_changing_cleaned_srt(self):
        with tempfile.TemporaryDirectory() as dirname:
            folder=Path(dirname)
            source=folder/'dialogues.srt'
            source.write_text('1\n00:00:00,500 --> 00:00:02,000\n- Bonjour !\n— Salut !\n\n'
                              '2\n00:00:02,500 --> 00:00:04,000\n- Vas-y. - D’accord.\n',
                              encoding='utf-8')
            mkv=folder/'source.mkv'
            subprocess.run([
                'ffmpeg','-hide_banner','-loglevel','error','-y',
                '-f','lavfi','-i','color=c=black:s=160x90:r=5:d=5',
                '-f','lavfi','-i','sine=f=400:d=5', '-i',str(source),
                '-map','0:v','-map','1:a','-map','2:s',
                '-c:v','mpeg4','-q:v','5','-c:a','aac','-c:s','srt',str(mkv)
            ],check=True, timeout=20,capture_output=True)
            media=inspect_media(mkv)
            settings=Settings(media,0,4.5,media.audios[0]['index'],media.audios[0]['index'],
                              convert_detx=True,detx_sub_idx=media.subtitles[0]['index'],
                              vo_enabled=True,vo_audio_idx=media.audios[0]['index'],
                              vo_sub_idx=media.subtitles[0]['index'])
            engine=Engine()
            engine.prepare_subtitles(settings,folder,folder)
            engine.prepare_vo_sub(settings,folder)
            parsed=ET.parse(folder/'Bande_Rythmo.detx').getroot()
            lines=self._lines(parsed)
            self.assertEqual([c[0] for c in lines],['0','1','0','1'])
            self.assertEqual([c[1] for c in lines],['Bonjour !','Salut !','Vas-y.','D’accord.'])
            self.assertEqual(lines[0][2],'01:00:00:12')
            self.assertEqual(lines[0][2:],lines[1][2:])
            self.assertEqual(len(parse_srt((folder/'Bande_Rythmo.srt').read_text(encoding='utf-8-sig'))),2)
            # La préparation VOSTFR conserve les deux dialogues d'origine.
            prepared = (folder/'vo_burn.srt').read_text(encoding='utf-8-sig')
            self.assertEqual(prepared.count('-->'),2)
            self.assertIn('- Bonjour !\n— Salut !', prepared)

if __name__=='__main__':
    unittest.main()
