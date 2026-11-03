"""EasyCap — interface graphique Tkinter / tkinterdnd2 (drag & drop)."""
from __future__ import annotations

from pathlib import Path
import queue
import time
from datetime import datetime
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from easycap_eta import RemainingTimeEstimator
from easycap_core import (Engine, EasyCapError, Cancelled, Settings, TEXT_CODECS, IMAGE_CODECS,
                          STEREO_PRESETS,
                          fmt_time, inspect_media, stream_label, time_to_seconds,
                          VIDEO_EXTS)

try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    CAN_DND = True
except ImportError:
    TkinterDnD = None
    DND_FILES = None
    CAN_DND = False

BASE = TkinterDnD.Tk if CAN_DND else tk.Tk
BG = '#121419'
SURFACE = '#1c2027'
CARD = '#242a34'
TEXT = '#f2f4f7'
MUTED = '#a8b2bf'
ORANGE = '#ff8b20'
BORDER = '#363d47'
ROOTDIR = Path(__file__).resolve().parent


class EasyCapApp(BASE):
    def __init__(self):
        super().__init__()
        self.title('EasyCap v1.0 — Préparation intégrale de bande rythmo')
        self.geometry('1040x850')
        self.minsize(790, 675)
        self.configure(bg=BG)
        # Icône de fenêtre et de barre des tâches : même logo que le raccourci Windows.
        self._window_icon = None
        try:
            icon_path = ROOTDIR / 'assets' / 'easycap.ico'
            if sys.platform == 'win32' and icon_path.is_file():
                self.iconbitmap(default=str(icon_path))
            else:
                png_icon = ROOTDIR / 'assets' / 'easycap_logo.png'
                if png_icon.is_file():
                    self._window_icon = tk.PhotoImage(file=str(png_icon))
                    self.iconphoto(True, self._window_icon)
        except (tk.TclError, OSError):
            pass  # L'absence de support d'icône ne doit pas empêcher le démarrage.
        self.media = None
        self.audio_mapping: list[int] = []
        self.subtext_mapping: list[int] = []
        self.suball_mapping: list[int | None] = []
        self.vo_mode = tk.BooleanVar(value=False)
        self.detx_mode = tk.BooleanVar(value=False)
        self.remove_mode = tk.StringVar(value='auto')
        self.stereo_preset_var = tk.StringVar(value=STEREO_PRESETS['standard']['label'])
        self.stereo_status_var = tk.StringVar(value='Sélectionnez un préréglage pour l’annulation du centre stéréo.')
        self.start_var = tk.StringVar(value='00:00:00')
        self.end_var = tk.StringVar(value='00:00:00')
        self.path_var = tk.StringVar(value='Glissez votre fichier vidéo ici ou cliquez pour le choisir')
        self.output_var = tk.StringVar(value=str(Path.home() / 'Videos'))
        self.vo_res_var = tk.StringVar(value='1080p')
        self.status_var = tk.StringVar(value='Prêt : importez une vidéo pour commencer.')
        self.timer_var = tk.StringVar(value='Temps restant estimé (dossier complet) : —')
        self.started_at: float | None = None
        self.eta: RemainingTimeEstimator | None = None
        self.events = queue.Queue()
        self.engine: Engine | None = None
        self.worker: threading.Thread | None = None
        self._logo = None
        self._build()
        self.after(100, self._poll)
        self.protocol('WM_DELETE_WINDOW', self._close)

    def _style(self):
        theme = ttk.Style(self)
        try: theme.theme_use('clam')
        except tk.TclError: pass
        theme.configure('Easy.TCombobox', fieldbackground=CARD, background=CARD,
                        foreground=TEXT, bordercolor=BORDER, arrowcolor=ORANGE, padding=6)
        theme.map('Easy.TCombobox', fieldbackground=[('readonly',CARD)],
                  foreground=[('readonly',TEXT)], selectbackground=[('readonly', CARD)],
                  selectforeground=[('readonly',TEXT)])
        theme.configure('Easy.Horizontal.TProgressbar', background=ORANGE,
                        troughcolor=CARD, bordercolor=BORDER, lightcolor=ORANGE, darkcolor=ORANGE)

    def _label(self, parent, text, *, fg=TEXT, size=10, bold=False, **opts):
        return tk.Label(parent, text=text, bg=parent.cget('bg'), fg=fg,
                        font=('Segoe UI',size,'bold' if bold else 'normal'), **opts)

    def _frame(self, parent, *, bg=SURFACE, **kwargs):
        return tk.Frame(parent, bg=bg, **kwargs)

    def _build(self):
        self._style()
        top = self._frame(self, bg=BG)
        top.pack(fill='x', padx=26, pady=(17,9))
        logo = ROOTDIR / 'assets' / 'easycap_logo.png'
        if logo.exists():
            try:
                from PIL import Image, ImageTk
                self._logo = ImageTk.PhotoImage(Image.open(logo).convert('RGBA').resize((84,84),Image.Resampling.LANCZOS))
            except ImportError:
                self._logo = tk.PhotoImage(file=str(logo)).subsample(5,5)
            tk.Label(top, image=self._logo, bg=BG).pack(side='left', padx=(0,15))
        heading = self._frame(top, bg=BG)
        heading.pack(side='left', fill='x', expand=True)
        self._label(heading, 'EasyCap', fg=ORANGE, size=27, bold=True).pack(anchor='w')
        self._label(heading, 'PRÉPARATION INTÉGRALE DE BANDE RYTHMO', fg=MUTED, size=10).pack(anchor='w')
        self._label(top, 'FFmpeg • MPEG-4 • Cappella • DETX', fg=MUTED, size=9).pack(side='right', anchor='s')

        outer = self._frame(self, bg=BG)
        outer.pack(fill='both', expand=True, padx=24)
        canvas = tk.Canvas(outer, highlightthickness=0, bg=BG)
        bar = ttk.Scrollbar(outer, orient='vertical', command=canvas.yview)
        self.inner = self._frame(canvas, bg=BG)
        self.inner.bind('<Configure>', lambda ev: canvas.configure(scrollregion=canvas.bbox('all')))
        self.container = canvas.create_window((0,0),window=self.inner,anchor='nw')
        canvas.bind('<Configure>',lambda ev:canvas.itemconfig(self.container,width=ev.width))
        canvas.configure(yscrollcommand=bar.set)
        canvas.pack(side='left',fill='both',expand=True)
        bar.pack(side='right',fill='y')
        self.canvas = canvas
        def wheel(event):
            if sys.platform == 'darwin': canvas.yview_scroll(-int(event.delta),'units')
            else: canvas.yview_scroll(-int(event.delta/120),'units')
        canvas.bind_all('<MouseWheel>', wheel)

        self._source_card()
        self._scene_card()
        self._audio_card()
        self._subs_card()
        self._vo_card()
        self._output_card()

        bottom = self._frame(self, bg=BG)
        bottom.pack(fill='x', padx=25,pady=(10,17))
        actions=self._frame(bottom,bg=BG)
        actions.pack(fill='x')
        self.run_btn = tk.Button(actions, text='▶  PRÉPARER LE DOSSIER',bg=ORANGE,fg='#161616',
                                 activebackground='#ffad57',activeforeground='#111',
                                 font=('Segoe UI',12,'bold'),borderwidth=0,cursor='hand2',
                                 padx=22,pady=12,command=self._start,state='disabled')
        self.run_btn.pack(side='left')
        self.stop_btn = tk.Button(actions, text='■  Annuler',bg=CARD, fg=TEXT,
                                  font=('Segoe UI',10,'bold'),borderwidth=0,
                                  padx=18,pady=13,command=self._stop,state='disabled')
        self.stop_btn.pack(side='left',padx=10)
        self.progress = ttk.Progressbar(bottom,style='Easy.Horizontal.TProgressbar',maximum=100)
        self.progress.pack(fill='x',pady=(11,6))
        tk.Label(bottom,textvariable=self.timer_var,bg=BG,fg=ORANGE,
                 anchor='w',font=('Segoe UI',10,'bold')).pack(fill='x',pady=(0,4))
        tk.Label(bottom,textvariable=self.status_var,bg=BG,fg=MUTED,
                 anchor='w',font=('Segoe UI',9)).pack(fill='x')
        self.logbox = tk.Text(bottom,height=6,bg='#0e1115',fg='#c5cfda',
                              font=('Consolas',9),relief='flat',borderwidth=0,
                              wrap='word',padx=10,pady=8)
        self.logbox.pack(fill='x',pady=(7,0))
        self.logbox.configure(state='disabled')
        self._enable_details(False)

    def _card(self, heading, desc=None):
        frame = self._frame(self.inner)
        frame.pack(fill='x', pady=(0,12),padx=2)
        title = self._frame(frame)
        title.pack(fill='x',padx=17,pady=(13,3))
        self._label(title,heading,size=12,bold=True).pack(anchor='w')
        if desc: self._label(title,desc,size=9,fg=MUTED,justify='left',wraplength=880).pack(anchor='w',pady=(2,1))
        body = self._frame(frame)
        body.pack(fill='x',padx=17,pady=(9,16))
        return body

    def _source_card(self):
        body=self._card('01   Fichier vidéo','MP4, MKV, M2TS ou AVI — une piste audio au minimum est obligatoire.')
        area=self._frame(body,bg=CARD)
        area.pack(fill='x')
        path_label=tk.Label(area,textvariable=self.path_var,bg=CARD,fg=TEXT,
                            font=('Segoe UI',10),wraplength=700,anchor='w',justify='left',padx=15,pady=17)
        path_label.pack(side='left',fill='x',expand=True)
        button=tk.Button(area,text='Parcourir…',command=self._browse,bg='#3c434f',fg=TEXT,
                         font=('Segoe UI',10,'bold'),padx=15,pady=10,borderwidth=0,cursor='hand2')
        button.pack(side='right',padx=9)
        if CAN_DND:
            for item in (area,path_label,button):
                item.drop_target_register(DND_FILES)
                item.dnd_bind('<<Drop>>',self._on_drop)
        else:
            self._label(body,'Pour le glisser-déposer : pip install tkinterdnd2',fg='#ffbb78',size=9).pack(anchor='w',pady=(7,0))
        self.info_label=self._label(body,'Aucune vidéo analysée.',fg=MUTED,size=9,anchor='w')
        self.info_label.pack(fill='x',pady=(7,0))

    def _scene_card(self):
        body=self._card('02   Intervalle de la scène','Le début et la fin sont modifiables (HH:MM:SS ou HH:MM:SS.mmm).')
        grid=self._frame(body)
        grid.pack(fill='x')
        for col,(label,var) in enumerate([('DÉBUT',self.start_var),('FIN',self.end_var)]):
            group=self._frame(grid)
            group.grid(row=0,column=col,sticky='ew',padx=(0 if col==0 else 9,0))
            self._label(group,label,fg=MUTED,size=9,bold=True).pack(anchor='w',pady=(0,4))
            entry=tk.Entry(group,textvariable=var,bg=CARD,fg=TEXT,insertbackground=TEXT,
                            relief='flat',font=('Consolas',12),borderwidth=0)
            entry.pack(fill='x',ipady=8,ipadx=10)
            if col==0: self.start_entry=entry
            else: self.end_entry=entry
        grid.columnconfigure(0,weight=1);grid.columnconfigure(1,weight=1)

    def _combo(self,parent,title,values=None):
        line=self._frame(parent)
        line.pack(fill='x',pady=(0,10))
        self._label(line,title,fg=MUTED,size=9,bold=True).pack(anchor='w',pady=(0,5))
        combo=ttk.Combobox(line,values=values or [],state='readonly',style='Easy.TCombobox')
        combo.pack(fill='x')
        return combo

    def _audio_card(self):
        body=self._card('03   Pistes audio et suppression des voix',
                        'La première piste audio est sélectionnée par défaut dans les deux menus.')
        self.cap_combo=self._combo(body,'LANGUE AUDIO POUR CAPPELLA — CAPPELLA_VOIX.MP3')
        self.remove_combo=self._combo(body,'LANGUE AUDIO À UTILISER POUR LA SUPPRESSION')
        self.remove_combo.bind('<<ComboboxSelected>>', lambda event: self._refresh_option_states())
        modes=self._frame(body)
        modes.pack(fill='x')
        for label,val in (('Automatique — annulation du centre / canal FC','auto'),
                          ('Manuelle (montage) — extraction sans réencodage','manual')):
            tk.Radiobutton(modes,text=label,variable=self.remove_mode,value=val,
                           command=self._refresh_option_states,
                           bg=SURFACE,fg=TEXT,selectcolor=CARD,activebackground=SURFACE,
                           activeforeground=TEXT,font=('Segoe UI',10)).pack(anchor='w',pady=3)
        self.stereo_frame=self._frame(body,bg=CARD)
        self.stereo_frame.pack(fill='x',pady=(10,0))
        inside=self._frame(self.stereo_frame,bg=CARD)
        inside.pack(fill='x',padx=13,pady=12)
        self._label(inside,'FILTRE VOCAL — SOURCES STÉRÉO 2.0',fg=ORANGE,size=9,bold=True).pack(anchor='w',pady=(0,7))
        self._label(inside,'PRÉRÉGLAGE SELON L’ENVIRONNEMENT',fg=MUTED,size=9,bold=True).pack(anchor='w')
        self.stereo_preset_combo=ttk.Combobox(inside,textvariable=self.stereo_preset_var,
                       values=[p['label'] for p in STEREO_PRESETS.values()],state='readonly',
                       style='Easy.TCombobox')
        self.stereo_preset_combo.pack(fill='x',pady=(4,10))
        self._label(inside,'Chaque préréglage possède ses réglages fixes, sans ajustement de puissance.',
                    fg=MUTED,size=9).pack(anchor='w',pady=(0,6))
        tk.Label(inside,textvariable=self.stereo_status_var,bg=CARD,fg=MUTED,
                 font=('Segoe UI',9),anchor='w',justify='left',wraplength=780).pack(fill='x',pady=(7,0))
        self._label(body,'Note : l’automatique est approximative en mono/stéréo ; en multicanal, la piste centrale est mutée.',
                    fg=MUTED,size=9,wraplength=850,justify='left').pack(anchor='w',pady=(9,0))

    def _subs_card(self):
        body=self._card('04   Sous-titres / Cappella .detx','Conversion facultative, sur quatre pistes maximum. Timecodes fixes à 24 images/s, origine 01:00:00:00 ; deux voix sur pistes 0/1.')
        self.detx_chk=tk.Checkbutton(body,text='Créer Bande_Rythmo.[format original textuel] + Bande_Rythmo.detx',
                                     variable=self.detx_mode,command=self._refresh_option_states,
                                     bg=SURFACE,fg=TEXT,selectcolor=CARD,activeforeground=TEXT,
                                     activebackground=SURFACE,font=('Segoe UI',10))
        self.detx_chk.pack(anchor='w')
        self.detx_combo=self._combo(body,'PISTE DE SOUS-TITRES TEXTUELS')
        self._label(body,'Cadence DETX automatique : 24 i/s • origine 01:00:00:00',
                    fg=MUTED,size=9).pack(anchor='w',pady=(3,0))

    def _vo_card(self):
        body=self._card('05   Vidéo de référence VO / VOSTFR',
                        'H.264/x264 medium, 2 passes : 1080p 3000 kb/s · 720p 2000 kb/s · 576p et moins 1500 kb/s.')
        self.vo_chk=tk.Checkbutton(body,text='Créer une vidéo de référence (Scène_VO / Scène_VOSTFR)',
                                   variable=self.vo_mode,command=self._refresh_option_states,
                                   bg=SURFACE,fg=TEXT,selectcolor=CARD,activeforeground=TEXT,
                                   activebackground=SURFACE,font=('Segoe UI',10))
        self.vo_chk.pack(anchor='w')
        self.vo_audio_combo=self._combo(body,'PISTE AUDIO ORIGINALE OBLIGATOIRE')
        self.vo_sub_combo=self._combo(body,'SOUS-TITRES À INCRUSTER (SRT/VTT/ASS/PGS/VobSub)')
        self._label(body,'SRT/VTT : taille visuelle identique à toutes les résolutions (police fine, contour discret).  ASS/SSA : style original.  PGS/VobSub : incrustation expérimentale.',
                    fg=MUTED,size=9,wraplength=850,justify='left').pack(anchor='w',pady=(0,8))
        res=self._frame(body)
        res.pack(fill='x')
        self._label(res,'RÉSOLUTION MAXIMALE',fg=MUTED,size=9,bold=True).pack(side='left',padx=(0,10))
        self.vo_res_combo=ttk.Combobox(res,values=['576p','720p','1080p'],textvariable=self.vo_res_var,
                                      width=12,state='readonly',style='Easy.TCombobox')
        self.vo_res_combo.pack(side='left')

    def _output_card(self):
        body=self._card('06   Dossier de destination','Le programme crée automatiquement un sous-dossier nommé comme la vidéo source.')
        line=self._frame(body,bg=CARD)
        line.pack(fill='x')
        tk.Label(line,textvariable=self.output_var,bg=CARD,fg=TEXT,anchor='w',
                 font=('Segoe UI',10),padx=10,pady=9).pack(side='left',fill='x',expand=True)
        tk.Button(line,text='Modifier…',command=self._browse_output,bg='#3c434f',fg=TEXT,
                  borderwidth=0,padx=12,pady=9,font=('Segoe UI',10)).pack(side='right',padx=5)

    def _browse(self):
        file=filedialog.askopenfilename(title='Choisir une vidéo',
                        filetypes=[('Vidéos','*.mp4 *.mkv *.m2ts *.avi'),('Tous les fichiers','*.*')])
        if file: self._open_media(file)

    def _on_drop(self,event):
        files=self.tk.splitlist(event.data)
        if files: self._open_media(files[0])

    def _open_media(self,filename):
        if self.worker and self.worker.is_alive():
            messagebox.showwarning('EasyCap','Impossible de changer de fichier pendant l’encodage.')
            return
        try:
            info=inspect_media(filename)
        except (EasyCapError,ValueError,OSError) as e:
            messagebox.showerror('Vidéo invalide',str(e))
            return
        self.media=info
        self.timer_var.set('Temps restant estimé (dossier complet) : —')
        self.progress['value']=0
        self.path_var.set(str(info.path))
        self.start_var.set('00:00:00')
        self.end_var.set(fmt_time(info.duration, True))
        self.audio_mapping=[s['index'] for s in info.audios]
        audio_names=[stream_label(s) for s in info.audios]
        for combo in (self.cap_combo,self.remove_combo,self.vo_audio_combo):
            combo['values']=audio_names
            combo.current(0)
        textsubs=[s for s in info.subtitles if s.get('codec_name') in TEXT_CODECS]
        self.subtext_mapping=[s['index'] for s in textsubs]
        self.detx_combo['values']=[stream_label(s) for s in textsubs]
        if textsubs:self.detx_combo.current(0)
        vo_supported = [s for s in info.subtitles if s.get('codec_name') in TEXT_CODECS | {'hdmv_pgs_subtitle', 'dvd_subtitle'}]
        self.suball_mapping=[None]+[s['index'] for s in vo_supported]
        self.vo_sub_combo['values']=['Aucun sous-titre']+[stream_label(s) for s in vo_supported]
        bitmap_count = sum(1 for s in vo_supported if s.get('codec_name') in {'hdmv_pgs_subtitle', 'dvd_subtitle'})
        if bitmap_count:
            self._append(f'ℹ {bitmap_count} piste(s) PGS/VobSub disponible(s) (incrustation FFmpeg expérimentale).')
        self.vo_sub_combo.current(0)
        self.detx_mode.set(False);self.vo_mode.set(False)
        self._refresh_option_states()
        self._enable_details(True)
        size=f"{info.video.get('width','?')}×{info.video.get('height','?')}"
        hdr=' • HDR' if info.hdr else ' • SDR'
        self.info_label.configure(text=f"{fmt_time(info.duration,True)} • {size} • {len(info.audios)} audio • "
                                           f"{len(info.subtitles)} sous-titres • {info.fps:.3f} fps{hdr}")
        self._append(f'✓ Vidéo chargée : {info.path.name}')
        self.status_var.set('Vidéo analysée. Configurez les options et lancez la préparation.')

    def _enable_details(self,enabled):
        for item in (self.start_entry,self.end_entry,self.cap_combo,self.remove_combo,
                     self.vo_audio_combo,self.vo_sub_combo,self.detx_combo,self.vo_res_combo,
                     self.detx_chk,self.vo_chk):
            if isinstance(item,ttk.Combobox): item.configure(state='readonly' if enabled else 'disabled')
            else: item.configure(state='normal' if enabled else 'disabled')
        self.run_btn.configure(state='normal' if enabled else 'disabled')
        self._refresh_option_states()

    def _refresh_option_states(self):
        busy = self.worker is not None and self.worker.is_alive()
        allow_detx=bool(self.media and self.detx_mode.get() and self.subtext_mapping and not busy)
        allow_vo=bool(self.media and self.vo_mode.get() and not busy)
        self.detx_combo.configure(state='readonly' if allow_detx else 'disabled')
        for c in (self.vo_audio_combo,self.vo_sub_combo,self.vo_res_combo):
            c.configure(state='readonly' if allow_vo else 'disabled')
        selected = self.remove_combo.current()
        channels = (int(self.media.audios[selected].get('channels') or 2)
                    if self.media and 0 <= selected < len(self.media.audios) else None)
        allow_stereo = bool(self.media and not busy and self.remove_mode.get() == 'auto' and channels == 2)
        self.stereo_preset_combo.configure(state='readonly' if allow_stereo else 'disabled')
        if channels == 2 and self.remove_mode.get() == 'auto':
            self.stereo_status_var.set('Préréglage fixe : réduction du centre stéréo selon la scène. '
                                       'Les voix décentrées et la réverbération peuvent subsister.')
        elif self.remove_mode.get() == 'manual':
            self.stereo_status_var.set('Mode manuel : fichier original extrait sans traitement.')
        elif channels is not None and channels >= 3:
            self.stereo_status_var.set('Mixage multicanal : suppression du canal central FC, sans filtre stéréo.')
        else:
            self.stereo_status_var.set('Les préréglages sont disponibles en automatique sur une piste stéréo 2.0.')

    def _browse_output(self):
        directory=filedialog.askdirectory(title='Choisir le dossier parent',initialdir=self.output_var.get())
        if directory:self.output_var.set(directory)

    def _chosen(self,combo, mapping, required=True):
        idx=combo.current()
        if idx < 0 or idx >= len(mapping):
            if required: raise EasyCapError('Sélectionnez une piste valide.')
            return None
        return mapping[idx]

    def _start(self):
        if not self.media:return
        try:
            s=Settings(media=self.media,start=time_to_seconds(self.start_var.get()),
                       end=time_to_seconds(self.end_var.get()),
                       cappella_audio_idx=self._chosen(self.cap_combo,self.audio_mapping),
                       remove_audio_idx=self._chosen(self.remove_combo,self.audio_mapping),
                       remove_mode=self.remove_mode.get(),convert_detx=self.detx_mode.get(),
                       stereo_preset=next((key for key, profile in STEREO_PRESETS.items()
                                           if profile['label'] == self.stereo_preset_var.get()), 'standard'),
                       detx_sub_idx=self._chosen(self.detx_combo,self.subtext_mapping) if self.detx_mode.get() else None,
                       vo_enabled=self.vo_mode.get(),
                       vo_audio_idx=self._chosen(self.vo_audio_combo,self.audio_mapping) if self.vo_mode.get() else None,
                       vo_sub_idx=self._chosen(self.vo_sub_combo,self.suball_mapping) if self.vo_mode.get() else None,
                       vo_height=int(self.vo_res_var.get().rstrip('p')),
                       output_root=Path(self.output_var.get()))
            s.validate()
            folder=s.output_root/s.media.path.stem
            if folder.exists() and any(folder.iterdir()) and not messagebox.askyesno('Fichiers existants',
                 f'Le dossier {folder} contient déjà des fichiers. Les sorties EasyCap de même nom seront écrasées. Continuer ?'):
                return
        except (EasyCapError,ValueError,OSError) as err:
            messagebox.showerror('Paramètres invalides',str(err))
            return
        self.engine=Engine(log=lambda msg:self.events.put(('log',msg)),
                           progress=lambda p,msg:self.events.put(('progress',p,msg)))
        self.run_btn.configure(state='disabled')
        self.stop_btn.configure(state='normal')
        self.progress['value']=0
        self.started_at = time.monotonic()
        self.eta = RemainingTimeEstimator(self.started_at, with_vo=s.vo_enabled)
        self.timer_var.set('Temps restant estimé (dossier complet) : calcul en cours…')
        self.status_var.set('Encodage démarré…')
        self._refresh_option_states()
        start_clock = self.started_at
        def run():
            try:
                result=self.engine.run(s)
                self.events.put(('success',str(result),time.monotonic()-start_clock,datetime.now()))
            except Cancelled as e:
                self.events.put(('cancelled',str(e),time.monotonic()-start_clock,datetime.now()))
            except Exception as e:
                self.events.put(('error',str(e),time.monotonic()-start_clock,datetime.now()))
        self.worker=threading.Thread(target=run,daemon=True)
        self.worker.start()

    def _stop(self):
        if self.engine:
            self.status_var.set('Annulation demandée…')
            self.engine.cancel()

    @staticmethod
    def _duration_text(duration: float) -> str:
        total = max(0, int(duration))
        hours, remainder = divmod(total, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f'{hours:02d}:{minutes:02d}:{seconds:02d}'

    def _poll(self):
        try:
            while True:
                event=self.events.get_nowait()
                kind=event[0]
                if kind=='log': self._append(event[1])
                elif kind=='progress':
                    if self.eta is not None:
                        self.eta.update(event[1], event[2], time.monotonic())
                    self.progress['value']=round(100*event[1],1)
                    self.status_var.set(f'{event[2]}  —  {100*event[1]:.0f}%')
                elif kind in ('success','error','cancelled'):
                    self.stop_btn.configure(state='disabled')
                    self.run_btn.configure(state='normal' if self.media else 'disabled')
                    self.worker=None
                    self.started_at=None
                    self.eta=None
                    self._refresh_option_states()
                    elapsed=self._duration_text(event[2])
                    finished=event[3].strftime('%d/%m/%Y à %H:%M:%S')
                    if kind=='success':
                        self.timer_var.set(f'Temps restant : 00:00:00  •  Terminé le {finished}')
                        self.status_var.set(f'✓ Dossier intégral prêt — traitement terminé en {elapsed}.')
                        self.progress['value']=100
                        self._append(f'✓ Dossier prêt le {finished} après {elapsed}.')
                        self._append('Fichiers disponibles : '+event[1])
                        messagebox.showinfo('EasyCap — dossier prêt',
                                            f'Dossier créé :\n{event[1]}\n\nTemps total : {elapsed}\nTerminé le {finished}')
                    elif kind=='cancelled':
                        self.timer_var.set('Temps restant : —  •  Opération annulée')
                        self.status_var.set(f'Opération annulée après {elapsed}. Les fichiers déjà créés peuvent rester.')
                        self._append(event[1])
                    else:
                        self.timer_var.set('Temps restant : —  •  Opération interrompue')
                        self.status_var.set(f'Erreur après {elapsed} — consulter le journal.')
                        self._append('✖ '+event[1])
                        messagebox.showerror('EasyCap — erreur',event[1][-2500:])
        except queue.Empty:pass
        if self.started_at is not None and self.eta is not None:
            now = time.monotonic()
            seconds = self.eta.seconds_left(now)
            if seconds is None:
                value = 'calcul en cours…'
            elif self.eta.needs_recalculation(now):
                value = 'réévaluation en cours…'
            else:
                value = self._duration_text(seconds)
            self.timer_var.set('Temps restant estimé (dossier complet) : ' + value)
        self.after(100,self._poll)

    def _append(self,msg):
        self.logbox.configure(state='normal')
        self.logbox.insert('end',msg+'\n')
        self.logbox.see('end')
        self.logbox.configure(state='disabled')

    def _close(self):
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno('Quitter EasyCap','Un traitement est en cours. Voulez-vous l’annuler et quitter ?'):
                return
            self._stop()
        self.destroy()


def main():
    app=EasyCapApp()
    if len(sys.argv)>1:
        app.after(150, lambda:app._open_media(sys.argv[1]))
    app.mainloop()

if __name__=='__main__':
    main()
