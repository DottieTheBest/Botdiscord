import os

from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
DEV_GUILD_ID = os.getenv("DEV_GUILD_ID") or None
DB_PATH = os.getenv("DB_PATH", "data/bot.db")

EMBED_COLOR = 0x5865F2
SUCCESS_COLOR = 0x57F287
WARNING_COLOR = 0xFEE75C
ERROR_COLOR = 0xED4245

# --- Tickets ---------------------------------------------------------------
# clé: (nom affiché, emoji, description)
TICKET_CATEGORIES = {
    "support": ("Support", "🛠️", "Need help or an answer"),
    "purchase": ("Purchase", "🛒", "Question or order"),
    "complaint": ("Complaint", "📢", "Report a problem or a member"),
    "partnership": ("Partnership", "🤝", "Partnership proposal"),
    "other": ("Other", "❓", "Any other request"),
}

# Old French keys (tickets already stored in the database) -> new keys
TICKET_CATEGORY_ALIASES = {
    "achat": "purchase",
    "plainte": "complaint",
    "partenariat": "partnership",
    "autre": "other",
}


def ticket_category(key: str) -> tuple:
    key = TICKET_CATEGORY_ALIASES.get(key, key)
    return TICKET_CATEGORIES.get(key, (key.title(), "🎫", ""))
MAX_OPEN_TICKETS_PER_USER = 3
TRANSCRIPT_MAX_MESSAGES = 5000

# --- Modération ------------------------------------------------------------
# nombre d'avertissements -> (sanction, durée en secondes pour un timeout)
PROGRESSIVE_SANCTIONS = {
    3: ("timeout", 60 * 60),        # 3 warns  : timeout 1 h
    5: ("timeout", 60 * 60 * 24),   # 5 warns  : timeout 24 h
    7: ("kick", 0),                 # 7 warns  : expulsion
    10: ("ban", 0),                 # 10 warns : bannissement
}

# Anti-spam
SPAM_WINDOW = 5            # fenêtre d'analyse en secondes
SPAM_MAX_MESSAGES = 5      # messages max dans la fenêtre
SPAM_MAX_DUPLICATES = 3    # même message répété N fois dans la fenêtre
SPAM_MAX_MENTIONS = 5      # mentions max dans un seul message
SPAM_TIMEOUT = 5 * 60      # timeout appliqué si aucune sanction progressive

POLL_MAX_OPTIONS = 5

# --- Giveaways -------------------------------------------------------------
GIVEAWAY_MIN_DURATION = 10                 # secondes
GIVEAWAY_MAX_DURATION = 60 * 24 * 3600     # 60 jours
GIVEAWAY_MAX_WINNERS = 20
GIVEAWAY_MAX_ACTIVE = 25                   # giveaways en cours (ou en pause) par serveur
GIVEAWAY_MAX_IMAGE_BYTES = 8 * 1024 * 1024
GIVEAWAY_ENDED_COLOR = 0x99AAB5
