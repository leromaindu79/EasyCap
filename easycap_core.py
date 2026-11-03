"""EasyCap — moteur de préparation de bande rythmo. Python 3.10+ / FFmpeg 6+.

Aucun shell n'est utilisé. Les arguments sont passés à FFmpeg en tableaux,
et toutes les tâches peuvent être annulées depuis l'interface.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from fractions import Fraction
from html import unescape
from pathlib import Path
from typing import Callable
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import xml.etree.ElementTree as ET

VIDEO_EXTS = {'.mp4', '.mkv', '.m2ts', '.avi'}
TEXT_CODECS = {'subrip', 'srt', 'ass', 'ssa', 'webvtt', 'mov_text', 'text',
               'microdvd', 'mpl2', 'subviewer', 'subviewer1', 'sami', 'ttml'}
IMAGE_CODECS = {'hdmv_pgs_subtitle', 'dvd_subtitle', 'dvb_subtitle', 'xsub'}
X264_PRESET = 'medium'  # Preset H.264, les deux passes VO/VOSTFR.
# Libass donne aux SRT/VTT un canevas logique de référence. Une même valeur
# force_style sur des sorties différentes fournit un texte de taille *relative*
# identique à l'image finale (testé sur 1920x1080, 1280x720, 1024x576).
# Source de référence : sous-titres blancs centrés avec bordure noire discrète.
VOSTFR_TEXT_FONT_SIZE = 20
VOSTFR_TEXT_FONTS = {1080: VOSTFR_TEXT_FONT_SIZE,
                     720: VOSTFR_TEXT_FONT_SIZE,
                     576: VOSTFR_TEXT_FONT_SIZE}
VOSTFR_TEXT_OUTLINE = 0.9
VOSTFR_TEXT_MARGIN_V = 15
VOSTFR_TEXT_SHADOW = 0


def vostfr_text_force_style() -> str:
    """Rendu SRT/VTT discret et proportionnel, indépendant de la résolution.

    Ne PAS appliquer aux ASS/SSA, dont les styles originaux sont conservés.
    """
    return (f'FontName=Arial,FontSize={VOSTFR_TEXT_FONT_SIZE},Alignment=2,'
            f'MarginV={VOSTFR_TEXT_MARGIN_V},Outline={VOSTFR_TEXT_OUTLINE},'
            f'Shadow={VOSTFR_TEXT_SHADOW},PrimaryColour=&H00FFFFFF,'
            f'OutlineColour=&H00000000')
DETX_START_HOUR = 1  # Origine Cappella 01:00:00:00, sans décalage d'une image.
CAPPELLA_FPS = 24  # CFR : ne modifie pas la vitesse de lecture / les PTS de la source.
DETX_FPS = CAPPELLA_FPS  # Synchronisation des repères DETX avec la vidéo Cappella.
# La réduction des voix stéréo vise le signal commun L+R (centre fantôme).
# Un crossover complémentaire conserve les graves et les aigus extérieurs à
# la bande choisie. Il ne s'agit PAS d'une séparation de sources par IA.
STEREO_PRESETS = {
    # Une seule intensité fixe par profil : aucun contrôle de puissance côté GUI.
    # « Standard » reproduit l'annulation classique pleine bande du centre stéréo.
    'standard': {'label': 'Standard', 'strength': 100, 'low': None, 'high': None},
    'room': {'label': "Intérieur d'une pièce", 'strength': 85, 'low': 150, 'high': 5200},
    'room_reverb': {'label': "Intérieur d'une pièce + Réverbération", 'strength': 95, 'low': 90, 'high': 7000},
    'memory': {'label': 'Souvenirs / Voix intérieurs', 'strength': 90, 'low': 320, 'high': 3400},
}
SUBTITLE_PREROLL = 60.0  # Conserve les dialogues commencés avant la scène.
LOSSLESS_DEST = {'flac', 'pcm_s16le', 'pcm_s24le', 'pcm_s32le', 'pcm_f32le',
                 'pcm_f64le', 'pcm_s16be', 'pcm_s24be', 'pcm_s32be', 'pcm_u8',
                 'dts', 'truehd', 'mlp', 'eac3', 'alac', 'wavpack', 'ape'}


class EasyCapError(RuntimeError):
    pass


class Cancelled(EasyCapError):
    pass


def binary(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise EasyCapError(f"{name} est introuvable. Installez FFmpeg et ajoutez-le au PATH.")
    return path


def time_to_seconds(value: str) -> float:
    text = value.strip().replace(',', '.')
    parts = text.split(':')
    if len(parts) > 3 or not parts or not all(re.fullmatch(r'\d+(?:\.\d+)?', p) for p in parts):
        raise ValueError("Timecode invalide. Exemple : 00:12:35 ou 00:12:35.250")
    if len(parts) == 3:
        h, m, s = map(float, parts)
        if not (0 <= m < 60 and 0 <= s < 60):
            raise ValueError('Minutes ou secondes en dehors de 00–59.')
        return h * 3600 + m * 60 + s
    if len(parts) == 2:
        m, s = map(float, parts)
        if not 0 <= s < 60:
            raise ValueError('Secondes en dehors de 00–59.')
        return m * 60 + s
    return float(parts[0])


def fmt_time(seconds: float, decimals: bool = False) -> str:
    ms = max(0, int(round(seconds * 1000)))
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, millis = divmod(rem, 1000)
    return f'{h:02d}:{m:02d}:{s:02d}' + (f'.{millis:03d}' if decimals else '')


def srt_time(seconds: float) -> str:
    return fmt_time(seconds, True).replace('.', ',')


def ass_time(seconds: float) -> str:
    cs = max(0, int(round(seconds * 100)))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, c = divmod(rem, 100)
    return f'{h}:{m:02d}:{s:02d}.{c:02d}'


def frame_time(seconds: float, fps: float, offset_frames: int = 0) -> str:
    """DETX : HH:MM:SS:FF, comptage par image au sein de chaque seconde.

    Les références BR.srt/BR.detx emploient un arrondi à l'image la plus proche
    (23.976 i/s), pas un simple plancher des millisecondes.

    offset_frames est appliqué APRÈS la quantification, directement sur le
    timecode HH:MM:SS:FF, pour garantir exactement une image de décalage
    même aux fréquences fractionnaires (23.976, 29.97, etc.).
    """
    fps = fps if fps > 0 else 25
    nominal = max(1, int(round(fps)))
    seconds = max(0., seconds)
    whole = int(math.floor(seconds))
    frame = int(math.floor((seconds - whole) * fps + 0.5))
    if frame >= nominal:
        whole += 1
        frame = 0
    total_frames_in_tc = max(0, whole * nominal + frame + offset_frames)
    whole, frame = divmod(total_frames_in_tc, nominal)
    h, rem = divmod(whole, 3600)
    m, sec = divmod(rem, 60)
    return f'{h:02d}:{m:02d}:{sec:02d}:{frame:02d}'


def _float_ratio(value: str | None, default=25.) -> float:
    try:
        n = float(Fraction(str(value)))
        return n if n > 0 else default
    except (ValueError, ZeroDivisionError, TypeError):
        return default


def _run_quick(cmd: list[str]) -> str:
    p = subprocess.run(cmd, capture_output=True, text=True, errors='replace', stdin=subprocess.DEVNULL)
    if p.returncode:
        raise EasyCapError((p.stderr or p.stdout or 'Échec de la commande')[-3000:])
    return p.stdout


@dataclass
class Media:
    path: Path
    duration: float
    video: dict
    audios: list[dict]
    subtitles: list[dict]
    fps: float
    hdr: bool


def inspect_media(path: str | Path) -> Media:
    path = Path(path).expanduser().resolve()
    if not path.is_file() or path.suffix.lower() not in VIDEO_EXTS:
        raise EasyCapError('Sélectionnez une vidéo MP4, MKV, M2TS ou AVI valide.')
    raw = _run_quick([binary('ffprobe'), '-v', 'error', '-show_streams',
                     '-show_format', '-of', 'json', str(path)])
    meta = json.loads(raw)
    streams = meta.get('streams', [])
    videos = [s for s in streams if s.get('codec_type') == 'video'
              and not s.get('disposition', {}).get('attached_pic')]
    audios = [s for s in streams if s.get('codec_type') == 'audio']
    subtitles = [s for s in streams if s.get('codec_type') == 'subtitle']
    if not videos:
        raise EasyCapError('Le fichier ne contient aucune piste vidéo.')
    if not audios:
        raise EasyCapError('Vidéo refusée : aucune piste audio trouvée.')
    video = videos[0]
    duration = float(meta.get('format', {}).get('duration') or video.get('duration') or 0)
    if duration <= 0:
        raise EasyCapError('Durée de la vidéo inconnue ou invalide.')
    fps = _float_ratio(video.get('avg_frame_rate'), default=0) or _float_ratio(video.get('r_frame_rate'))
    side = str(video.get('side_data_list', [])).lower()
    transfer = str(video.get('color_transfer', '')).lower()
    hdr = transfer in ('smpte2084', 'arib-std-b67') or any(
        tag in side for tag in ('dovi', 'dolby vision', 'hdr10+', 'hdr dynamic', 'hdr10plus'))
    return Media(path, duration, video, audios, subtitles, fps, hdr)


def stream_label(stream: dict) -> str:
    tags = stream.get('tags', {})
    lang = tags.get('language', 'und')
    title = tags.get('title') or tags.get('handler_name', '')
    codec = stream.get('codec_name', '?').upper()
    ch = stream.get('channel_layout') or f"{stream.get('channels', '?')} can."
    suffix = f' — {title}' if title and title not in ('SoundHandler', 'SubtitleHandler') else ''
    return f"#{stream['index']} [{lang}] {codec} | {ch}{suffix}"


@dataclass
class Cue:
    start: float
    end: float
    text: str
    style: str = 'Default'
    track: int = 0
    forced_track: int | None = None  # Duos : pistes 0/1, ou 2/3 si un duo est déjà présent.
    source_format: str = "srt"  # Fusion d’événements identiques réservée à ASS/SSA.


@dataclass
class Settings:
    media: Media
    start: float
    end: float
    cappella_audio_idx: int
    remove_audio_idx: int
    remove_mode: str = 'auto'  # auto ou manual
    convert_detx: bool = False
    detx_sub_idx: int | None = None
    vo_enabled: bool = False
    vo_audio_idx: int | None = None
    vo_sub_idx: int | None = None
    vo_height: int = 1080
    optimize_4k: bool = True
    output_root: Path = field(default_factory=lambda: Path.home() / 'Videos')
    stereo_preset: str = 'standard'

    def validate(self):
        if self.end <= self.start or self.start < 0 or self.end > self.media.duration + 0.1:
            raise EasyCapError('L’intervalle choisi doit être positif et rester dans la durée de la vidéo.')
        ai = {s['index'] for s in self.media.audios}
        si = {s['index'] for s in self.media.subtitles}
        if self.cappella_audio_idx not in ai or self.remove_audio_idx not in ai:
            raise EasyCapError('Piste audio sélectionnée introuvable.')
        if self.remove_mode not in ('auto', 'manual'):
            raise EasyCapError('Mode audio invalide.')
        if self.stereo_preset not in STEREO_PRESETS:
            raise EasyCapError('Préréglage stéréo inconnu.')
        if self.convert_detx and (self.detx_sub_idx not in si or
                self._sub(self.detx_sub_idx).get('codec_name') not in TEXT_CODECS):
            raise EasyCapError('Le DETX nécessite une piste de sous-titres textuelle.')
        if self.vo_enabled and self.vo_audio_idx not in ai:
            raise EasyCapError('Choisissez une piste audio pour la version originale.')
        if self.vo_sub_idx is not None and self.vo_sub_idx not in si:
            raise EasyCapError('Piste de sous-titres VO introuvable.')
        if self.vo_enabled and self.vo_sub_idx is not None:
            subtitle_codec = self._sub(self.vo_sub_idx).get('codec_name', '')
            if subtitle_codec not in TEXT_CODECS | {'hdmv_pgs_subtitle', 'dvd_subtitle'}:
                raise EasyCapError(
                    f'Sous-titres {subtitle_codec} non pris en charge pour l’incrustation. '
                    'Choisissez SRT/VTT/ASS, PGS, VobSub ou aucun sous-titre.')
        if self.vo_height not in (576, 720, 1080):
            raise EasyCapError('Résolution VO invalide.')

    def _sub(self, idx):
        return next(s for s in self.media.subtitles if s['index'] == idx)


def clean_caption(text: str) -> str:
    text = unescape(text).replace('\\N', ' ').replace('\\n', ' ').replace('\\h', ' ')
    text = re.sub(r'<\s*/?\s*(?:i|b|u|font)\b[^>]*>', '', text, flags=re.I)
    text = re.sub(r'\{\\[^{}]*\}', '', text)  # ASS, SRT position tags
    text = re.sub(r'<[^>]+>', '', text)  # VTT cue tags
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def split_two_speakers(raw: str) -> tuple[str, str] | None:
    """Détecte deux répliques introduites par « - » ou « — ».

    Accepte aussi bien :
      - Première voix\n- Deuxième voix
    que :
      - Première voix - Deuxième voix

    Cette interprétation est destinée au DETX seulement ; la VOSTFR conserve
    la mise en page de sa source. Les signes sont retirés, pas le dialogue.
    """
    # Les fichiers ASS emploient \N pour un retour à la ligne.
    raw = raw.replace(r'\N', '\n').replace(r'\n', '\n')
    lines = [clean_caption(line) for line in raw.splitlines()]
    lines = [line for line in lines if line]
    # Deux répliques peuvent chacune se poursuivre sur d'autres lignes.
    # La présence de deux débuts de dialogue reste obligatoire : un simple
    # saut de ligne n'indique jamais un changement de personnage.
    prefixes = [re.match(r'^[-—]\s+(.+)$', line) for line in lines]
    starts = [index for index, hit in enumerate(prefixes) if hit is not None]
    if len(starts) == 2 and starts[0] == 0:
        first = ' '.join([prefixes[0].group(1)] + lines[1:starts[1]]).strip()
        second = ' '.join([prefixes[starts[1]].group(1)] + lines[starts[1]+1:]).strip()
        if first and second:
            return first, second
    if len(lines) == 1:
        inline = re.match(r'^[-—]\s+(.+?)\s+[-—]\s+(.+)$', lines[0])
        if inline:
            return inline.group(1).strip(), inline.group(2).strip()
    return None


def _cues_from_caption(start: float, end: float, raw: str,
                       style: str = 'Default', split_dialogues: bool = False,
                       source_format: str = "srt") -> list[Cue]:
    if split_dialogues:
        speakers = split_two_speakers(raw)
        if speakers:
            return [Cue(start, end, text, style, forced_track=index,
                        source_format=source_format)
                    for index, text in enumerate(speakers) if text]
    caption = clean_caption(raw)
    return [Cue(start, end, caption, style, source_format=source_format)] if caption else []


def subtitle_blocks(content: str) -> list[str]:
    """Blocs sous-titres tolérants sans perdre la deuxième réplique.

    Une ligne de dialogue séparée par une ligne vide ne doit pas être perdue
    lorsqu'elle n'a pas sa propre borne temporelle. Les blocs VTT NOTE, STYLE
    et REGION restent, en revanche, des métadonnées indépendantes.
    """
    normalized = content.lstrip('\ufeff').replace('\r\n', '\n').replace('\r', '\n')
    pieces = re.split(r'\n\s*\n', normalized.strip()) if normalized.strip() else []
    blocks: list[str] = []
    for piece in pieces:
        stripped = piece.lstrip()
        if '-->' in piece or stripped.startswith(('WEBVTT', 'NOTE', 'STYLE', 'REGION')):
            blocks.append(piece)
        elif blocks and '-->' in blocks[-1]:
            # Une seconde réplique (ou continuation) sans horodatage propre.
            blocks[-1] += '\n\n' + piece
        else:
            blocks.append(piece)
    return blocks


def parse_srt(content: str, split_dialogues: bool = False) -> list[Cue]:
    cues = []
    for block in subtitle_blocks(content):
        lines = block.split('\n')
        hit = next((i for i, line in enumerate(lines) if '-->' in line), None)
        if hit is None:
            continue
        first, sep, last = lines[hit].partition('-->')
        try:
            a = time_to_seconds(first.strip().split()[0])
            b = time_to_seconds(last.strip().split()[0])
        except (ValueError, IndexError):
            continue
        if b > a:
            cues.extend(_cues_from_caption(a, b, '\n'.join(lines[hit+1:]),
                                            split_dialogues=split_dialogues))
    return cues


def parse_vtt(content: str, split_dialogues: bool = False) -> list[Cue]:
    return parse_srt(content, split_dialogues=split_dialogues)


def parse_ass(content: str, split_dialogues: bool = False) -> list[Cue]:
    section = ''
    fields = []
    cues = []
    for line in content.lstrip('\ufeff').splitlines():
        line = line.strip()
        if line.startswith('['):
            section = line.lower()
        if section != '[events]':
            continue
        if line.lower().startswith('format:'):
            fields = [f.strip().lower() for f in line.split(':', 1)[1].split(',')]
        if not line.lower().startswith('dialogue:'):
            continue
        if not fields:
            fields = ['layer', 'start', 'end', 'style', 'name', 'marginl',
                      'marginr', 'marginv', 'effect', 'text']
        vals = line.split(':', 1)[1].lstrip().split(',', len(fields)-1)
        if len(vals) < len(fields):
            continue
        d = dict(zip(fields, vals))
        try:
            a, b = time_to_seconds(d['start']), time_to_seconds(d['end'])
        except (ValueError, KeyError):
            continue
        if b > a:
            cues.extend(_cues_from_caption(a, b, d.get('text', ''),
                          d.get('style', 'Default').strip(), split_dialogues=split_dialogues,
                          source_format='ass'))
    return cues


def extract_text(path: Path, codec: str, split_dialogues: bool = False) -> list[Cue]:
    data = path.read_bytes()
    try:
        value = data.decode('utf-8-sig')
    except UnicodeDecodeError:
        try:
            value = data.decode('utf-16')
        except UnicodeDecodeError:
            value = data.decode('cp1252', errors='replace')
    if codec in ('ass', 'ssa'):
        return parse_ass(value, split_dialogues=split_dialogues)
    if codec == 'webvtt':
        return parse_vtt(value, split_dialogues=split_dialogues)
    return parse_srt(value, split_dialogues=split_dialogues)


def organize_cues(cues: list[Cue], start: float, end: float) -> list[Cue]:
    """Répartit les répliques DETX sur quatre pistes sans en masquer une.

    Un duo possède deux blocs XML indépendants, même début/fin, en pistes 0/1
    si elles sont disponibles ; si un autre duo chevauche déjà le premier,
    les deux autres pistes 2/3 servent au second duo. Aucun dialogue ne doit
    être écrasé par un autre dialogue simultané sur la même piste.
    """
    selected = [Cue(max(0, c.start-start), min(end-start, c.end-start),
                    c.text, c.style, forced_track=c.forced_track,
                    source_format=c.source_format)
                for c in cues if c.end > start and c.start < end and c.text.strip()]
    selected = [c for c in selected if c.end > c.start and c.text]
    selected.sort(key=lambda c: (c.start, c.end))
    merged: list[Cue] = []
    for cue in selected:
        # Le regroupement par mêmes horaires/style ne concerne que les
        # événements ASS/SSA distincts (pas deux sous-titres SRT/VTT).
        matching = next((prior for prior in merged
                         if cue.source_format == 'ass' and prior.source_format == 'ass'
                         and cue.forced_track is None and prior.forced_track is None
                         and cue.style == prior.style
                         and abs(prior.start - cue.start) < .011
                         and abs(prior.end - cue.end) < .011), None)
        if matching is not None:
            if cue.text != matching.text:
                matching.text += ' ' + cue.text
        else:
            merged.append(cue)

    styles = list(dict.fromkeys(c.style for c in merged))
    style_preferred = {name: min(idx, 3) for idx, name in enumerate(styles)}
    occupied: list[list[Cue]] = [[] for _ in range(4)]
    placed: set[int] = set()

    def free(track: int, cue: Cue) -> bool:
        return not any(other.start < cue.end and other.end > cue.start
                       for other in occupied[track])

    def assign(index: int, track: int):
        cue = merged[index]
        cue.track = track
        occupied[track].append(cue)
        placed.add(index)

    # Réserver d'abord la place des duos, pour garantir les pistes 0/1 lorsque
    # possible et permettre un deuxième duo simultané sur les pistes 2/3.
    for index, cue in enumerate(merged):
        if index in placed or cue.forced_track != 0:
            continue
        partner = next((j for j in range(index + 1, len(merged))
                        if j not in placed and merged[j].forced_track == 1
                        and merged[j].style == cue.style
                        and abs(merged[j].start - cue.start) < .011
                        and abs(merged[j].end - cue.end) < .011), None)
        if partner is None:
            continue
        preferences = [(0, 1), (2, 3), (0, 2), (1, 3),
                       (1, 2), (0, 3), (2, 0), (3, 1)]
        pair = next((a_b for a_b in preferences
                     if free(a_b[0], cue) and free(a_b[1], merged[partner])), None)
        if pair is None:
            raise EasyCapError('DETX : plus de quatre dialogues simultanés ; '
                               'impossible de conserver toutes les répliques sans superposition.')
        assign(index, pair[0])
        assign(partner, pair[1])

    for index, cue in enumerate(merged):
        if index in placed:
            continue
        preference = cue.forced_track if cue.forced_track is not None else style_preferred[cue.style]
        available = [i for i in range(4) if free(i, cue)]
        if not available:
            raise EasyCapError('DETX : plus de quatre sous-titres qui se chevauchent ; '
                               'impossible de les afficher sur quatre pistes sans perte.')
        assign(index, preference if preference in available else available[0])
    return merged


def write_srt(cues: list[Cue], path: Path):
    body = '\n\n'.join(f'{idx}\n{srt_time(c.start)} --> {srt_time(c.end)}\n{c.text}'
                       for idx, c in enumerate(cues, 1))
    path.write_text(body + ('\n' if body else ''), encoding='utf-8-sig')


def write_vtt(cues: list[Cue], path: Path):
    body = '\n\n'.join(f'{fmt_time(c.start, True)} --> {fmt_time(c.end, True)}\n{c.text}'
                       for c in cues)
    path.write_text('WEBVTT\n\n' + body + '\n', encoding='utf-8')


def prepare_original_text_for_burn(source: Path, destination: Path,
                                   start: float, end: float, fmt: str) -> int:
    """Découpe/recale la VOSTFR sans nettoyer ou fusionner le texte original.

    Conserve l'italique, les balises de la source, les tirets et les retours
    à la ligne des SRT/VTT. À la différence du DETX, aucune normalisation
    de dialogue n'intervient ici. Les seuls champs réécrits sont les horaires
    (nécessaires pour synchroniser la scène extraite).
    """
    if fmt not in ('srt', 'vtt'):
        raise ValueError(f'Format texte pour incrustation non pris en charge : {fmt}')
    raw = source.read_text(encoding='utf-8-sig', errors='replace')
    raw = raw.replace('\r\n', '\n').replace('\r', '\n')
    blocks = subtitle_blocks(raw)
    result: list[str] = []
    preserved_headers: list[str] = []
    if fmt == 'vtt':
        preserved_headers.append('WEBVTT')
    kept = 0
    for block in blocks:
        lines = block.split('\n')
        stripped = block.lstrip()
        if fmt == 'vtt' and (stripped.startswith(('NOTE', 'STYLE', 'REGION'))):
            # Garder les métadonnées utiles au rendu VTT lorsque disponibles.
            preserved_headers.append(block)
            continue
        pos = next((i for i, line in enumerate(lines) if '-->' in line), None)
        if pos is None:
            continue
        left, _, right = lines[pos].partition('-->')
        start_piece = left.strip().split()
        end_piece = right.strip().split(maxsplit=1)
        if not start_piece or not end_piece:
            continue
        try:
            a, b = time_to_seconds(start_piece[0]), time_to_seconds(end_piece[0])
        except ValueError:
            continue
        if b <= start or a >= end:
            continue
        a, b = max(0., a-start), min(end-start, b-start)
        if b <= a:
            continue
        if fmt == 'vtt':
            timecode = lambda t: fmt_time(t, True)
            extras = f' {end_piece[1]}' if len(end_piece) > 1 else ''
            # Les identifiants de cue et paramètres de placement sont conservés.
            prefix = lines[:pos]
        else:
            timecode = srt_time
            extras = ''
            prefix = [str(kept + 1)]
        # Aucun changement aux lignes de texte, balises et sauts de ligne.
        result.append('\n'.join(prefix + [f'{timecode(a)} --> {timecode(b)}{extras}']
                                + lines[pos+1:]))
        kept += 1
    destination.write_text('\n\n'.join(preserved_headers + result) + '\n',
                           encoding='utf-8-sig' if fmt == 'srt' else 'utf-8')
    return kept


def subtitle_profile_height(selected_height: int, source_height: int) -> int:
    """Profil d’export vidéo (les sources <576p restent à leur résolution)."""
    effective = min(selected_height, source_height)
    if effective > 720:
        return 1080
    if effective > 576:
        return 720
    return 576


def vo_subtitle_font_size(selected_height: int, source_height: int) -> int:
    return VOSTFR_TEXT_FONTS[subtitle_profile_height(selected_height, source_height)]

def target_video_dimensions(video: dict, selected_height: int,
                            crop: str | None = None) -> tuple[int, int]:
    """Dimensions après crop et scale, à deux pixels d'arrondi près."""
    width = int(video.get('width') or 1920)
    height = int(video.get('height') or 1080)
    if crop:
        m = re.fullmatch(r'crop=(\d+):(\d+):(\d+):(\d+)', crop)
        if m:
            width, height = int(m[1]), int(m[2])
    cap = {576: 1024, 720: 1280, 1080: 1920}[selected_height]
    factor = min(1.0, cap / width, selected_height / height)
    return max(2, 2 * int(width * factor / 2)), max(2, 2 * int(height * factor / 2))


