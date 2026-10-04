#!/usr/bin/env python3
"""OpenHands Cloud keepalive / one-canonical-conversation-per-repository.

Normal operation selects only the newest conversation for each repository.
An older conversation is considered only after the newest conversation's
sandbox is genuinely gone (MISSING/not found). This prevents parallel agents
from the same repository while still allowing recovery to an older conversation.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import time

try:
    import requests
except ImportError:
    print("requests puuttuu. Asenna:\n  python -m pip install requests", file=sys.stderr)
    sys.exit(1)


class ConversationNotFound(RuntimeError):
    """Raised only when OpenHands confirms that a conversation no longer exists."""


DEFAULT_NUDGE_LOOP = (
    "Jatka autonomista looppia ty\u00f6nkulkusi mukaan.\n\n"
    "1) Tarkista nykyinen tila (git, avoimet ty\u00f6t, roadmap).\n"
    "2) Jatka kesken olevaa ty\u00f6t\u00e4 ilman turhaa selittely\u00e4.\n"
    "3) Ennen uuden ty\u00f6n aloittamista tarkista git, avoimet PR:t ja issue-ty\u00f6 "
    "sek\u00e4 varmista, ettei toinen agentti tee samaa ty\u00f6t\u00e4. V\u00e4lt\u00e4 duplikaatit.\n"
    "4) Jos nykyiset teht\u00e4v\u00e4t loppuvat: ota lis\u00e4\u00e4 t\u00f6it\u00e4 roadmapilta "
    "tai avoimista issueista (kun s\u00e4\u00e4nn\u00f6t sen sallivat).\n"
    "5) Jos roadmapkin on tyhj\u00e4: tutki mit\u00e4 sovelluksesta puuttuu "
    "isommana kokonaisuutena, lis\u00e4\u00e4 suosituksesi roadmapille ja "
    "ota se ty\u00f6st\u00f6\u00f6n.\n"
    "6) Pushaa muutokset normaalilla kadenssilla. \u00c4l\u00e4 mergaa "
    "suojattuihin haaroihin ilman erillist\u00e4 ohjetta.\n\n"
    "\u00c4l\u00e4 vastaa pelk\u00e4ll\u00e4 DONE. Jos looppi on tietoisesti lopetettava, "
    "vastaa t\u00e4sm\u00e4lleen:\n"
    "LOOP-STOP"
)

DEFAULT_NUDGE_TASK = (
    "Jatka teht\u00e4v\u00e4\u00e4 siit\u00e4 mihin j\u00e4it.\n\n"
    "Tarkista ensin nykyinen tila ja jatka itse teht\u00e4v\u00e4n suorittamista "
    "ilman turhaa selittely\u00e4.\n\n"
    "Jos teht\u00e4v\u00e4 on t\u00e4ysin valmis, vastaa t\u00e4sm\u00e4lleen:\n"
    "DONE\n\n"
    "Jos teht\u00e4v\u00e4 ei ole viel\u00e4 valmis, jatka ty\u00f6skentely\u00e4 ja vie teht\u00e4v\u00e4 "
    "mahdollisimman pitk\u00e4lle."
)
