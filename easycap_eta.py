"""Estimation du temps restant pour le dossier intégral EasyCap.

La barre de progression historique réserve 32 % à l'audio (qui est en général
bien plus rapide que les encodages vidéo). Convertir directement ce pourcentage
en temps restant produirait donc des prévisions trompeuses. Cet estimateur utilise
une pondération de travail séparée, différente selon les sorties à produire.

Ce module reste indépendant de Tkinter pour permettre les tests déterministes.
Une estimation n'est jamais une garantie de temps de rendu : FFmpeg peut changer
de vitesse selon le codec, la scène, les filtres ou le stockage.
"""
from __future__ import annotations

import math


def _piecewise(fraction: float, knots: tuple[tuple[float, float], ...]) -> float:
    fraction = min(1., max(0., fraction))
    for (left, low), (right, high) in zip(knots, knots[1:]):
        if fraction <= right:
            return low + (high - low) * (fraction - left) / (right - left)
    return 1.


class RemainingTimeEstimator:
    """Prévision glissante basée sur les vraies progressions FFmpeg.

    Les jalons sont pondérés par *coût temporel estimé* (vidéo plus coûteuse
    que les pistes audio). L'estimation est recalculée et lissée en continu.
    Les valeurs initiales sont volontairement indisponibles : quelques secondes
    d'exécution sont nécessaires pour mesurer une vitesse réelle.
    """

    def __init__(self, started_at: float, *, with_vo: bool):
        self.started_at = started_at
        self.with_vo = with_vo
        self.cache_active: bool | None = None
        self.fraction = 0.
        self.last_progress_at = started_at
        self.finish_at: float | None = None
        self.finished = False
        self._last_smoothing_at: float | None = None

    def _weighted_fraction(self, fraction: float) -> float:
        if not self.with_vo:
            # Pas de VO : MPEG-4 2 passes, puis un petit bloc d'audio/DETX.
            knots = ((0., 0.), (.03, .025), (.46, .85), (.79, .975), (1., 1.))
        elif self.cache_active:
            # Source 4K/HDR : cache partagé, MPEG-4, audio, puis H.264 x264.
            knots = ((0., 0.), (.03, .02), (.18, .18), (.46, .41),
                     (.79, .465), (.995, .995), (1., 1.))
        else:
            # Sans cache : MPEG-4, audio et H.264 x264 (en général le plus lent).
            knots = ((0., 0.), (.03, .025), (.46, .35),
                     (.79, .405), (.995, .995), (1., 1.))
        return _piecewise(fraction, knots)

    def update(self, fraction: float, stage: str, now: float) -> None:
        """Intègre un événement (pourcentage global et libellé de l'étape)."""
        if self.finished:
            return
        if not isinstance(fraction, (int, float)) or not math.isfinite(fraction):
            return
        fraction = min(1., max(0., float(fraction)))
        if 'cache FFV1' in stage:
            self.cache_active = True
        elif 'Cappella_Video.mp4' in stage and self.cache_active is None:
            self.cache_active = False

        if fraction >= 1.:
            self.finished = True
            self.fraction = 1.
            self.finish_at = now
            return
        if fraction <= self.fraction:
            return
        self.fraction = fraction
        self.last_progress_at = now
        elapsed = max(0., now - self.started_at)
        work = self._weighted_fraction(self.fraction)

        # Les premières valeurs de FFmpeg sont trop bruitées pour calculer une ETA.
        if elapsed < 8. or work < .04:
            return
        # Durée projetée de toutes les tâches : pas uniquement l'étape active.
        projected_total = elapsed / work
        target_finish_at = self.started_at + projected_total
        # Intervalle entre révisions : n'intègre pas 30 événements dans 0,1 s.
        if self.finish_at is None:
            self.finish_at = target_finish_at
            self._last_smoothing_at = now
            return
        since = now - (self._last_smoothing_at or now)
        if since < .75:
            return
        # Interpolation temporelle exponentielle ; une étape rapide (audio)
        # ne peut pas supprimer instantanément la durée future de x264.
        alpha = 1. - math.exp(-max(0., since) / 12.)
        self.finish_at = self.finish_at + alpha * (target_finish_at - self.finish_at)
        self._last_smoothing_at = now

    def seconds_left(self, now: float) -> int | None:
        if self.finished:
            return 0
        if self.finish_at is None:
            return None
        # Si l'exécution est plus lente que prévu, ne pas afficher 00:00:00
        # avant que tous les fichiers soient effectivement créés.
        return max(1, math.ceil(self.finish_at - now))

    def needs_recalculation(self, now: float) -> bool:
        """Signale une estimation dépassée, par exemple lors d'une attente disque."""
        return (not self.finished and self.finish_at is not None
                and now > self.finish_at and now - self.last_progress_at > 5.)