def write_ass(cues: list[Cue], path: Path, style: str = 'basic',
              font_size: int = VOSTFR_TEXT_FONT_SIZE, play_res_y: int = 1080):
    """ASS minimal utilitaire ; la VO/VOSTFR utilise force_style sur le SRT/VTT source."""
    if style == 'burn':
        header = (f'[Script Info]\nScriptType: v4.00+\nPlayResX: {int(play_res_y*16/9)}\n'
                  f'PlayResY: {play_res_y}\nWrapStyle: 0\nScaledBorderAndShadow: yes\n\n'
                  '[V4+ Styles]\n'
                  'Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n'
                  f'Style: Default,Arial,{font_size},&H00FFFFFF,&H000000FF,&H00000000,&H99000000,0,0,0,0,100,100,0,0,1,{VOSTFR_TEXT_OUTLINE:g},{VOSTFR_TEXT_SHADOW:g},2,30,30,{VOSTFR_TEXT_MARGIN_V},1\n\n')
    else:
        header = ('[Script Info]\nScriptType: v4.00+\nPlayResX: 1920\nPlayResY: 1080\n\n'
                  '[V4+ Styles]\n'
                  'Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n'
                  'Style: Default,Arial,45,&H00FFFFFF,&H000000FF,&H00000000,&H99000000,0,0,0,0,100,100,0,0,1,2,0,2,40,40,35,1\n\n')
    body = ('[Events]\n'
            'Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n')
    for cue in cues:
        body += (f'Dialogue: 0,{ass_time(cue.start)},{ass_time(cue.end)},Default,,'
                 f'0,0,0,,{cue.text}\n')
    path.write_text(header+body, encoding='utf-8-sig')


