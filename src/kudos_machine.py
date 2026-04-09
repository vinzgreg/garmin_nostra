"""KudosMachine — replies with kudos to everyone who favourites an activity post."""

from __future__ import annotations

import logging
import random
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mastodon_bot import MastodonBot
    from storage import ActivityStore

logger = logging.getLogger(__name__)

# ── 100 unique German kudos messages ─────────────────────────────────────────

KUDOS_MESSAGES = [
    "Gut gemacht, du Rennsau!",
    "Du bist ne krasse Rakete!",
    "Geilofant!",
    "Bist Speedy-Gonzales!",
    "Kudos, Granate!",
    "Lauf Forrest, Lauf!",
    "Wer hat den schnellsten Hintern im ganzen Land? Du!",
    "Alter Schwede, das war fett!",
    "Du läufst so schnell, dass selbst der Wind neidisch ist!",
    "Bist du ein Motor? Weil du einfach nicht stoppst!",
    "Turboschnitzel!",
    "Du machst das wie ein Champion — ohne Pause, ohne Mitleid!",
    "Voll in der Spur, du Streckenfresser!",
    "Wenn du aufhörst zu laufen, hört die Welt auf zu drehen!",
    "Das war kein Sport, das war Performance-Kunst!",
    "Läuft bei dir. Buchstäblich.",
    "Schneller als der Postbote, fitter als der Rest!",
    "Schwitze dich zur Legende!",
    "Du bist das menschliche Äquivalent eines Turbomotors!",
    "Kilometersammler der Extraklasse!",
    "GPS macht Überstunden wegen dir!",
    "Dein Puls weiß, was er tut — Respekt!",
    "Strava weint vor Freude!",
    "Du trainierst, als wäre morgen verboten!",
    "Niemals aufgeben — das ist dein Motto, und es funktioniert!",
    "Irgendwo gerade weint ein Sofa — weil du nicht drauf sitzt!",
    "Deine Beine sind Legenden!",
    "Renn weiter, du Kilometer-Goblin!",
    "Wer braucht ein Auto, wenn man Beine wie deine hat?",
    "Du bist das, wofür Sportsgeist erfunden wurde!",
    "Olympia weiß noch nicht, was auf sie zukommt!",
    "Du bist so fit, das erschreckt mich ein bisschen!",
    "Mit Vollgas und ohne Bremse — das bist du!",
    "Chapeau, Sportskanone!",
    "Absolute Einheit auf der Strecke!",
    "Bestzeit im Anmarsch, Ausrede im Ruhestand!",
    "Du jagst Rekorde wie andere Pokémons jagen!",
    "Gleitend, schnell und unaufhaltsam — wie ein gut geschmiertes Fahrrad!",
    "Dein Herz schlägt im Takt des Sieges!",
    "Wer sagt, Helden tragen Umhänge? Manche tragen Garmin!",
    "Bist du aus Kryptonit? Weil du unzerstörbar bist!",
    "Du bist der Beweis, dass Menschen fliegen können — zumindest fast!",
    "Einfach machen — und du machst es einfach episch!",
    "Ein bisschen Wahnsinn, viel Talent!",
    "Die Strecke hat dich nicht besiegt. Sie hat überlebt. Knapp.",
    "Held ohne Umhang, aber mit Herzfrequenzmesser!",
    "Deine Kondition ist ein Kunstwerk!",
    "Das war kein Training — das war Poesie in Bewegung!",
    "Vollgas, kein Handbrake — so läuft das bei dir!",
    "Du trainierst wie ein Tier und läufst wie ein Gott!",
    "Asphalt-Dominator!",
    "Beine aus Stahl, Herz aus Gold!",
    "Wenn Training Sünde wäre, würdest du brennen!",
    "Einfach unaufhaltsam. Punkt.",
    "Dein Tempo macht mir Angst — und ich bin ein Bot!",
    "Kilometer fressen wie andere Kekse essen!",
    "Jede Aktivität ist ein Meisterwerk. Du bist Picasso mit Laufschuhen!",
    "Was ist schneller als du? Nichts.",
    "Ich habe Angst, dass du irgendwann den Planeten überholst!",
    "Du bist der Grund, warum Garmin nachts keine Ruhe findet!",
    "Mehr Ausdauer als ein Duracell-Hase auf Koffein!",
    "Die Strecke kennt deinen Namen — und hat Respekt!",
    "Wärst du ein Fahrzeug, würdest du für Raserei gesperrt!",
    "Schweißperlen-Skulptur des Tages!",
    "So viel Energie — bist du ein Kernkraftwerk?!",
    "Du gibst erst auf, wenn GPS aufgibt!",
    "Laufen als Lebenseinstellung — und du lebst sie voll!",
    "Dein Garmin hat Hitzewallungen bekommen!",
    "Du läufst, also bist du. Cogito ergo curro!",
    "Kalorienvernichter der Sonderklasse!",
    "Wenn Fitness-Influencer groß werden wollen, wollen sie werden wie du!",
    "Du bist der Boss auf dieser Strecke. Keine Diskussion.",
    "Training? Nein — das ist Lebenskunst!",
    "Deine Schrittfrequenz ist illegaler als 200 km/h auf der Autobahn!",
    "Die Berge zittern, wenn du kommst!",
    "Bist du sicher, dass du kein Elektroantrieb bist?!",
    "Mega krass, mega schnell, mega du!",
    "Du wärst sogar als NPC in einem Laufspiel zu schnell!",
    "Wenn Schmerz die Grenze ist, hast du sie gerade überschritten — mit Anlauf!",
    "Strecke gemeistert. Ausreden vernichtet!",
    "Atemlos durch die Nacht — aber aus gutem Grund!",
    "Du bellst nicht — du beißt die Strecke!",
    "Aus dem Stoff gemacht, aus dem Champions-Träume sind!",
    "Wenn Berge Augen hätten, würden sie starren!",
    "Turbo-Tier auf der Strecke!",
    "Du bist so schnell, du überholst dich selbst!",
    "Nüchtern betrachtet: Einfach unglaublich!",
    "Laufschuh-Legende!",
    "Du machst Schweiß zu Kunst!",
    "Schallmauer? Welche Schallmauer?!",
    "Du bist der Unterschied zwischen Ausrede und Aktivität!",
    "Kein Weg zu weit, kein Berg zu hoch — so tickst du!",
    "Fit wie ein Turnschuh — ein nagelneuer, natürlich!",
    "Du hast heute wieder bewiesen, dass Grenzen nur im Kopf existieren!",
    "Respekt, du Ausdauer-Maschine ohne Aus-Knopf!",
    "Bist du eigentlich aus Titan? Fragt man sich.",
    "Dein Trainingsplan hat mehr Substanz als die meisten Lebenspläne!",
    "Kein Regen, kein Wind, keine Ausrede — nur Vollgas. Das bist du!",
    "Du machst Kilometer zu Konfetti!",
    "Schweißgebadet und trotzdem König — so geht das!",
]

