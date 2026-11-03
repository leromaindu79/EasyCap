"""EasyCap v2.6 — DETX sur pistes dédiées, VOSTFR d'origine, tailles 72/24/19."""
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from easycap_core import (Cue, Engine, Media, Settings, VOSTFR_TEXT_FONTS,
                          organize_cues, parse_ass, parse_srt, parse_vtt,
                          prepare_ass_original_for_burn,
                          prepare_original_text_for_burn, vo_subtitle_font_size, write_detx)


class TestV26(unittest.TestCase):
    def _root(self, raw, parser, seconds=5):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / 'Bande_Rythmo.detx'
            cues = organize_cues(parser(raw, split_dialogues=True), 0, seconds)
            write_detx(cues, out)
            return ET.parse(out).getroot(), out.read_text(encoding='utf-8')

    def test_four_fonts_by_effective_resolution(self):
        self.assertEqual(VOSTFR_TEXT_FONTS, {1080: 20, 720: 20, 576: 20})
        for selected, source, expected in ((1080,2160,20),(1080,1080,20),
                                           (1080,720,20),(720,2160,20),
                                           (1080,576,20),(576,2160,20),
                                           (720,480,20)):
            with self.subTest(selected=selected, source=source):
                self.assertEqual(vo_subtitle_font_size(selected, source), expected)

    def test_dash_dialogues_become_two_real_xml_line_blocks(self):
        examples = (
            (parse_srt, '1\n00:00:02,000 --> 00:00:04,000\n<i>- Bonjour</i> toi\n— <font color="#CDEF12">Salut !</font>\n'),
            (parse_vtt, 'WEBVTT\n\n00:00:02.000 --> 00:00:04.000\n<i>- Bonjour</i> toi\n— <font color="#CDEF12">Salut !</font>\n'),
            (parse_ass, '[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n'
                        'Dialogue: 0,0:00:02.00,0:00:04.00,Default,,0,0,0,,{\\an8}- Bonjour toi\\N— Salut !\n'),
        )
        for parser, raw in examples:
            with self.subTest(parser=parser.__name__):
                root, data = self._root(raw, parser)
                lines = root.findall('body/line')
                self.assertEqual(len(lines), 2)
                self.assertEqual([line.attrib for line in lines],
                                 [{'role':'sub','track':'0'}, {'role':'sub','track':'1'}])
                self.assertEqual([line.findtext('text') for line in lines], ['Bonjour toi','Salut !'])
                for line in lines:
                    lipsync = line.findall('lipsync')
                    self.assertEqual(len(lipsync),2)
                    self.assertEqual(lipsync[0].attrib,{'timecode':'01:00:02:00','type':'in_open'})
                    self.assertEqual(lipsync[1].attrib,{'timecode':'01:00:04:00','type':'out_open'})
                self.assertNotIn('font color', data)
                self.assertNotIn('<i>', data)
                self.assertNotIn('\\an8', data)

    def test_plain_multiline_without_dashes_is_merged_with_spaces(self):
        raw = ('1\n00:00:00,000 --> 00:00:02,000\n'
               '<i>Une phrase</i>\n<font color="#FFFF00">sur deux lignes</font>\n'
               'avec {\\an5} une troisième\n')
        root, xml = self._root(raw, parse_srt)
        lines = root.findall('body/line')
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0].get('track'), '0')
        self.assertEqual(lines[0].findtext('text'), 'Une phrase sur deux lignes avec une troisième')
        for tag in ('<i>', '</i>', '<font', '</font>', r'{\an5}'):
            self.assertNotIn(tag, xml)

    def test_ass_four_distinct_tracks_overlap_and_same_style_merges(self):
        raw = ('[Script Info]\nTitle: MA SOURCE ASS\n[V4+ Styles]\n'
               'Style: Premier,Arial,42\n[Events]\n'
               'Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n'
               'Dialogue: 0,0:00:00.00,0:00:03.00,Premier,,0,0,0,,Bonjour\n'
               'Dialogue: 0,0:00:00.20,0:00:03.20,Second,,0,0,0,,Réplique 2\n'
               'Dialogue: 0,0:00:00.40,0:00:03.40,Third,,0,0,0,,Réplique 3\n'
               'Dialogue: 0,0:00:00.60,0:00:03.60,Fourth,,0,0,0,,Réplique 4\n'
               'Dialogue: 0,0:00:00.00,0:00:03.00,Premier,,0,0,0,,suite de Bonjour\n')
        root, xml = self._root(raw, parse_ass)
        lines = root.findall('body/line')
        self.assertEqual([x.get('track') for x in lines], ['0', '1', '2', '3'])
        self.assertEqual(lines[0].findtext('text'), 'Bonjour suite de Bonjour')
        self.assertNotIn('MA SOURCE ASS', xml)
        self.assertNotIn('Style:', xml)
        self.assertNotIn('Fontname', xml)
        self.assertNotIn('Arial', xml)

    def test_ssa_parse_and_convert_avoids_subtitle_metadata(self):
        raw = ('[Script Info]\nTitle: Mon SSA\n[V4 Styles]\n'
               'Format: Name, Fontname, Fontsize\nStyle: Default,Arial,18\n'
               '[Events]\nFormat: Marked, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n'
               'Dialogue: Marked=0,0:00:01.00,0:00:03.00,Default,,0000,0000,0000,,{\\an2}<i>Bonjour</i>\\Nmonsieur\n')
        root, xml = self._root(raw, parse_ass)
        self.assertEqual(root.findtext('body/line/text'),'Bonjour monsieur')
        self.assertNotIn('Mon SSA',xml)
        self.assertNotIn('Arial',xml)

    def test_header_exact_semantics_absolute_paths(self):
        with tempfile.TemporaryDirectory() as d:
            target=Path(d)/'MonFilm'/'Bande_Rythmo.detx'
            target.parent.mkdir()
            write_detx([Cue(0,1,'Test')], target)
            root=ET.parse(target).getroot()
            header=root.find('header')
            self.assertEqual(header.find('cappella').attrib, {'version':'3.7.0'})
            self.assertEqual(header.find('videofile').attrib, {'timestamp':'01:00:00:00'})
            self.assertEqual(header.find('videofile').text, str((target.parent/'Cappella_Video.mp4').resolve()))
            self.assertEqual(header.find('audiofile').text, str((target.parent/'Cappella_Voix.mp3').resolve()))
            self.assertEqual(header.find('last_position').attrib, {'timecode':'01:00:00:00', 'track':'0'})
            self.assertEqual(root.find('body/line/lipsync').get('timecode'), '01:00:00:00')

    def test_vostfr_preserves_srt_lines_marks_and_italics(self):
        raw='1\n00:01:40,000 --> 00:01:42,000\n- <i>Bonjour</i>\n— Salut !\n'
        with tempfile.TemporaryDirectory() as d:
            source=Path(d)/'source.srt'
            dest=Path(d)/'scene.srt'
            source.write_text(raw,encoding='utf-8')
            self.assertEqual(prepare_original_text_for_burn(source,dest,100,103,'srt'),1)
            output=dest.read_text(encoding='utf-8-sig')
            self.assertIn('- <i>Bonjour</i>\n— Salut !',output)
            self.assertIn('00:00:00,000 --> 00:00:02,000',output)

    def test_vostfr_preserves_vtt_cue_settings_and_text(self):
        raw=('WEBVTT\n\n00:01:40.000 --> 00:01:42.000 align:start position:20%\n'
             '- <i>Bonjour</i>\n— Salut !\n')
        with tempfile.TemporaryDirectory() as d:
            source=Path(d)/'source.vtt'
            dest=Path(d)/'scene.vtt'
            source.write_text(raw,encoding='utf-8')
            self.assertEqual(prepare_original_text_for_burn(source,dest,100,103,'vtt'),1)
            output=dest.read_text(encoding='utf-8')
            self.assertIn('00:00:00.000 --> 00:00:02.000 align:start position:20%', output)
            self.assertIn('- <i>Bonjour</i>\n— Salut !', output)

    def test_vostfr_ass_preserves_font_and_position_overrides(self):
        raw=('[Script Info]\nTitle: Style original\n[V4+ Styles]\n'
             'Format: Name, Fontname, Fontsize\nStyle: Default,Arial,18\n'
             '[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n'
             'Dialogue: 0,0:00:02.00,0:00:05.00,Default,,0,0,0,,{\\fs22\\pos(150,100)}- <i>Bonjour</i>\\N— Salut !\n')
        with tempfile.TemporaryDirectory() as d:
            source=Path(d)/'source.ass'
            dest=Path(d)/'scene.ass'
            source.write_text(raw,encoding='utf-8')
            prepare_ass_original_for_burn(source,dest,1,6)
            output=dest.read_text(encoding='utf-8-sig')
            self.assertIn('Style: Default,Arial,18',output)
            self.assertIn(r'{\fs22\pos(150,100)}- <i>Bonjour</i>\N— Salut !',output)
            self.assertIn('0:00:01.00,0:00:04.00',output)

    def test_engine_vo_route_reuses_untouched_srt_text_not_detx(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)
            original=path/'source.srt'
            original.write_text('1\n00:00:01,000 --> 00:00:03,000\n- <i>Bonjour</i>\n— Salut !\n', encoding='utf-8')
            media=Media(path/'film.mkv',10,{'index':0,'height':720},
                        [{'index':1,'codec_name':'aac','channels':2}],
                        [{'index':2,'codec_name':'subrip'}],24.0,False)
            settings=Settings(media,0,4,1,1,vo_enabled=True,vo_audio_idx=1,
                              vo_sub_idx=2,vo_height=720)
            engine=Engine()
            with patch.object(engine,'extract_sub',return_value=(original,'subrip','srt')):
                prepared=engine.prepare_vo_sub(settings,path)
            self.assertIsNotNone(prepared)
            result=prepared.read_text(encoding='utf-8-sig')
            self.assertIn('- <i>Bonjour</i>\n— Salut !',result)
            self.assertEqual(vo_subtitle_font_size(settings.vo_height,media.video['height']),20)

if __name__=='__main__':
    unittest.main()