def detx_time(seconds: float, fps: float = DETX_FPS) -> str:
    """Origine Cappella : +1 heure, exactement 01:00:00:00 pour t=0."""
    nominal_fps = max(1, int(round(fps if fps > 0 else DETX_FPS)))
    return frame_time(seconds, fps, offset_frames=DETX_START_HOUR * 3600 * nominal_fps)


def write_detx(cues: list[Cue], path: Path, fps: float = DETX_FPS,
               video_path: str | Path | None = None,
               audio_path: str | Path | None = None):
    # Décalage Cappella : +1 heure sans image additionnelle pour les lèvres.
    # Le curseur Cappella revient TOUJOURS à 01:00:00:00 au chargement.
    # La hiérarchie et les attributs sont calqués sur les exemples BR.detx/BR.srt fournis.
    path = Path(path)
    video = Path(video_path) if video_path is not None else path.parent / 'Cappella_Video.mp4'
    audio = Path(audio_path) if audio_path is not None else path.parent / 'Cappella_Voix.mp3'
    root = ET.Element('detx', {'copyright': 'Chinkel S.A., 2007 - 2016'})
    header = ET.SubElement(root, 'header')
    ET.SubElement(header, 'cappella', {'version': '3.7.0'})
    ET.SubElement(header, 'videofile', {'timestamp': '01:00:00:00'}).text = str(video.expanduser().resolve())
    ET.SubElement(header, 'audiofile').text = str(audio.expanduser().resolve())
    ET.SubElement(header, 'last_position', {'timecode': '01:00:00:00', 'track': '0'})
    roles = ET.SubElement(root, 'roles')
    ET.SubElement(roles, 'role', {'color': '#000000', 'description': 'subtitle',
                                  'id': 'sub', 'name': 'sub'})
    body = ET.SubElement(root, 'body')
    for cue in cues:
        line = ET.SubElement(body, 'line', {'role': 'sub', 'track': str(cue.track)})
        ET.SubElement(line, 'lipsync', {'timecode': detx_time(cue.start, fps),
                                        'type': 'in_open'})
        ET.SubElement(line, 'text').text = cue.text
        ET.SubElement(line, 'lipsync', {'timecode': detx_time(cue.end, fps),
                                        'type': 'out_open'})
    ET.indent(root, space='  ')
    data = ET.tostring(root, encoding='utf-8', xml_declaration=True)
    path.write_bytes(data.replace(b' />', b'/>').replace(b'\n', b'\r\n'))