class KudosMachine:
    """
    Polls each posted Mastodon activity status for new favourites and replies
    with an encouraging kudos message mentioning both the fav-giver and the
    activity owner.

    One kudos reply is sent per (status_id, fav-giver account_id) pair,
    tracked permanently in the ``kudos_sent`` DB table.
    """

    def __init__(
        self,
        bot: "MastodonBot",
        custom_template: str | None = None,
        post_delay_s: float = 2.0,
    ) -> None:
        self._bot = bot
        self._post_delay_s = post_delay_s

        if len(KUDOS_MESSAGES) != 100:
            raise ValueError(f"Expected 100 kudos messages, got {len(KUDOS_MESSAGES)}")

        # Validate the custom template at startup so a typo doesn't crash
        # every kudos attempt at runtime.
        if custom_template:
            try:
                custom_template.format(fav_giver="@test", activity_user="@test")
            except (KeyError, IndexError) as exc:
                logger.error(
                    "kudosCustom template is invalid (%s): %s — falling back to built-in messages.",
                    exc, custom_template,
                )
                custom_template = None
        self._template = custom_template

    def process_user(
        self,
        user_id: int,
        mastodon_handle: str,
        store: "ActivityStore",
        max_age_days: int | None = None,
        visibility: str = "direct",
    ) -> None:
        """Check all posted activities for new favs and send kudos replies."""
        activities = store.get_activities_for_kudos(user_id, max_age_days=max_age_days)
        if not activities:
            logger.debug(
                "[kudos] No eligible activities for user_id=%d "
                "(mastodon_posted=1 AND mastodon_status_id IS NOT NULL%s). "
                "Hint: activities posted before mastodon_status_id was tracked have NULL status_id.",
                user_id,
                f", max_age_days={max_age_days}" if max_age_days is not None else "",
            )
            return

        logger.debug("[kudos] Checking %d activities for new favs.", len(activities))

        for activity in activities:
            status_id = activity["mastodon_status_id"]
            garmin_id = activity["garmin_activity_id"]

            try:
                favs = self._bot.get_favourited_by(status_id)
            except Exception as exc:
                # 403 = status is unlisted/private; favourited_by is not available.
                # Log at DEBUG to avoid noisy warnings for permanently inaccessible statuses.
                is_forbidden = "403" in str(exc) or "Forbidden" in str(exc)
                (logger.debug if is_forbidden else logger.warning)(
                    "[kudos] Could not fetch favs for status %s (activity %s): %s",
                    status_id, garmin_id, exc,
                )
                continue

            new_favs = [a for a in favs if not store.is_kudos_sent(status_id, str(a["id"]))]
            logger.info(
                "[kudos] Activity %s (status %s): %d fav(s) total, %d new.",
                garmin_id, status_id, len(favs), len(new_favs),
            )

            for account in favs:
                account_id = str(account["id"])
                fav_handle = "@" + account["acct"]

                if store.is_kudos_sent(status_id, account_id):
                    continue

                text = self._build_text(fav_handle, mastodon_handle)
                try:
                    self._bot.post_reply(text, in_reply_to_id=status_id, visibility=visibility)
                    store.mark_kudos_sent(status_id, account_id)
                    logger.info(
                        "[kudos] Replied to status %s — kudos to %s for activity %s.",
                        status_id, fav_handle, garmin_id,
                    )
                    if self._post_delay_s > 0:
                        time.sleep(self._post_delay_s)
                except Exception as exc:
                    logger.error(
                        "[kudos] Failed to send kudos to %s for status %s: %s",
                        fav_handle, status_id, exc,
                    )

    def _build_text(self, fav_handle: str, activity_handle: str) -> str:
        if self._template:
            return self._template.format(
                fav_giver=fav_handle,
                activity_user=activity_handle,
            )
        msg = random.choice(KUDOS_MESSAGES)
        return f":kudos: {fav_handle} {activity_handle} — {msg}"
