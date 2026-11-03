"""v2.4 : spécification intégrale DETX et liens Cappella relatifs au dossier de sortie."""
import shutil
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from easycap_core import (Cue, Engine, Settings, inspect_media, clean_caption, parse_ass,
                          parse_srt, parse_vtt, organize_cues, write_detx)


class TestDetxV24(unittest.TestCase):
    def _render(self, cues, d):
        out = Path(d) / 'Scène test & doublage' / 'Bande_Rythmo.detx'
        out.parent.mkdir(parents=True, exist_ok=True)
        write_detx(organize_cues(cues, 0, 15), out)
        return ET.parse(out).getroot(), out

    @staticmethod
    def _lines(root):
        return [(int(line.get('track')), line.findtext('text'),
                 line.findall('lipsync')[0].get('timecode'),
                 line.findall('lipsync')[1].get('timecode'))
                for line in root.findall('body/line')]

    def test_exact_header_paths_and_initial_cursor(self):
        with tempfile.TemporaryDirectory() as d:
            root, file = self._render([Cue(2, 5, 'Bonsoir')], d)
            self.assertEqual([n.tag for n in root.find('header')],
                             ['cappella','videofile','audiofile','last_position'])
            header = root.find('header')
            self.assertEqual(header.find('cappella').get('version'), '3.7.0')
            self.assertEqual(header.find('videofile').get('timestamp'), '01:00:00:00')
            self.assertEqual(header.findtext('videofile'), str((file.parent / 'Cappella_Video.mp4').resolve()))
            self.assertEqual(header.findtext('audiofile'), str((file.parent / 'Cappella_Voix.mp3').resolve()))
            self.assertEqual(header.find('last_position').attrib,
                             {'timecode': '01:00:00:00', 'track': '0'})
            self.assertEqual(self._lines(root)[0][2:], ('01:00:02:00','01:00:05:00'))
            # Dans le fichier XML, les caractères spéciaux des chemins sont échappés.
            self.assertIn('test &amp; doublage', file.read_text(encoding='utf-8'))

    def test_header_uses_explicit_output_location(self):
        with tempfile.TemporaryDirectory() as d:
            root, out = self._render([], d)
            paths = root.find('header')
            folder = Path(d) / 'Autre dossier'
            write_detx([], out, video_path=folder/'Cappella_Video.mp4',
                       audio_path=folder/'Cappella_Voix.mp3')
            roots = ET.parse(out).getroot()
            self.assertEqual(roots.findtext('header/videofile'), str((folder/'Cappella_Video.mp4').resolve()))
            self.assertEqual(roots.findtext('header/audiofile'), str((folder/'Cappella_Voix.mp3').resolve()))
            self.assertEqual(roots.find('header/last_position').get('timecode'), '01:00:00:00')

    def test_cleanup_tags_single_line_and_spaces(self):
        raw = '<i>Bonjour</i>\n<font color="#FF00FF">  chers</font> {\\an8} amis\\hde\n\n<i>tous</i>'
        self.assertEqual(clean_caption(raw), 'Bonjour chers amis de tous')
        self.assertEqual(clean_caption('<b>Tom</b> &amp; Jerry{\\pos(10,10)}'), 'Tom & Jerry')
        srt = '1\n00:00:01,000 --> 00:00:02,000\nBonjour\nle monde\n'
        with tempfile.TemporaryDirectory() as d:
            root, _ = self._render(parse_srt(srt, split_dialogues=True), d)
            self.assertEqual(self._lines(root)[0][1], 'Bonjour le monde')

    def test_duo_multiline_srt_dashes_with_continuations(self):
        srt = ('1\n00:00:02,000 --> 00:00:04,000\n<i>- Bonjour</i>\ncher ami\n'
               '<font color="#ABCDEF">— Salut</font>\nà toi aussi\n')
        with tempfile.TemporaryDirectory() as d:
            root, _ = self._render(parse_srt(srt, split_dialogues=True), d)
            self.assertEqual(self._lines(root), [(0, 'Bonjour cher ami','01:00:02:00','01:00:04:00'),
                                                  (1, 'Salut à toi aussi','01:00:02:00','01:00:04:00')])

    def test_vtt_duo_inline_and_normal(self):
        vtt = ('WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n- Oui. — Non.\n\n'
               '00:00:03.000 --> 00:00:04.000\nUne phrase\nsur deux lignes\n')
        with tempfile.TemporaryDirectory() as d:
            root, _ = self._render(parse_vtt(vtt, split_dialogues=True), d)
            self.assertEqual([(t, text) for t, text, *_ in self._lines(root)],
                             [(0,'Oui.'), (1,'Non.'), (0,'Une phrase sur deux lignes')])

    def test_ass_ssa_same_style_merges_different_styles_and_overlaps_split(self):
        ass = '''[Script Info]
Title: Exemples de sous-titres
[V4+ Styles]
Format: Name, Fontname, Fontsize
Style: Acteur,Arial,52
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:01.00,0:00:04.00,Acteur,,0,0,0,,{\\an8}<i>Bonjour</i>
Dialogue: 0,0:00:01.00,0:00:04.00,Acteur,,0,0,0,,cher ami
Dialogue: 0,0:00:01.00,0:00:04.00,AutreActeur,,0,0,0,,Un autre personnage
Dialogue: 0,0:00:02.00,0:00:05.00,Acteur,,0,0,0,,Personnage interrompant
Dialogue: 0,0:00:03.00,0:00:06.00,Troisième,,0,0,0,,Encore un autre
'''
        cues = organize_cues(parse_ass(ass, split_dialogues=True), 0, 7)
        self.assertEqual(len(cues), 4)
        self.assertEqual(cues[0].text, 'Bonjour cher ami')
        self.assertEqual([(c.text, c.track) for c in cues],
                         [('Bonjour cher ami',0), ('Un autre personnage',1),
                          ('Personnage interrompant',2), ('Encore un autre',3)])
        with tempfile.TemporaryDirectory() as d:
            root, _ = self._render(parse_ass(ass, split_dialogues=True), d)
            self.assertEqual(len(root.findall('roles/role')), 1)
            self.assertEqual([v.tag for v in root.findall('body/line')], ['line']*4)
            # Aucune métadonnée de sous-titres ASS ne subsiste dans le DETX.
            self.assertNotIn('Fontname', ET.tostring(root).decode())
            self.assertNotIn('Acteur', ET.tostring(root).decode())

    def test_ass_two_dash_dialogues_force_tracks_even_with_other_overlapping_style(self):
        ass = '''[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:01.00,0:00:04.00,PersonneA,,0,0,0,,{\\an8}- Bonjour\\N— Salut
Dialogue: 0,0:00:02.00,0:00:03.00,Autre,,0,0,0,,Bonjour aussi
'''
        with tempfile.TemporaryDirectory() as d:
            root, _ = self._render(parse_ass(ass, split_dialogues=True), d)
            self.assertEqual([(tr, txt) for tr, txt, *_ in self._lines(root)],
                             [(0, 'Bonjour'), (1,'Salut'), (2,'Bonjour aussi')])

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg absent')
    def test_real_mkv_subtitle_extraction_header_and_dual_tracks(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            srt = folder/'piste.srt'
            srt.write_text('1\n00:00:00,500 --> 00:00:01,500\n- Bonjour\n— Bonsoir\n', encoding='utf-8')
            mkv = folder/'film.mkv'
            subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i',
                'color=size=160x90:rate=24:duration=2','-f','lavfi','-i',
                'sine=frequency=350:duration=2','-i',str(srt),
                '-map','0:v','-map','1:a','-map','2:s',
                '-c:v','mpeg4','-c:a','aac','-c:s','srt',str(mkv)],
                check=True,capture_output=True,timeout=30)
            media=inspect_media(mkv)
            output = folder/'Dossier créé'
            output.mkdir()
            s = Settings(media, 0, 1.9, media.audios[0]['index'], media.audios[0]['index'],
                         convert_detx=True, detx_sub_idx=media.subtitles[0]['index'])
            Engine().prepare_subtitles(s, output, folder)
            root=ET.parse(output/'Bande_Rythmo.detx').getroot()
            self.assertEqual(root.findtext('header/videofile'),str((output/'Cappella_Video.mp4').resolve()))
            self.assertEqual(root.findtext('header/audiofile'),str((output/'Cappella_Voix.mp3').resolve()))
            self.assertEqual(root.find('header/last_position').get('timecode'),'01:00:00:00')
            self.assertEqual([c[0] for c in self._lines(root)],[0,1])
            self.assertEqual(self._lines(root)[0][2:4], ('01:00:00:12','01:00:01:12'))

if __name__ == '__main__':
    unittest.main()