def _scaled_ass_inline_font_size(text: str, factor: float) -> str:
    """Double les overrides ASS \fsN uniquement, pas \fscx ni \fscy."""
    def scale(match):
        size = float(match.group(1)) * factor
        return r'\fs' + f'{size:g}'
    return re.sub(r'\\fs([0-9]+(?:\.[0-9]+)?)(?![0-9.])', scale, text)


def prepare_ass_original_for_burn(original: Path, dest: Path, start: float, end: float,
                                  font_scale: float = 1.0):
    """Styles ASS et overrides inchangés, seuls les dialogues sont recalés."""
    raw = original.read_text(encoding='utf-8-sig', errors='replace')
    result: list[str] = []
    section = ''
    event_fields: list[str] = []
    style_fields: list[str] = []
    for line in raw.splitlines():
        if line.strip().startswith('['):
            section = line.strip().lower()
        if line.lower().startswith('format:'):
            parsed = [v.strip().lower() for v in line.split(':', 1)[1].split(',')]
            if section == '[events]':
                event_fields = parsed
            elif section in ('[v4+ styles]', '[v4 styles]'):
                style_fields = parsed
        if font_scale != 1 and section in ('[v4+ styles]', '[v4 styles]') and line.lower().startswith('style:'):
            if style_fields and 'fontsize' in style_fields:
                values = line.split(':', 1)[1].lstrip().split(',', len(style_fields)-1)
                if len(values) == len(style_fields):
                    index = style_fields.index('fontsize')
                    try:
                        values[index] = f'{float(values[index])*font_scale:g}'
                        line = 'Style: ' + ','.join(values)
                    except ValueError:
                        pass
        if section == '[events]' and line.lower().startswith('dialogue:'):
            if not event_fields:
                event_fields = ['layer', 'start', 'end', 'style', 'name', 'marginl',
                                'marginr', 'marginv', 'effect', 'text']
            values = line.split(':', 1)[1].lstrip().split(',', len(event_fields)-1)
            if len(values) < len(event_fields):
                continue
            try:
                a = time_to_seconds(values[event_fields.index('start')])
                b = time_to_seconds(values[event_fields.index('end')])
            except (ValueError, IndexError):
                continue
            if b <= start or a >= end:
                continue
            values[event_fields.index('start')] = ass_time(max(0, a-start))
            values[event_fields.index('end')] = ass_time(min(end-start, b-start))
            if 'text' in event_fields:
                text_idx = event_fields.index('text')
                if font_scale != 1:
                    values[text_idx] = _scaled_ass_inline_font_size(values[text_idx], font_scale)
            line = 'Dialogue: ' + ','.join(values)
        result.append(line)
    dest.write_text('\n'.join(result)+'\n', encoding='utf-8-sig')


def vo_bitrate_kbps(selected_height: int, source_height: int) -> int:
    """Débit en fonction de la résolution effectivement atteignable sans upscale."""
    effective_height = min(selected_height, source_height)
    return 3000 if effective_height > 720 else (2000 if effective_height > 576 else 1500)


def subtitle_container(codec: str) -> tuple[str, str]:
    if codec in ('ass', 'ssa'):
        return 'ass', 'ass'
    if codec == 'webvtt':
        return 'vtt', 'webvtt'
    return 'srt', 'srt'


def audio_extension(codec: str) -> str:
    return {'aac':'m4a', 'alac':'m4a', 'mp3':'mp3', 'mp2':'mp2',
            'ac3':'ac3', 'eac3':'eac3', 'dts':'dts', 'truehd':'thd',
            'flac':'flac', 'opus':'opus', 'vorbis':'ogg',
            'pcm_s16le':'wav', 'pcm_s24le':'wav', 'pcm_s32le':'wav',
            'pcm_f32le':'wav', 'pcm_f64le':'wav'}.get(codec, 'mka')


def audio_encoder(codec: str, stream: dict) -> tuple[list[str], str]:
    """Pour l'automatique: codec proche source pour audio avec pertes."""
    br = int(stream.get('bit_rate') or 256000)
    if codec == 'aac':
        return ['-c:a', 'aac', '-b:a', f'{min(768000,max(128000,br))}'], 'm4a'
    if codec == 'ac3':
        return ['-c:a', 'ac3', '-b:a', f'{min(640000,max(192000,br))}'], 'ac3'
    if codec == 'opus':
        return ['-c:a', 'libopus', '-b:a', f'{min(512000,max(96000,br))}'], 'opus'
    if codec == 'vorbis':
        return ['-c:a', 'libvorbis', '-q:a', '5'], 'ogg'
    if codec == 'mp2':
        return ['-c:a', 'mp2', '-b:a', '320k'], 'mp2'
    return ['-c:a', 'flac', '-sample_fmt', 's16'], 'flac'


def pan_filter(channels: int, layout: str = '') -> str:
    if channels >= 3:
        # Ordre FFmpeg standard pour les mixages 3.0/5.1/7.1 : FL,FR,FC,...
        if channels > 8:
            raise EasyCapError('Les configurations audio de plus de 8 canaux ne sont pas prises en charge.')
        vals = '|'.join(f'c{i}=' + ('0*c2' if i == 2 else f'c{i}') for i in range(channels))
        supported_layout = layout if re.fullmatch(r'[A-Za-z0-9.()_]+', layout) else ''
        return f'pan={supported_layout or str(channels) + "c"}|{vals}'
    if channels == 2:
        return 'pan=stereo|c0=0.5*c0-0.5*c1|c1=0.5*c1-0.5*c0'
    # Pas de canal central isolable en mono : atténuation très approximative
    # d'une bande vocale, jamais une vraie séparation.
    return 'equalizer=f=1200:t=o:w=2:g=-24'


def stereo_voice_filter(preset: str = 'standard', sample_rate: int = 48000) -> str:
    """Filtre stéréo avec préréglages à paramètres fixes (sans puissance utilisateur).

    L'annulation du signal partagé (centre fantôme) reste approximative :
    les voix décentrées ou leurs queues de réverbération peuvent subsister.
    Les profils « pièce » ne réduisent le centre que dans la bande vocale.
    """
    if preset not in STEREO_PRESETS:
        raise ValueError('Préréglage stéréo invalide.')
    if not isinstance(sample_rate, int) or not 8000 <= sample_rate <= 768000:
        raise ValueError('Fréquence d’échantillonnage audio invalide.')
    profile = STEREO_PRESETS[preset]
    p = profile['strength'] / 100.0
    same, opposite = 1.0 - p / 2.0, -p / 2.0
    matrix = (f'pan=stereo|c0={same:.6f}*c0{opposite:+.6f}*c1'
              f'|c1={same:.6f}*c1{opposite:+.6f}*c0')
    if profile['low'] is None:
        return pan_filter(2)  # Filtre classique, identique aux versions précédant les préréglages.
    # Reste dans le domaine spectral si la piste est à 8 kHz, par exemple.
    low = min(profile['low'], int(sample_rate * 0.20))
    high = min(profile['high'], int(sample_rate * 0.45))
    return (f'acrossover=split={low} {high}:order=4th[ec_low][ec_mid][ec_high];'
            f'[ec_mid]{matrix}[ec_reduced];'
            '[ec_low][ec_reduced][ec_high]amix=inputs=3:normalize=0')



def source_depth(stream: dict) -> int:
    if stream.get('codec_name') == 'eac3':
        return 16
    for attr in ('bits_per_raw_sample', 'bits_per_sample'):
        try:
            bits = int(stream.get(attr) or 0)
            if bits:
                return 16 if bits <= 16 else 24
        except ValueError:
            pass
    return 24 if stream.get('codec_name') in ('truehd', 'dts', 'flac', 'alac') else 16


def _parse_ffprobe_packet_data(dump: str) -> str:
    """Décode les octets d'un paquet SubRip affichés par ffprobe -show_data.

    Le tableau hexadécimal est source de vérité ; la colonne ASCII n'est
    volontairement pas utilisée (accents UTF-8 et caractères non imprimables).
    """
    data = bytearray()
    for line in dump.splitlines():
        match = re.match(r'^\s*[0-9a-fA-F]{8}:\s*(.*)$', line)
        if not match:
            continue
        hexpart = match.group(1).split('  ', 1)[0]
        raw = ''.join(hexpart.split())
        if raw:
            data.extend(bytes.fromhex(raw))
    return data.decode('utf-8-sig', errors='replace')


def subtitle_window(settings: Settings) -> tuple[float, float, float, float]:
    """Extrait une fenêtre bornée et retourne seek, durée et limites relatives.

    Les sous-titres sont remis à t=0 par FFmpeg lors du seek d'entrée.
    Une marge préserve ceux commencés juste avant la scène.
    """
    seek = max(0.0, settings.start - SUBTITLE_PREROLL)
    duration = max(0.01, settings.end - seek + 0.5)
    return seek, duration, settings.start - seek, settings.end - seek


def video_stall_timeout(first_pass_elapsed: float | None = None) -> float:
    """A second x264 pass can initialise slowly; 120 s is not a hang proof.

    Duration is measured in real seconds. Long first passes raise the safety
    threshold so an unusually slow 4K machine is not prematurely stopped.
    """
    return min(3600.0, max(600.0, (first_pass_elapsed or 0.0) * 2.5))


