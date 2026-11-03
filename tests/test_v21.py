"""Régression des filtres stéréo, désormais fournis en quatre profils fixes."""
import math
import shutil
import subprocess
import sys
import tempfile
import unittest
import wave
from array import array
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from easycap_core import (STEREO_PRESETS, stereo_voice_filter, pan_filter, Engine,
                          Settings, Media, EasyCapError)


class TestV21(unittest.TestCase):
    def setUp(self):
        self.media=Media(Path('/film.mkv'),10.0,{'index':0},
                         [{'index':1,'channels':2,'codec_name':'flac', 'bits_per_raw_sample':'16'},
                          {'index':2,'channels':6,'channel_layout':'5.1','codec_name':'ac3'}],
                         [],24,False)

    def test_fixed_presets(self):
        self.assertEqual(set(STEREO_PRESETS), {'standard', 'room', 'room_reverb', 'memory'})
        self.assertEqual([p['label'] for p in STEREO_PRESETS.values()], [
            'Standard', "Intérieur d'une pièce",
            "Intérieur d'une pièce + Réverbération", 'Souvenirs / Voix intérieurs'])
        for name in STEREO_PRESETS:
            filt = stereo_voice_filter(name)
            self.assertIn('pan=stereo', filt)
            self.assertEqual('acrossover' in filt, name != 'standard')
        self.assertEqual(stereo_voice_filter(), pan_filter(2))
        self.assertIn('split=90 3600', stereo_voice_filter('room_reverb', 8000))
        for invalid in ('outside', 'full', 'reverb', 'nonexistent'):
            with self.assertRaises(ValueError): stereo_voice_filter(invalid)
        for invalid_rate in (0, 7999, 768001, 24000.5, True):
            with self.assertRaises(ValueError): stereo_voice_filter('standard', invalid_rate)

    def test_settings_validation(self):
        settings=Settings(self.media, 0, 2, 1, 1, stereo_preset='room_reverb')
        settings.validate()
        settings.stereo_preset='bad'
        with self.assertRaises(EasyCapError): settings.validate()
        self.assertEqual(Settings(self.media,0,2,1,1).stereo_preset, 'standard')
        self.assertNotIn('stereo_strength', Settings.__dataclass_fields__)

    def test_audio_pipeline_stereo_and_multichannel(self):
        calls=[]
        engine=Engine(log=lambda msg: calls.append(('LOG',msg)))
        def fake_ffmpeg(args,label,*a,**kw):
            calls.append((label,args))
        engine.ffmpeg=fake_ffmpeg
        with tempfile.TemporaryDirectory() as d:
            settings=Settings(self.media,0,2,1,1,stereo_preset='room')
            engine._audio(settings,Path(d))
            encoded=[a for name,a in calls if name in ('Sans_Voix.flac','Cappella_Sans_Voix.mp3')]
            self.assertEqual(len(encoded),2)
            for command in encoded:
                self.assertEqual(command[command.index('-af')+1],stereo_voice_filter('room'))
            calls.clear()
            settings.remove_audio_idx=2
            engine._audio(settings,Path(d))
            decoded=[a for name,a in calls if name=='Sans_Voix.ac3']
            self.assertEqual(len(decoded),1)
            command=decoded[0]
            self.assertEqual(command[command.index('-af')+1],pan_filter(6,'5.1'))
            calls.clear()
            settings.remove_mode='manual'
            engine._audio(settings,Path(d))
            self.assertFalse(any('-af' in a for _,a in calls if isinstance(a,list)))

    @unittest.skipUnless(shutil.which('ffmpeg'),'FFmpeg nécessaire')
    def test_real_center_attenuation_and_ambience(self):
        """700 Hz centrés supprimés, 60 Hz hors bande préservés.

        Le signal stéréo synthétique contient une voix test centrée 700 Hz,
        une basse centrale 60 Hz et une ambiance latérale 1200 Hz.
        """
        with tempfile.TemporaryDirectory() as d:
            d=Path(d)
            generated='aevalsrc=0.2*sin(2*PI*700*t)+0.06*sin(2*PI*60*t)+0.05*sin(2*PI*1200*t)|0.2*sin(2*PI*700*t)+0.06*sin(2*PI*60*t)-0.05*sin(2*PI*1200*t):s=12000:d=0.6'
            filters={'original':'anull',
                     'room':stereo_voice_filter('room'),
                     'room_reverb':stereo_voice_filter('room_reverb'),
                     'standard':stereo_voice_filter('standard')}
            for name, audio_filter in filters.items():
                subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-f','lavfi','-i',generated,
                         '-af',audio_filter,'-c:a','pcm_s16le','-y',str(d / (name+'.wav'))],
                         check=True,timeout=20)
            def coeff(name, frequency, channel=0):
                with wave.open(str(d / (name+'.wav'))) as w:
                    self.assertEqual(w.getnchannels(),2)
                    fs=w.getframerate()
                    pcm=array('h',w.readframes(w.getnframes()))
                # Jette le transitoire initial du crossover.
                signal=pcm[2000*2::2] if channel==0 else pcm[2000*2+1::2]
                signal=[v/32768.0 for v in signal]
                n=len(signal)
                c=sum(v*math.cos(2*math.pi*frequency*i/fs) for i,v in enumerate(signal))
                s=sum(v*math.sin(2*math.pi*frequency*i/fs) for i,v in enumerate(signal))
                return 2*math.hypot(c,s)/n
            original=coeff('original',700)
            self.assertLess(coeff('standard',700)/original,0.10)
            self.assertAlmostEqual(coeff('room',700)/original,0.15,delta=0.09)
            self.assertAlmostEqual(coeff('room_reverb',700)/original,0.05,delta=0.09)
            self.assertGreater(coeff('room',60)/coeff('original',60),0.77)
            self.assertGreater(coeff('room',1200)/coeff('original',1200),0.84)

if __name__=='__main__': unittest.main()