class Engine:
    def __init__(self, log: Callable[[str], None] | None = None,
                 progress: Callable[[float, str], None] | None = None):
        self._journal: list[str] = []
        sink = log or (lambda msg: None)
        def record(message: str):
            self._journal.append(message)
            sink(message)
        self.log = record
        self.progress = progress or (lambda fraction, detail: None)
        self.cancel_flag = threading.Event()
        self._current: subprocess.Popen | None = None
        self._subtitle_cache: dict[int, tuple[Path, str, str]] = {}

    def cancel(self):
        self.cancel_flag.set()
        if self._current and self._current.poll() is None:
            self._current.kill()

    def _check(self):
        if self.cancel_flag.is_set():
            raise Cancelled('Opération annulée.')

    def ffmpeg(self, args: list[str], label: str, seconds: float = 0,
               progress_span: tuple[float, float] = (0, 0),
               stall_timeout: float | None = None) -> float:
        """Execute FFmpeg, tracking timestamp/frame progress and diagnostic silence.

        Only prolonged silence is treated as a suspected hang. A slow x264
        second pass is given significantly longer than 120 seconds.
        Returns wall-clock elapsed seconds for adaptive stage supervision.
        """
        self._check()
        cmd = [binary('ffmpeg'), '-hide_banner', '-nostdin', '-y', '-loglevel', 'warning',
               '-stats_period', '1', '-progress', 'pipe:1'] + [str(x) for x in args]
        self.log(f'▶ {label}')
        stage_start = time.monotonic()
        last_activity = [stage_start]
        last_forward = [stage_start]
        last_out_seconds = [-1.0]
        last_frame = [-1]
        killed_for_stall = [False]
        done_monitor = threading.Event()
        subtitle_job = label.startswith('Extraction des sous-titres')
        # Subtitles can be silent for minutes. Bound their total scan time, not
        # the gaps between events. Everything else uses an adaptive limit.
        subtitle_deadline = min(1200.0, max(300.0, seconds * 5.0))
        timeout = (float(stall_timeout) if stall_timeout is not None
                   else (600.0 if 'passe' in label else 300.0))
        timeout = max(1.0, timeout)
        last_note = [0]

        # stderr is drained to a temporary file to avoid pipe deadlocks.
        with tempfile.TemporaryFile(mode='w+t', encoding='utf-8', errors='replace') as err:
            self._current = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                             stdout=subprocess.PIPE, stderr=err,
                                             text=True, encoding='utf-8', errors='replace')
            proc = self._current
            def watchdog():
                while not done_monitor.wait(5):
                    now = time.monotonic()
                    idle = now - last_forward[0]
                    total = now - stage_start
                    if not subtitle_job and idle >= 120 and int(idle // 60) > last_note[0]:
                        last_note[0] = int(idle // 60)
                        self.log(f'⚠ {label} : aucune image/timecode supplémentaire depuis {idle:.0f} s '
                                 f'(dernière image {last_frame[0]}, temps {max(0, last_out_seconds[0]):.1f} s). '
                                 'Traitement surveillé ; vous pouvez Annuler.')
                    timed_out = ((total > subtitle_deadline) if subtitle_job
                                 else (idle > timeout))
                    if timed_out and proc.poll() is None:
                        killed_for_stall[0] = True
                        proc.kill()
                        return
            guard = threading.Thread(target=watchdog, daemon=True)
            guard.start()
            try:
                assert proc.stdout is not None
                for line in proc.stdout:
                    self._check()
                    stripped = line.strip()
                    if '=' not in stripped:
                        continue
                    key, value = stripped.split('=', 1)
                    if key in ('progress', 'frame', 'out_time_us', 'out_time_ms', 'out_time'):
                        last_activity[0] = time.monotonic()
                    if key == 'frame':
                        try:
                            frame = int(value)
                            if frame > last_frame[0]:
                                last_frame[0] = frame
                                last_forward[0] = time.monotonic()
                        except ValueError:
                            pass
                    if key in ('out_time_us', 'out_time_ms') and seconds > 0:
                        try:
                            elapsed = max(0., int(value) / 1000000.)
                            if elapsed > last_out_seconds[0]:
                                last_out_seconds[0] = elapsed
                                last_forward[0] = time.monotonic()
                            a, b = progress_span
                            if b > a:
                                self.progress(min(b, a + (b-a)*min(1., elapsed/seconds)), label)
                        except ValueError:
                            pass
                ret = proc.wait()
            finally:
                if proc.stdout:
                    proc.stdout.close()
                done_monitor.set()
                self._current = None
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
            if self.cancel_flag.is_set():
                raise Cancelled('Opération annulée.')
            if killed_for_stall[0]:
                self.log('Commande FFmpeg interrompue : ' + subprocess.list2cmdline(cmd))
                err.seek(0)
                detail = err.read()[-4000:]
                if subtitle_job:
                    raise EasyCapError(
                        f'Extraction des sous-titres trop longue ({label}, limite {subtitle_deadline:.0f} s). '
                        f'Informations FFmpeg :\n{detail}')
                raise EasyCapError(
                    f'FFmpeg sans nouvelle image/timecode pendant plus de {timeout:.0f} s ({label}). '
                    f'Dernière image {last_frame[0]}, dernier timecode {max(0, last_out_seconds[0]):.1f} s.\n'
                    f'Informations FFmpeg :\n{detail}')
            if ret:
                err.seek(0)
                detail = err.read()[-5000:]
                self.log('Commande FFmpeg échouée : ' + subprocess.list2cmdline(cmd))
                raise EasyCapError(f'Échec FFmpeg ({label}) :\n{detail or "code " + str(ret)}')
            runtime = time.monotonic()-stage_start
            self.log(f'✓ {label} : {runtime:.1f} s réelles')
            return runtime

    def detect_crop(self, s: Settings) -> str | None:
        """Détection par échantillons, vote majoritaire, rejet de coupes extrêmes."""
        video = s.media.video
        w, h = int(video.get('width', 0)), int(video.get('height', 0))
        if not w or not h:
            return None
        duration = s.end-s.start
        candidates: list[tuple[int, int, int, int]] = []
        for fraction in (.20, .55, .80):
            self._check()
            sample = min(0.9, duration)
            pos = s.start + max(0, duration*fraction - sample/2)
            cmd = [binary('ffmpeg'), '-hide_banner', '-nostdin', '-ss', f'{pos:.3f}',
                   '-i', str(s.media.path), '-t', f'{sample:.2f}',
                   '-map', f"0:{video['index']}", '-vf', 'fps=6,cropdetect=20:16:0',
                   '-an', '-f', 'null', '-']
            p = subprocess.run(cmd, capture_output=True, text=True, errors='replace',
                               stdin=subprocess.DEVNULL)
            for cw, ch, x, y in re.findall(r'crop=(\d+):(\d+):(\d+):(\d+)', p.stderr):
                cw, ch, x, y = map(int, (cw, ch, x, y))
                if cw <= w and ch <= h and cw >= w*.55 and ch >= h*.55:
                    candidates.append((cw, ch, x, y))
        if not candidates:
            return None
        # Favorise les crops récurrents et conserve l'image sans recadrage
        # si la détection n'est pas suffisamment cohérente.
        common, occurrences = Counter(candidates).most_common(1)[0]
        if occurrences < max(2, int(len(candidates)*.30)):
            self.log('Recadrage non fiable : conservation du cadre original.')
            return None
        cw, ch, x, y = common
        if (w-cw)/w < .035 and (h-ch)/h < .035:
            return None
        if abs(2*x - (w-cw)) > 32 or abs(2*y - (h-ch)) > 32:
            self.log('Recadrage asymétrique incertain : conservation du cadre original.')
            return None
        self.log(f'Barres noires détectées : crop={cw}:{ch}:{x}:{y}')
        return f'crop={cw}:{ch}:{x}:{y}'

    def video_filter(self, s: Settings, height: int, crop: str | None,
                     skip_hdr: bool = False) -> str:
        filters = []
        if crop:
            filters.append(crop)
        if s.media.hdr and not skip_hdr:
            filters += ['zscale=t=linear:npl=100', 'format=gbrpf32le',
                        'tonemap=mobius:desat=0',
                        'zscale=t=bt709:p=bt709:m=bt709:r=tv', 'format=yuv420p']
        maxwidth = {576: 1024, 720: 1280, 1080: 1920}[height]
        filters += [f'scale=w=\'min(iw,{maxwidth})\':h=\'min(ih,{height})\':'
                    'force_original_aspect_ratio=decrease:force_divisible_by=2:flags=lanczos',
                    'setsar=1', 'format=yuv420p']
        return ','.join(filters)

    def extract_subrip_packets(self, s: Settings, idx: int, target: Path) -> bool:
        """Extraction rapide SubRip par packets/PTS, sans réencodage FFmpeg.

        ffprobe s'arrête aux bornes de lecture plutôt que de devoir attendre
        le prochain paquet sparse du décodeur de sous-titres FFmpeg. Repli
        automatique sur FFmpeg en cas de données temporelles non exploitables.
        """
        seek, duration, _begin, _end = subtitle_window(s)
        video_start = float(s.media.video.get('start_time') or 0.0)
        interval_start = seek + video_start
        interval_end = seek + duration + video_start
        self._check()
        command = [binary('ffprobe'), '-v', 'error', '-select_streams', str(idx),
                   '-read_intervals', f'{interval_start:.3f}%{interval_end:.3f}',
                   '-show_packets', '-show_data',
                   '-show_entries', 'packet=pts_time,duration_time,data',
                   '-of', 'json', str(s.media.path)]
        try:
            # Même pour ffprobe, le bouton Annuler doit rester réactif.
            started = time.monotonic()
            proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, errors='replace', stdin=subprocess.DEVNULL)
            self._current = proc
            try:
                while True:
                    self._check()
                    try:
                        stdout, stderr = proc.communicate(timeout=0.5)
                        break
                    except subprocess.TimeoutExpired:
                        if time.monotonic() - started > 90:
                            raise subprocess.TimeoutExpired(command, 90)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.communicate()
                self._current = None
            self._check()
            if proc.returncode:
                raise ValueError((stderr or '')[-500:])
            packets = json.loads(stdout).get('packets', [])
            cues: list[Cue] = []
            for pkt in packets:
                if 'pts_time' not in pkt or 'duration_time' not in pkt:
                    raise ValueError('Paquet sans PTS/durée')
                start = float(pkt['pts_time']) - video_start
                end = start + float(pkt['duration_time'])
                if not math.isfinite(start) or not math.isfinite(end):
                    raise ValueError('Horodatage non fini')
                if end <= start or start >= interval_end - video_start or end <= seek:
                    continue
                text = _parse_ffprobe_packet_data(pkt.get('data', ''))
                if not text and pkt.get('data'):
                    raise ValueError('Paquet non décodable')
                if text.strip():
                    cues.append(Cue(max(0.0,start-seek), max(0.0,end-seek), text))
            # Le contenu natif de l'extraction est réutilisé pour SRT et DETX.
            write_srt(cues, target)
            self.log(f'SRT #{idx} : {len(cues)} sous-titre(s) extrait(s) via ffprobe, sans décoder toute la vidéo.')
            return True
        except (OSError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
            self.log(f'Lecture rapide SRT indisponible ({exc}) ; utilisation FFmpeg sur la fenêtre bornée.')
            return False

    def extract_sub(self, s: Settings, idx: int, folder: Path, prefix: str) -> tuple[Path, str, str]:
        """Extrait seulement la fenêtre autour de la scène, jamais le film entier."""
        cached = self._subtitle_cache.get(idx)
        if cached and cached[0].is_file():
            self.log(f'Sous-titres #{idx} : extraction déjà disponible (réutilisation DETX/VOSTFR).')
            return cached
        stream = next(x for x in s.media.subtitles if x['index'] == idx)
        codec = stream.get('codec_name', '')
        if codec not in TEXT_CODECS:
            raise EasyCapError(f'La piste #{idx} ({codec}) n’est pas textuelle.')
        ext, encoder = subtitle_container(codec)
        target = folder / f'{prefix}.{ext}'
        seek, duration, _begin, _end = subtitle_window(s)
        self.log(f'Sous-titres #{idx} ({codec}) : lecture limitée à '
                 f'{fmt_time(seek, True)} → {fmt_time(seek + duration, True)} '
                 '(avec marge avant la scène).')
        # Sur les flux SubRip, la lecture directe des paquets évite le risque
        # des sous-titres espacés sans progrès FFmpeg, sur de très gros MKV.
        if codec in ('subrip', 'srt') and s.media.path.is_file():
            if self.extract_subrip_packets(s, idx, target):
                self._subtitle_cache[idx] = (target, codec, ext)
                return target, codec, ext
        # ASS/SSA : préférer une copie bit-à-bit des paquets et de l'en-tête
        # de style ; certains réencodeurs détruisent des styles / positions
        # personnalisées. Si le muxer ne permet pas la copie, repli explicite.
        if codec in ('ass', 'ssa'):
            copy_args = ['-ss', f'{seek:.3f}', '-t', f'{duration:.3f}',
                         '-i', str(s.media.path), '-map', f'0:{idx}',
                         '-an', '-vn', '-dn', '-c:s', 'copy', str(target)]
            try:
                self.ffmpeg(copy_args, f'Extraction ASS/SSA native #{idx}', duration)
                self.log('ASS/SSA : styles et textes copiés sans réencodage des sous-titres.')
                self._subtitle_cache[idx] = (target, codec, ext)
                return target, codec, ext
            except Cancelled:
                raise
            except EasyCapError as exc:
                target.unlink(missing_ok=True)
                self.log(f'Copie ASS/SSA non prise en charge ({exc}) ; conversion FFmpeg de secours.')
        # -ss/-t AVANT -i : FFmpeg limite la lecture en entrée, même sans
        # événement de sous-titrage près de la fin. Évite de scanner 2 h de MKV.
        # L'encodeur de sous-titres réinitialise les timestamps relativement au seek.
        cmd = ['-ss', f'{seek:.3f}', '-t', f'{duration:.3f}', '-i', str(s.media.path),
               '-map', f'0:{idx}', '-an', '-vn', '-dn', '-c:s', encoder, str(target)]
        self.ffmpeg(cmd, f'Extraction des sous-titres #{idx}', duration)
        self._subtitle_cache[idx] = (target, codec, ext)
        return target, codec, ext

    def prepare_subtitles(self, s: Settings, output: Path, tmp: Path):
        if not s.convert_detx:
            return
        assert s.detx_sub_idx is not None
        self.log(f'Conversion DETX : piste textuelle #{s.detx_sub_idx}.')
        raw, codec, ext = self.extract_sub(s, s.detx_sub_idx, tmp, 'detx_source')
        _seek, _window, relative_start, relative_end = subtitle_window(s)
        # Le DETX seul reçoit le nettoyage, la réunion des lignes et les
        # changements de piste. La copie de référence, si créée, garde chaque
        # ligne originale et ses balises ; elle n'est JAMAIS utilisée pour la VOSTFR.
        detx_cues = organize_cues(extract_text(raw, codec, split_dialogues=True),
                                  relative_start, relative_end)
        if not detx_cues:
            self.log('Attention : aucun sous-titre dans la scène choisie.')
        if ext == 'ass':
            prepare_ass_original_for_burn(raw, output / 'Bande_Rythmo.ass',
                                          relative_start, relative_end)
        else:
            prepare_original_text_for_burn(raw, output / f'Bande_Rythmo.{ext}',
                                           relative_start, relative_end, ext)
        write_detx(detx_cues, output / 'Bande_Rythmo.detx', DETX_FPS,
                   video_path=output / 'Cappella_Video.mp4',
                   audio_path=output / 'Cappella_Voix.mp3')
        doubles = sum(c.forced_track == 1 for c in detx_cues)
        self.log('DETX : timecodes à 24 images/s, origine 01:00:00:00 (+1 heure, sans image supplémentaire).')
        self.log('DETX : liens absolus vers Cappella_Video.mp4 / Cappella_Voix.mp3 ; position initiale 01:00:00:00.')
        self.log(f'DETX : {len(detx_cues)} répliques, {max((c.track for c in detx_cues), default=0)+1} piste(s), '
                 f'{doubles} duo(s) séparé(s) sur pistes 0 et 1.')

    def prepare_vo_sub(self, s: Settings, tmp: Path,
                       crop: str | None = None) -> Path | None:
        if s.vo_sub_idx is None:
            return None
        codec = s._sub(s.vo_sub_idx).get('codec_name', '')
        if codec in {'hdmv_pgs_subtitle', 'dvd_subtitle'}:
            return None  # Les bitmap sont incrustés via la chaîne overlay FFmpeg.
        if codec not in TEXT_CODECS:
            raise EasyCapError('Sous-titres graphiques non pris en charge.')
        self.log(f'Incrustation VOSTFR textuelle : piste #{s.vo_sub_idx}.')
        raw, actual, ext = self.extract_sub(s, s.vo_sub_idx, tmp, 'vo_source')
        _seek, _window, relative_start, relative_end = subtitle_window(s)
        if ext == 'ass':
            original_ass = tmp / 'vo_burn.ass'
            prepare_ass_original_for_burn(raw, original_ass, relative_start, relative_end)
            self.log('ASS/SSA : texte, balises, retours à la ligne et styles originaux conservés ; seuls les horaires sont recalés.')
            return original_ass
        original_text = tmp / f'vo_burn.{ext}'
        count = prepare_original_text_for_burn(raw, original_text,
                                               relative_start, relative_end, ext)
        raw_h = int(s.media.video.get('height', 1080))
        profile = subtitle_profile_height(s.vo_height, raw_h)
        fontsize = vo_subtitle_font_size(s.vo_height, raw_h)
        self.log(f'SRT/VTT : {count} sous-titre(s) original(aux), sans nettoyage ou fusion ; '
                 f'police logique {fontsize} et contour fin ({VOSTFR_TEXT_OUTLINE}), '
                 f'identiques en 1080p/720p/576p (profil {profile}p).')
        return original_text

    @staticmethod
    def bitmap_geometry(s: Settings) -> tuple[int, int, int, int]:
        """Dimensions raster PGS/VobSub comparées au canevas vidéo source."""
        video = s.media.video
        sub = s._sub(s.vo_sub_idx)
        vw, vh = int(video.get('width') or 0), int(video.get('height') or 0)
        sw, sh = int(sub.get('width') or vw), int(sub.get('height') or vh)
        if not min(vw, vh, sw, sh):
            raise EasyCapError('Dimensions vidéo ou bitmap impossibles à déterminer.')
        return sw, sh, vw, vh

    def bitmap_subtitle_offset(self, s: Settings, crop: str | None) -> tuple[int, bool]:
        """Analyse non destructive de l'enveloppe des sous-titres bitmap.

        Le filtre bbox mesure les pixels de la couche PGS/DVD décodée par
        sub2video. Si la piste ne peut être inspectée, le crop sera désactivé
        pour cette exportation, afin de ne pas tronquer des sous-titres.
        Retour : (translation vers le haut en pixels, position vérifiée).
        """
        if not crop:
            return 0, True
        m = re.fullmatch(r'crop=(\d+):(\d+):(\d+):(\d+)', crop)
        if not m:
            return 0, False
        cw, ch, x, y = map(int, m.groups())
        if s.vo_sub_idx is None:
            return 0, True
        args = [binary('ffmpeg'), '-hide_banner', '-nostdin', '-ss', f'{s.start:.3f}',
                '-i', str(s.media.path), '-t', f'{s.end-s.start:.3f}',
                '-filter_complex', f'[0:{s.vo_sub_idx}]format=rgba,alphaextract,bbox=min_val=1[b]',
                '-map', '[b]', '-frames:v', '250', '-an', '-f', 'null', '-']
        self.log('PGS/VobSub : vérification de la zone occupée par les images de sous-titres…')
        try:
            # bbox émet ses coordonnées sur stderr avec loglevel info.
            p = subprocess.run(args, capture_output=True, text=True, errors='replace',
                               stdin=subprocess.DEVNULL, timeout=35)
            found = [(int(a), int(b)) for a, b in re.findall(
                r'\bx1:\d+\s+x2:\d+\s+y1:(\d+)\s+y2:(\d+)', p.stderr)]
            if p.returncode or not found:
                self.log('PGS/VobSub : positions non mesurables, recadrage désactivé pour préserver les sous-titres.')
                return 0, False
            sw, sh, vw, vh = self.bitmap_geometry(s)
            positions = [(round(a*vh/sh), round(b*vh/sh)) for a,b in found]
            visible_bottom = y + ch - 8
            bottom = max(b for a,b in positions)
            if bottom <= visible_bottom:
                self.log('PGS/VobSub : images entièrement dans la zone conservée ; aucune translation.')
                return 0, True
            move_up = bottom - visible_bottom
            top_after = min(a for a,b in positions) - move_up
            if top_after < y+5:
                self.log('PGS/VobSub : repositionnement insuffisant pour tout garder ; recadrage désactivé.')
                return 0, False
            self.log(f'PGS/VobSub : sous-titres atteints par le crop ; remontée globale de {move_up} px.')
            return move_up, True
        except (subprocess.TimeoutExpired, OSError) as exc:
            self.log(f'PGS/VobSub : analyse interrompue ({exc}) ; aucun crop sur VOSTFR.')
            return 0, False

    def preprocess_video(self, s: Settings, tmp: Path, crop: str | None,
                         height: int = 1080) -> Path:
        """Cache FFV1 : une seule lecture/décodage 4K/HDR, partagé par 4 passes.

        Temporaire, automatiquement supprimé. Les quatre passes finales
        décodent un fichier déjà recadré, SDR, au plus en 1080p.
        """
        cache = tmp / 'source_preparee_ffv1.mkv'
        duration = s.end - s.start
        args = ['-ss', f'{s.start:.3f}', '-i', str(s.media.path), '-t', f'{duration:.3f}',
                '-map', f"0:{s.media.video['index']}", '-an', '-sn',
                '-vf', self.video_filter(s, height, crop),
                '-c:v', 'ffv1', '-level', '3', '-slicecrc', '0',
                '-slices', '16', '-threads', '0', '-map_chapters', '-1', str(cache)]
        self.log('Optimisation 4K/HDR : création d’un cache vidéo temporaire lossless FFV1, SDR si nécessaire.')
        self.ffmpeg(args, 'Préparation vidéo partagée (cache FFV1)', duration, (.03,.18))
        return cache

    def _video_pass(self, s: Settings, output: Path, filter_chain: str, codec: str,
                    bitrate: int, label: str, tmp: Path,
                    audio_idx: int | None = None,
                    progress_range: tuple[float,float] = (0, .5),
                    video_source: Path | None = None,
                    bitmap_sub_idx: int | None = None,
                    bitmap_shift: int = 0):
        """Deux passes MPEG-4/x264. Compatible cache partagé et sous-titres bitmap."""
        duration = s.end - s.start
        if video_source is None:
            inp = ['-ss', f'{s.start:.3f}', '-i', str(s.media.path)]
            video_idx = s.media.video['index']
        else:
            # Le cache commence à t=0, seule l'audio vient de l'original.
            inp = ['-i', str(video_source)]
            video_idx = 0
        inp += ['-t', f'{duration:.3f}']
        # Cappella doit être en 24 i/s constants, sans changement de vitesse :
        # 'fps=24' conserve la timeline source (drop/dup), contrairement à
        # 'setpts=...' ou au réglage de fréquence en entrée (-r avant -i).
        # Les deux passes utilisent la même normalisation d'images.
        if codec == 'mpeg4':
            fps_ratio = str(CAPPELLA_FPS)
            filter_chain = f'{filter_chain},fps=fps={CAPPELLA_FPS}:round=near'
            self.log('Cappella : 24 images/s constantes, durée et vitesse de lecture inchangées (duplication/suppression des images si nécessaire).')
        else:
            fps_ratio = str(s.media.video.get('avg_frame_rate') or '25/1')
            if _float_ratio(fps_ratio, 0) <= 0:
                fps_ratio = str(s.media.video.get('r_frame_rate') or '25/1')
            if _float_ratio(fps_ratio, 0) <= 0:
                fps_ratio = f'{s.media.fps or 25:.6f}'
        settings = ['-r', fps_ratio, '-fps_mode', 'cfr',
                    '-c:v', codec, '-b:v', f'{bitrate}k', '-pix_fmt', 'yuv420p']
        if codec == 'libx264':
            settings += ['-preset', X264_PRESET]
            self.log(f'Encodage x264 : preset {X264_PRESET}, 2 passes ({bitrate} kb/s).')
        else:
            self.log(f'Encodage {codec} : 2 passes ({bitrate} kb/s).')
        if bitmap_sub_idx is None:
            vf_args = ['-map', f'0:{video_idx}', '-vf', filter_chain]
        else:
            # Le calque bitmap est placé aux coordonnées de l'image source,
            # AVANT le crop, puis les deux sont réduits ensemble.
            # Conversion implicite PGS/DVD -> RGBA documentée par FFmpeg.
            sw, sh, vw, vh = self.bitmap_geometry(s)
            align = (f',crop={sw}:{sh}:0:0,scale={vw}:{vh}:flags=bilinear'
                     if (sw, sh) != (vw, vh) else '')
            chain = (f'[0:{video_idx}]setpts=PTS-STARTPTS[v0];'
                     f'[0:{bitmap_sub_idx}]setpts=PTS-STARTPTS{align}[s0];'
                     f'[v0][s0]overlay=x=0:y=-{bitmap_shift}:shortest=0:'
                     f'eof_action=pass:format=auto,{filter_chain}[vout]')
            vf_args = ['-filter_complex', chain, '-map', '[vout]']
        # Keep both x264 passes strictly video-only: audio decoding (especially
        # TrueHD / Atmos) must not alter the filter graph or prevent the second
        # pass from advancing. AAC is produced separately, then stream-copied
        # into the finished MP4 without a third video encode.
        colors = (['-color_primaries', 'bt709', '-color_trc', 'bt709',
                   '-colorspace', 'bt709'] if s.media.hdr else [])
        passfile = tmp / ('pass_vostfr_x264' if codec == 'libx264' else 'pass_cappella_mpeg4')
        video_only = (tmp / 'scene_x264_video_seule.mp4'
                      if codec == 'libx264' and audio_idx is not None else output)
        if codec == 'libx264' and audio_idx is not None and output.exists():
            # Never leave a previous aborted scene masquerading as a fresh output.
            output.unlink()
        if codec == 'libx264' and audio_idx is not None:
            self.log('VO/VOSTFR : vidéo seule pour les deux passes x264 ; AAC séparé puis assemblage sans réencodage.')
        self.log('Vidéos : chapitres exclus des MP4 (-map_chapters -1).')
        elapsed_first = None
        for n in (1, 2):
            a, b = progress_range
            # Reserve last 10% of the VOSTFR task for AAC and lossless muxing.
            video_share = .90 if codec == 'libx264' and audio_idx is not None else 1.
            bounds = (a + (b-a)*video_share*(n-1)/2,
                      a + (b-a)*video_share*n/2)
            common = inp + vf_args + settings + ['-passlogfile', str(passfile), '-pass', str(n)]
            if n == 1:
                args = common + ['-an', '-sn', '-f', 'null', os.devnull]
            else:
                args = common + ['-an', '-sn'] + colors + ['-map_chapters', '-1',
                                                       '-movflags', '+faststart', str(video_only)]
            threshold = video_stall_timeout(elapsed_first)
            measured = self.ffmpeg(args, f'{label} — passe {n}/2', duration, bounds,
                                   stall_timeout=threshold)
            if n == 1:
                elapsed_first = float(measured) if isinstance(measured, (int, float)) else None
        if codec == 'libx264' and audio_idx is not None:
            audio = tmp / 'vo_audio_aac.m4a'
            # Decode only the chosen audio stream. Do not feed TrueHD to the
            # subtitle overlay or x264 two-pass graph.
            audio_args = ['-ss', f'{s.start:.3f}', '-i', str(s.media.path),
                          '-t', f'{duration:.3f}', '-map', f'0:{audio_idx}',
                          '-vn', '-sn', '-dn', '-c:a', 'aac', '-ac', '2',
                          '-b:a', '192k', '-ar', '48000', str(audio)]
            self.ffmpeg(audio_args, f'{label} — audio AAC indépendant', duration,
                        (progress_range[0] + (progress_range[1]-progress_range[0])*.90,
                         progress_range[0] + (progress_range[1]-progress_range[0])*.98),
                        stall_timeout=600)
            # Stream copy both prepared tracks; an incomplete mux must NEVER
            # appear under the final scene name. Keep the .partial on the same
            # filesystem as the output so replace() is atomic on Windows too.
            partial = output.with_name(output.stem + '_EasyCap_incomplet.mp4')
            partial.unlink(missing_ok=True)
            mux_args = ['-i', str(video_only), '-i', str(audio),
                        '-map', '0:v:0', '-map', '1:a:0', '-c:v', 'copy', '-c:a', 'copy',
                        '-map_chapters', '-1', '-movflags', '+faststart', '-shortest', str(partial)]
            try:
                self.ffmpeg(mux_args, f'{label} — assemblage MP4 sans réencodage', duration,
                            (progress_range[0] + (progress_range[1]-progress_range[0])*.98,
                             progress_range[1]), stall_timeout=300)
                if not partial.is_file() or partial.stat().st_size == 0:
                    raise EasyCapError('Assemblage final MP4 absent ou vide.')
                partial.replace(output)
            finally:
                partial.unlink(missing_ok=True)
            self.log('✓ x264 2 passes + AAC : assemblage final terminé.')

    def _audio(self, s: Settings, output: Path):
        dur = s.end - s.start
        inp = ['-ss', f'{s.start:.3f}', '-i', str(s.media.path),
               '-t', f'{dur:.3f}']
        voiced = inp + ['-map', f'0:{s.cappella_audio_idx}', '-vn', '-sn',
                        '-ac', '2', '-c:a', 'libmp3lame', '-b:a', '320k',
                        str(output / 'Cappella_Voix.mp3')]
        self.ffmpeg(voiced, 'Cappella_Voix.mp3', dur, (.46, .55))
        selected = next(x for x in s.media.audios if x['index'] == s.remove_audio_idx)
        codec = selected.get('codec_name', '')
        if s.remove_mode == 'manual':
            language = re.sub(r'[^A-Za-z0-9_-]', '', selected.get('tags', {}).get('language', 'und')) or 'und'
            ext = audio_extension(codec)
            dest = output / f'{language}.{ext}'
            self.ffmpeg(inp + ['-map', f'0:{s.remove_audio_idx}', '-vn', '-sn',
                                 '-c:a', 'copy', '-avoid_negative_ts', 'make_zero', str(dest)],
                        f'Extraction originale (sans réencodage) : {dest.name}', dur, (.55,.70))
            return
        channels = int(selected.get('channels') or 2)
        sample_rate = int(selected.get('sample_rate') or 48000)
        filt = (stereo_voice_filter(s.stereo_preset, sample_rate) if channels == 2
                else pan_filter(channels, selected.get('channel_layout', '')))
        if channels == 1:
            self.log('Attention : piste mono ; réduction fréquentielle approximative (aucune séparation vocale garantie).')
        elif channels == 2:
            profile = STEREO_PRESETS[s.stereo_preset]
            band = ('toutes fréquences' if profile['low'] is None else
                    f"{min(profile['low'],int(sample_rate*.20))}–"
                    f"{min(profile['high'],int(sample_rate*.45))} Hz")
            self.log(f"Stéréo : préréglage fixe « {profile['label']} », bande {band}.")
            self.log('Attention : la réduction du centre touche aussi les instruments et effets centrés ; '
                     'les échos décentrés ou diffus peuvent subsister.')
        else:
            self.log(f'{channels} canaux : suppression du canal index 2 (centre FC conventionnel).')
        main = inp + ['-map', f'0:{s.remove_audio_idx}', '-vn', '-sn',
                      '-af', filt]
        if codec == 'mp3':
            self.ffmpeg(main + ['-ac', '2', '-c:a', 'libmp3lame', '-b:a', '320k',
                                str(output / 'Sans_Voix.mp3')],
                        'Sans_Voix.mp3', dur, (.55, .74))
            return
        if codec in LOSSLESS_DEST or codec.startswith('pcm_'):
            depth = source_depth(selected)
            encode = ['-c:a', 'flac', '-sample_fmt', 's32' if depth > 16 else 's16']
            if depth > 16:
                encode += ['-bits_per_raw_sample', '24']
            ext = 'flac'
        else:
            encode, ext = audio_encoder(codec, selected)
        self.ffmpeg(main + encode + [str(output / f'Sans_Voix.{ext}')],
                    f'Sans_Voix.{ext}', dur, (.55, .69))
        self.ffmpeg(main + ['-ac', '2', '-c:a', 'libmp3lame', '-b:a', '320k',
                            str(output / 'Cappella_Sans_Voix.mp3')],
                    'Cappella_Sans_Voix.mp3', dur, (.69, .78))

    def run(self, s: Settings) -> Path:
        """Enregistre les temps de chaque étape, même en cas d'échec."""
        self._journal.clear()
        self._subtitle_cache.clear()
        start_clock = time.monotonic()
        folder = Path(s.output_root).expanduser().resolve() / s.media.path.stem
        try:
            return self._run_impl(s)
        except Exception as exc:
            self.log(f'ERREUR : {exc}')
            raise
        finally:
            self.log(f'Temps total moteur : {time.monotonic()-start_clock:.1f} secondes.')
            if folder.is_dir():
                try:
                    (folder / 'EasyCap_Journal.txt').write_text(
                        '\n'.join(self._journal) + '\n', encoding='utf-8')
                except OSError:
                    pass

    def _run_impl(self, s: Settings) -> Path:
        s.validate()
        binary('ffmpeg'); binary('ffprobe')
        output = Path(s.output_root).expanduser().resolve() / s.media.path.stem
        output.mkdir(parents=True, exist_ok=True)
        self.log(f'EasyCap — entrée : {s.media.path.name}')
        self.log(f'Scène : {fmt_time(s.start, True)} → {fmt_time(s.end, True)}')
        self.log(f'Sortie : {output}')
        self.progress(.005, 'Analyse des barres noires')
        with tempfile.TemporaryDirectory(prefix='easycap_') as work:
            tmp = Path(work)
            crop_begin = time.monotonic()
            crop = self.detect_crop(s)
            self.log(f'Analyse des barres noires : {time.monotonic()-crop_begin:.1f} s.')
            raw_h = int(s.media.video.get('height') or 1080)
            height, bitrate = (1080, 3000) if raw_h > 720 else ((720, 2000) if raw_h > 576 else (576, 1500))
            if s.media.hdr:
                self.log('Source HDR détectée : conversion SDR BT.709.')
            bitmap_vo = (s.vo_enabled and s.vo_sub_idx is not None and
                         s._sub(s.vo_sub_idx).get('codec_name') in {'hdmv_pgs_subtitle', 'dvd_subtitle'})
            styled_ass_vo = (s.vo_enabled and s.vo_sub_idx is not None and
                             s._sub(s.vo_sub_idx).get('codec_name') in {'ass', 'ssa'})
            cache = None
            if s.optimize_4k and s.vo_enabled and not bitmap_vo and not styled_ass_vo and (
                    raw_h > 1080 or int(s.media.video.get('width') or 0) > 1920 or s.media.hdr):
                estimated = int((s.end-s.start) * 22_000_000)
                free = shutil.disk_usage(tmp).free
                if free < max(2_000_000_000, estimated * 2):
                    self.log(f'Cache partagé désactivé : espace disponible trop faible ({free/1e9:.1f} Go).')
                else:
                    cache = self.preprocess_video(s, tmp, crop)
            if bitmap_vo and s.optimize_4k:
                self.log('Cache 4K désactivé pour PGS/VobSub : conservation du calque bitmap original.')
            if styled_ass_vo and s.optimize_4k:
                self.log('Cache 4K désactivé pour ASS/SSA : conservation du cadre et des positions natives.')
            video_filter = self.video_filter(s, height, None, skip_hdr=True) if cache else self.video_filter(s, height, crop)
            self._video_pass(s, output / 'Cappella_Video.mp4',
                             video_filter, 'mpeg4', bitrate, 'Cappella_Video.mp4', tmp,
                             video_source=cache, progress_range=(.18,.46) if cache else (.03,.46))
            self._audio(s, output)
            self.progress(.79, 'Sous-titres / DETX')
            self.prepare_subtitles(s, output, tmp)
            if s.vo_enabled:
                self.log('Préparation de la version originale.')
                burn_path = self.prepare_vo_sub(s, tmp, crop)
                vo_crop = crop
                if styled_ass_vo and vo_crop:
                    # Les positions ASS / SSA peuvent viser la zone noire du
                    # fichier source. Un recadrage supprimerait une seconde
                    # ligne pourtant présente dans le script original.
                    vo_crop = None
                    self.log('VOSTFR ASS/SSA : recadrage désactivé pour préserver les positions et les lignes des sous-titres d’origine.')
                shift = 0
                if bitmap_vo:
                    shift, can_crop = self.bitmap_subtitle_offset(s, crop)
                    if not can_crop:
                        vo_crop = None
                vf = self.video_filter(s, s.vo_height, None, skip_hdr=True) if cache else self.video_filter(s, s.vo_height, vo_crop)
                if burn_path is not None:
                    escaped = str(burn_path).replace('\\', '/').replace(':', '\\:').replace("'", "\\'").replace('[', '\\[').replace(']', '\\]')
                    if burn_path.suffix.lower() == '.ass':
                        # ASS/SSA : ni la police ni les styles originaux ne sont forcés.
                        vf += f",ass=filename='{escaped}'"
                    else:
                        # Libass conserve les italique/retours à la ligne SRT/VTT
                        # ainsi que les tirets de réplique. La taille est le
                        # même taille logique sur tous les profils, ce qui
                        # produit la même taille RELATIVE après mise à l'échelle
                        # libass. Contour plus fin ; sous-titres d’origine intacts.
                        style = vostfr_text_force_style()
                        vf += f",subtitles=filename='{escaped}':force_style='{style}'"
                name = 'Scène_VOSTFR.mp4' if s.vo_sub_idx is not None else 'Scène_VO.mp4'
                rate = vo_bitrate_kbps(s.vo_height, raw_h)
                self.log(f'VO/VOSTFR : hauteur maximale {s.vo_height}p (source {raw_h}p), débit cible {rate} kb/s.')
                if bitmap_vo:
                    self.log('Incrustation PGS/VobSub : calque bitmap natif ; mode FFmpeg expérimental.')
                self._video_pass(s, output / name, vf, 'libx264', rate,
                                 name, tmp, audio_idx=s.vo_audio_idx,
                                 video_source=cache, bitmap_sub_idx=s.vo_sub_idx if bitmap_vo else None,
                                 bitmap_shift=shift, progress_range=(.79, .995))
        self.progress(1.0, 'Terminé')
        self.log('✓ Dossier de préparation terminé.')
        return output
