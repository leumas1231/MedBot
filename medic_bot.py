import discord
import re
import gspread
import os
import json
import time
import threading
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from datetime import datetime, timedelta
from collections import defaultdict

load_dotenv()

# ================= CONFIG =================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = 1439473833273856120  # text channel if needed
SPREADSHEET_ID = "1aXhvKbXqXlHEu94dQctSJP8jk6tLvNWkrYHZyDYcI0c"
GUILD_ID = 861362652710174740  # your real server (guild) ID
VALID_RANKS = [
    "Field Medic",
    "Junior Medic",
    "Senior Medic",
    "Paramedic",
    "Doctor",
]

# ================= SHEET HEADERS =================
# Reports sheet now uses columns A:N.
# The last 4 columns let the bot know who submitted each report
# and which Discord embed should be edited later.
REPORT_HEADERS = [
    "Timestamp",
    "Medics",
    "Job Name",
    "Duration",
    "Points",
    "Clients",
    "Participant Names",
    "Description",
    "Report Date",
    "Message Link",
    "Reporter ID",
    "Reporter Name",
    "Message ID",
    "Channel ID",
]

# Master Log should only use columns A:Q
MASTER_HEADERS = [
    "Medic",
    "Rank",
    "Total Jobs",
    "Total Raw Points",
    "Total Adjusted Points",
    "Total Hours",
    "Raid",
    "LMPF",
    "Healing",
    "Rev/Spar",
    "Escort",
    "World Boss",
    "Arc",
    "Mission",
    "Hosted Event",
    "Host Training Event",
    "Participate In Training Event",
]

# Monthly leaderboard should only use columns A:I
LEADERBOARD_HEADERS = [
    "Rank",
    "Medic",
    "Raw Points",
    "Jobs Logged",
    "Rank Title",
    "Bonus Multiplier",
    "Adjusted Points",
    "Total Pay",
    "Total Ryo",
]

# Shared job options for report submission and report editing.
JOB_OPTIONS = [
    ("Raid / Defend", "Raid / Defend"),
    ("Duty with LMPF", "LMPF"),
    ("Healing Lowbies", "Healing Lowbies"),
    ("Rev Spar", "Rev Spar"),
    ("Escort", "Escort"),
    ("World Boss", "World Boss"),
    ("ARC I (15, 20, 27, 30)", "ARC I"),
    ("ARC II (30, 40, 50)", "ARC II"),
    ("ARC III (60)", "ARC III"),
    ("Mission", "Daily Mission"),
    ("Run An Event", "Hosted Event"),
    ("Host Training Event", "Host Training Event"),
    ("Participate In Training Event", "Participate In Training Event"),
]


# ================= RYO (PER-MONTH) =================
RYO_FILE = "monthly_ryo.json"
monthly_ryo = {}  # {"2026-01": 25000, ...}
DEFAULT_RYO = 25000  # fallback if month not set


def load_monthly_ryo():
    global monthly_ryo
    if os.path.exists(RYO_FILE):
        try:
            with open(RYO_FILE, "r") as f:
                monthly_ryo = json.load(f) or {}
        except Exception:
            monthly_ryo = {}
    else:
        monthly_ryo = {}


def save_monthly_ryo():
    with open(RYO_FILE, "w") as f:
        json.dump(monthly_ryo, f, indent=2)


def get_month_key(year: int, month: int) -> str:
    return f"{year}-{month:02d}"


def get_bank_ryo(year: int, month: int) -> int:
    return int(monthly_ryo.get(get_month_key(year, month), DEFAULT_RYO))


# Load on startup so /setryo persists across restarts
load_monthly_ryo()

# ================= GOOGLE SHEETS AUTH =================
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
CREDS = Credentials.from_service_account_file(
    os.getenv("GOOGLE_APPLICATION_CREDENTIALS"),
    scopes=SCOPES,
)

GC = gspread.authorize(CREDS)

# IMPORTANT: open spreadsheet ONCE and reuse it
SS = GC.open_by_key(SPREADSHEET_ID)
SHEET = SS.worksheet("Reports")  # raw report log worksheet


# ================= SHEET HELPERS =================
def ensure_reports_sheet_shape():
    """Keep the Reports sheet locked to the expected A:N structure."""
    SHEET.update("A1:N1", [REPORT_HEADERS])
    SHEET.resize(cols=len(REPORT_HEADERS))


def ensure_master_sheet_shape():
    """Add/refresh Master Log headers without deleting existing medic data or ranks."""
    try:
        master = SS.worksheet("Leaf Master Medical Log")
        master.resize(cols=len(MASTER_HEADERS))
        master.update("A1:Q1", [MASTER_HEADERS])
    except gspread.exceptions.WorksheetNotFound:
        pass


def get_records_with_expected_headers(ws, expected_headers):
    """
    Read records safely even if Google Sheets visually shows extra empty columns.
    This prevents gspread duplicate blank header errors like duplicates: [''].
    """
    return ws.get_all_records(expected_headers=expected_headers)


# ================= API READ CACHING (QUOTA FIX) =================
_RAW_CACHE = {"ts": 0.0, "records": []}
_RAW_CACHE_TTL = 30  # seconds
_RAW_LOCK = threading.Lock()

_MASTER_CACHE = {"ts": 0.0, "records": []}
_MASTER_TTL = 30  # seconds
_MASTER_LOCK = threading.Lock()


def get_raw_records_cached(force: bool = False):
    """Return Reports records, but cache for TTL seconds to avoid quota spikes."""
    now = time.time()
    with _RAW_LOCK:
        if (not force) and _RAW_CACHE["records"] and (now - _RAW_CACHE["ts"] < _RAW_CACHE_TTL):
            return _RAW_CACHE["records"]

        records = get_records_with_expected_headers(SHEET, REPORT_HEADERS)
        _RAW_CACHE["records"] = records
        _RAW_CACHE["ts"] = now
        return records


def invalidate_raw_cache():
    with _RAW_LOCK:
        _RAW_CACHE["ts"] = 0.0
        _RAW_CACHE["records"] = []


def get_master_records_cached(master_ws, force: bool = False):
    """Cache master worksheet get_all_records too."""
    now = time.time()
    with _MASTER_LOCK:
        if (not force) and _MASTER_CACHE["records"] and (now - _MASTER_CACHE["ts"] < _MASTER_TTL):
            return _MASTER_CACHE["records"]

        recs = get_records_with_expected_headers(master_ws, MASTER_HEADERS)
        _MASTER_CACHE["records"] = recs
        _MASTER_CACHE["ts"] = now
        return recs


def invalidate_master_cache():
    with _MASTER_LOCK:
        _MASTER_CACHE["ts"] = 0.0
        _MASTER_CACHE["records"] = []


# ================= REBUILD THROTTLE (QUOTA FIX) =================
_LAST_REBUILD = {"master": 0.0, "leaderboard": 0.0}
REBUILD_COOLDOWN = 60  # seconds


def should_run(key: str) -> bool:
    now = time.time()
    if now - _LAST_REBUILD[key] >= REBUILD_COOLDOWN:
        _LAST_REBUILD[key] = now
        return True
    return False


# ================= NAME NORMALIZATION =================
def load_medic_normalization():
    """Reads medic names from cached raw records & builds a normalization map."""
    records = get_raw_records_cached()
    mapping = {}

    for row in records:
        medics = row.get("Medics", "")
        for m in [x.strip() for x in medics.split(",") if x.strip()]:
            key = m.lower()
            if key not in mapping:
                mapping[key] = m  # store original capitalization
    return mapping


def normalize_medic_name(name: str, mapping: dict) -> str:
    """Converts a medic name to correct capitalization."""
    key = name.lower()
    if key in mapping:
        return mapping[key]  # already known medic → use canonical case

    proper = name.title()
    mapping[key] = proper
    return proper


# ================= POINT CALCULATOR =================
def calculate_points(job_name: str, duration: int, clients: int) -> int:
    job_name = job_name.lower().strip()

    # Training events — fixed points
    if "host training event" in job_name:
        return 35
    if "participate in training event" in job_name:
        return 20

    # Hosted Event — 30 points, must be at least 60 min and 5+ clients
    if "hosted event" in job_name:
        if duration >= 60 and clients >= 5:
            return 30
        return 0

    if "raid" in job_name or "defend" in job_name:
        return 5 + 4 * (duration // 15)
    if "criminal" in job_name or "lmpf" in job_name:
        return 3
    if "healing" in job_name or "lowbie" in job_name or "farm" in job_name:
        return clients + (duration // 15)
    if "rev" in job_name or "spar" in job_name:
        return clients + (duration // 15)
    if "escort" in job_name:
        return 2
    if "boss" in job_name or "world" in job_name:
        return clients * 3

    # Categorized arcs — points are awarded per client
    if job_name == "arc i":
        return clients * 10
    if job_name == "arc ii":
        return clients * 20
    if job_name == "arc iii":
        return clients * 30

    # Legacy Arc reports keep the old behavior if an older report is edited.
    if job_name == "arc":
        return clients * 30

    if "mission" in job_name or "daily" in job_name:
        return clients * 3

    return 0


# ================= RANK BONUS =================
def bonus_from_rank(rank: str) -> float:
    """Return bonus multiplier based on Rank string from Master Log."""
    r = (rank or "").lower()
    if "doctor" in r:
        return 3.0
    if "paramedic" in r:
        return 2.0
    if "senior" in r:
        return 1.5
    if "junior" in r:
        return 1.25
    if "field" in r:
        return 1.15
    return 1.0  # Unranked / unknown


# ================= MONTHLY LEADERBOARD =================
def update_leaderboard():
    records = get_raw_records_cached()
    now = datetime.now()
    current_month = now.month
    current_year = now.year
    current_month_name = now.strftime("%b")

    sheet_title = f"Leaderboard - {current_month_name} {current_year}"
    BANK_RYO = get_bank_ryo(current_year, current_month)

    # Load ranks from Master Log (if exists)
    rank_by_medic = {}
    try:
        master = SS.worksheet("Leaf Master Medical Log")
        master_records = get_master_records_cached(master)
        for row in master_records:
            medic_name = row.get("Medic", "").strip()
            if medic_name:
                rank_by_medic[medic_name] = row.get("Rank", "Unranked")
    except gspread.exceptions.WorksheetNotFound:
        rank_by_medic = {}

    # Create or open the monthly leaderboard sheet
    try:
        leaderboard_sheet = SS.worksheet(sheet_title)
    except gspread.exceptions.WorksheetNotFound:
        leaderboard_sheet = SS.add_worksheet(title=sheet_title, rows="200", cols=str(len(LEADERBOARD_HEADERS)))
        leaderboard_sheet.update("A1:I1", [LEADERBOARD_HEADERS])

    points_by_medic = defaultdict(int)
    jobs_by_medic = defaultdict(int)

    for row in records:
        date_str = str(row.get("Report Date", "")).strip()
        if not date_str:
            continue

        try:
            d = datetime.strptime(date_str, "%m/%d/%Y")
        except ValueError:
            continue

        if d.month == current_month and d.year == current_year:
            medics_raw = row.get("Medics", "")
            try:
                points = int(row.get("Points", 0))
            except ValueError:
                points = 0

            for medic in [m.strip() for m in medics_raw.split(",") if m.strip()]:
                points_by_medic[medic] += points
                jobs_by_medic[medic] += 1

    # IMPORTANT: Never clear/overwrite a leaderboard if there is no data
    if not points_by_medic:
        print("⚠️ No data for current month — leaderboard left unchanged")
        return [], {}

    adjusted_points = {}
    for medic, raw in points_by_medic.items():
        rank = rank_by_medic.get(medic, "Unranked")
        mult = bonus_from_rank(rank)
        adjusted_points[medic] = raw * mult

    total_adjusted = sum(adjusted_points.values())
    sorted_data = sorted(adjusted_points.items(), key=lambda x: x[1], reverse=True)

    output = [LEADERBOARD_HEADERS]

    for i, (medic, adj) in enumerate(sorted_data, start=1):
        raw = points_by_medic[medic]
        jobs = jobs_by_medic[medic]
        rank_title = rank_by_medic.get(medic, "Unranked")
        mult = bonus_from_rank(rank_title)
        share = adj / total_adjusted if total_adjusted > 0 else 0
        pay = round(share * BANK_RYO, 2)

        output.append([
            i,
            medic,
            raw,
            jobs,
            rank_title,
            mult,
            round(adj, 2),
            pay,
            BANK_RYO if i == 1 else "",
        ])

    leaderboard_sheet.clear()
    leaderboard_sheet.update("A1", output)
    leaderboard_sheet.resize(cols=len(LEADERBOARD_HEADERS))

    print(f"✅ Leaderboard updated for {current_month_name} {current_year} (Pool: {BANK_RYO})")
    return sorted_data, jobs_by_medic


def update_single_leaderboard(year: int, month: int):
    records = get_raw_records_cached()
    BANK_RYO = get_bank_ryo(year, month)

    sheet_title = f"Leaderboard - {datetime(year, month, 1).strftime('%b')} {year}"

    # Load ranks
    try:
        master = SS.worksheet("Leaf Master Medical Log")
        master_records = get_master_records_cached(master)
        rank_by_medic = {
            row.get("Medic", ""): row.get("Rank", "Unranked")
            for row in master_records
        }
    except gspread.exceptions.WorksheetNotFound:
        rank_by_medic = {}

    # Create or open the sheet
    try:
        leaderboard_sheet = SS.worksheet(sheet_title)
    except gspread.exceptions.WorksheetNotFound:
        leaderboard_sheet = SS.add_worksheet(sheet_title, rows=200, cols=len(LEADERBOARD_HEADERS))

    # Collect raw data for this month
    points_by_medic = defaultdict(int)
    jobs_by_medic = defaultdict(int)

    for row in records:
        date_str = str(row.get("Report Date", "")).strip()
        if not date_str:
            continue

        try:
            d = datetime.strptime(date_str, "%m/%d/%Y")
        except ValueError:
            continue

        if d.year == year and d.month == month:
            medics = [m.strip() for m in row.get("Medics", "").split(",") if m.strip()]
            try:
                pts = int(row.get("Points", 0))
            except ValueError:
                pts = 0

            for medic in medics:
                points_by_medic[medic] += pts
                jobs_by_medic[medic] += 1

    # IMPORTANT: do NOT clear/overwrite if empty
    if not points_by_medic:
        print(f"⚠️ No data for {sheet_title} — skipping update")
        return

    adjusted = {}
    for medic, raw_pts in points_by_medic.items():
        rank = rank_by_medic.get(medic, "Unranked")
        mult = bonus_from_rank(rank)
        adjusted[medic] = raw_pts * mult

    total_adj = sum(adjusted.values())

    output = [LEADERBOARD_HEADERS]

    sorted_medics = sorted(adjusted.items(), key=lambda x: x[1], reverse=True)

    for i, (medic, adj_pts) in enumerate(sorted_medics, start=1):
        raw_pts = points_by_medic[medic]
        jobs = jobs_by_medic[medic]
        rank_title = rank_by_medic.get(medic, "Unranked")
        mult = bonus_from_rank(rank_title)
        share = adj_pts / total_adj if total_adj else 0
        pay = round(share * BANK_RYO, 2)

        output.append([
            i,
            medic,
            raw_pts,
            jobs,
            rank_title,
            mult,
            round(adj_pts, 2),
            pay,
            BANK_RYO if i == 1 else "",
        ])

    leaderboard_sheet.clear()
    leaderboard_sheet.update("A1", output)
    leaderboard_sheet.resize(cols=len(LEADERBOARD_HEADERS))

    print(f"✅ Updated leaderboard: {sheet_title} (Pool: {BANK_RYO})")


def update_all_leaderboards():
    """Rebuild leaderboard sheets for every month found in the raw log."""
    records = get_raw_records_cached()

    months = set()
    for row in records:
        date_str = str(row.get("Report Date", "")).strip()
        if not date_str:
            continue
        try:
            d = datetime.strptime(date_str, "%m/%d/%Y")
            months.add((d.year, d.month))
        except ValueError:
            continue

    for year, month in sorted(months):
        title = f"Leaderboard - {datetime(year, month, 1).strftime('%b')} {year}"
        print(f"📅 Updating leaderboard for: {title}")
        update_single_leaderboard(year, month)


# ================= MASTER LOG (LIFETIME) =================
def update_master_log():
    # Ensure master sheet exists & capture existing ranks
    try:
        master = SS.worksheet("Leaf Master Medical Log")
        existing_records = get_master_records_cached(master)
        existing_ranks = {}

        name_map = load_medic_normalization()

        for row in existing_records:
            raw_name = row.get("Medic", "").strip()
            if not raw_name:
                continue

            normalized = normalize_medic_name(raw_name, name_map)
            existing_ranks[normalized] = row.get("Rank", "Unranked")
    except gspread.exceptions.WorksheetNotFound:
        master = SS.add_worksheet(title="Leaf Master Medical Log", rows="300", cols=str(len(MASTER_HEADERS)))
        existing_ranks = {}
        master.update("A1:Q1", [MASTER_HEADERS])
        invalidate_master_cache()

    records = get_raw_records_cached()

    print("DEBUG: raw rows =", len(records))
    print("DEBUG: first row keys =", records[0].keys() if records else "NO ROWS")

    raw_points = defaultdict(int)
    jobs = defaultdict(int)
    hours = defaultdict(float)
    hours_by_type = defaultdict(lambda: defaultdict(float))

    for row in records:
        medics_raw = row.get("Medics", "")
        job_name = str(row.get("Job Name", "")).lower()
        try:
            points = int(row.get("Points", 0))
        except ValueError:
            points = 0

        duration_str = str(row.get("Duration", "0 min"))
        try:
            minutes = int(duration_str.split()[0])
        except (ValueError, IndexError):
            minutes = 0
        job_hours = minutes / 60.0

        medics = [m.strip() for m in medics_raw.split(",") if m.strip()]
        for medic in medics:
            raw_points[medic] += points
            jobs[medic] += 1
            hours[medic] += job_hours

            if "raid" in job_name or "defend" in job_name:
                hours_by_type[medic]["Raid"] += job_hours
            elif "lmpf" in job_name:
                hours_by_type[medic]["LMPF"] += job_hours
            elif "healing" in job_name or "lowbie" in job_name:
                hours_by_type[medic]["Healing"] += job_hours
            elif "rev" in job_name or "spar" in job_name:
                hours_by_type[medic]["Rev/Spar"] += job_hours
            elif "escort" in job_name:
                hours_by_type[medic]["Escort"] += job_hours
            elif "world" in job_name:
                hours_by_type[medic]["World Boss"] += job_hours
            elif "arc" in job_name:
                hours_by_type[medic]["Arc"] += job_hours
            elif "mission" in job_name:
                hours_by_type[medic]["Mission"] += job_hours
            elif "hosted event" in job_name:
                hours_by_type[medic]["Hosted Event"] += job_hours
            elif "host training event" in job_name:
                hours_by_type[medic]["Host Training Event"] += job_hours
            elif "participate in training event" in job_name:
                hours_by_type[medic]["Participate In Training Event"] += job_hours

    output = [MASTER_HEADERS]

    for medic in sorted(jobs.keys()):
        rank = existing_ranks.get(medic, "Unranked")
        bonus_mult = bonus_from_rank(rank)
        adjusted = raw_points[medic] * bonus_mult

        output.append([
            medic,
            rank,
            jobs[medic],
            raw_points[medic],
            adjusted,
            round(hours[medic], 2),
            round(hours_by_type[medic]["Raid"], 2),
            round(hours_by_type[medic]["LMPF"], 2),
            round(hours_by_type[medic]["Healing"], 2),
            round(hours_by_type[medic]["Rev/Spar"], 2),
            round(hours_by_type[medic]["Escort"], 2),
            round(hours_by_type[medic]["World Boss"], 2),
            round(hours_by_type[medic]["Arc"], 2),
            round(hours_by_type[medic]["Mission"], 2),
            round(hours_by_type[medic]["Hosted Event"], 2),
            round(hours_by_type[medic]["Host Training Event"], 2),
            round(hours_by_type[medic]["Participate In Training Event"], 2),
        ])

    if len(output) <= 1:
        print("🚫 Master log rebuild aborted — no parsed data")
        return

    # IMPORTANT: only rebuild if ranks exist; otherwise you'd wipe ranks
    if len(existing_ranks) > 0:
        master.clear()
        master.update("A1", output)
        master.resize(cols=len(MASTER_HEADERS))
        invalidate_master_cache()
        print("✅ Leaf Master Medical Log updated")
    else:
        print("🚫 Aborting master log rebuild — ranks would be lost")


def set_rank_in_master_log(medic_name: str, rank: str) -> str:
    master = SS.worksheet("Leaf Master Medical Log")

    # Normalize medic name
    name_map = load_medic_normalization()
    medic = normalize_medic_name(medic_name.strip(), name_map)

    # Normalize rank to canonical value
    rank = next((r for r in VALID_RANKS if r.lower() == rank.lower()), None)
    if not rank:
        raise ValueError("Invalid rank")

    records = get_master_records_cached(master)

    target_row = None
    for i, row in enumerate(records, start=2):
        existing = str(row.get("Medic", "")).strip()
        if not existing:
            continue

        existing_norm = normalize_medic_name(existing, name_map)
        if existing_norm.lower() == medic.lower():
            target_row = i
            break

    if target_row is None:
        master.append_row([medic, rank], value_input_option="USER_ENTERED")
        master.resize(cols=len(MASTER_HEADERS))
        invalidate_master_cache()
        return f"Added **{medic}** with rank **{rank}**."
    else:
        master.update_cell(target_row, 2, rank)
        invalidate_master_cache()
        return f"Updated **{medic}** rank to **{rank}**."



# ================= REPORT EDIT HELPERS =================
def clean_sheet_id(value) -> str:
    text = str(value or "").strip()

    # Remove leading apostrophe if we added one to force Google Sheets text
    if text.startswith("'"):
        text = text[1:]

    # Remove trailing .0 if Google Sheets gave us a float-looking value
    if text.endswith(".0"):
        text = text[:-2]

    return text

def split_names(value: str):
    """Split comma-separated names, also allowing the word 'and'."""
    return [x.strip() for x in re.split(r",|\band\b", str(value or "")) if x.strip()]


def parse_report_duration_to_minutes(duration_value) -> int:
    """Convert values like '60 min' or '60' into an integer minute count."""
    text = str(duration_value or "0").strip()
    match = re.search(r"\d+", text)
    return int(match.group(0)) if match else 0


def parse_clients_count(clients_value) -> int:
    """Accept either a number or a comma-separated client list."""
    text = str(clients_value or "").strip()
    if not text:
        return 0
    if text.isdigit():
        return int(text)
    return len(split_names(text))


def parse_report_date_value(value):
    """Accept MM/DD/YYYY or YYYY-MM-DD."""
    text = str(value or "").strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def build_report_embed(
    job_type: str,
    desc: str,
    report_date,
    medic_list,
    duration: int,
    clients_count: int,
    points: int,
):
    """Build the public Discord embed for a submitted or edited report."""
    embed = discord.Embed(
        title=f"Medic Report — {job_type}",
        description=desc,
        color=0x00FFAA,
    )
    embed.add_field(name="Date", value=report_date.strftime("%B %d, %Y"))
    embed.add_field(name="Medics", value=", ".join(medic_list), inline=False)
    embed.add_field(name="Duration", value=f"{duration} min")
    embed.add_field(name="Clients", value=str(clients_count))
    embed.add_field(name="Points", value=str(points))
    embed.timestamp = datetime.now()
    return embed


def find_report_row_by_message_id(message_id: str):
    """
    Find a report by Discord Message ID.
    Returns (sheet_row_number, row_dict), or (None, None).
    """
    records = get_raw_records_cached(force=True)
    target = str(message_id).strip()

    for index, row in enumerate(records, start=2):
        if clean_sheet_id(row.get("Message ID", "")) == target:
            return index, row

    return None, None


def get_recent_reports_for_user(user_id: int, limit: int = 10):
    """Return the user's recent reports, newest first, with sheet row numbers."""
    records = get_raw_records_cached(force=True)
    user_id_text = str(user_id)
    matches = []

    for index, row in enumerate(records, start=2):
        if clean_sheet_id(row.get("Reporter ID", "")) == user_id_text:
            matches.append((index, row))

    return list(reversed(matches))[:limit]


def user_can_edit_report(interaction: discord.Interaction, row: dict) -> bool:
    """Original reporter or server admin can edit a report."""
    if interaction.user.guild_permissions.administrator:
        return True
    return clean_sheet_id(row.get("Reporter ID", "")) == str(interaction.user.id)


async def rebuild_after_report_change():
    """Rebuild derived sheets after a report is added/edited, but respect throttling."""
    if should_run("master"):
        update_master_log()
    if should_run("leaderboard"):
        update_leaderboard()

# ================= DISCORD BOT =================
intents = discord.Intents.default()
intents.message_content = True

bot = discord.Client(intents=intents)
tree = discord.app_commands.CommandTree(bot)


# ================= COMMANDS =================
@tree.command(
    name="setrank",
    description="Set a medic's rank in the Master Medical Log (admin only)"
)
@discord.app_commands.describe(
    medic="Medic name (case-insensitive)",
    rank="Select the medic's rank"
)
@discord.app_commands.choices(
    rank=[
        discord.app_commands.Choice(name="Field Medic", value="Field Medic"),
        discord.app_commands.Choice(name="Junior Medic", value="Junior Medic"),
        discord.app_commands.Choice(name="Senior Medic", value="Senior Medic"),
        discord.app_commands.Choice(name="Paramedic", value="Paramedic"),
        discord.app_commands.Choice(name="Doctor", value="Doctor"),
    ]
)
@discord.app_commands.guilds(discord.Object(id=GUILD_ID))
async def setrank(
    interaction: discord.Interaction,
    medic: str,
    rank: discord.app_commands.Choice[str],
):
    await interaction.response.defer(ephemeral=True)

    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("🚫 Admins only.")
        return

    try:
        msg = set_rank_in_master_log(medic, rank.value)

        # Recalculate derived data immediately (throttled)
        if should_run("master"):
            update_master_log()
        if should_run("leaderboard"):
            update_leaderboard()

        await interaction.followup.send(
            f"✅ {msg}\nBonus applied: **×{bonus_from_rank(rank.value)}**"
        )
    except Exception as e:
        await interaction.followup.send(f"⚠️ Error setting rank: {e}")


@tree.command(name="updatelogs", description="Force update ALL leaderboard sheets and the master log.")
@discord.app_commands.guilds(discord.Object(id=GUILD_ID))
async def update_logs(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        # force fresh cache reads for a manual rebuild
        get_raw_records_cached(force=True)
        update_master_log()
        update_all_leaderboards()
        await interaction.followup.send("✅ All logs and leaderboards updated!")
    except Exception as e:
        await interaction.followup.send(f"⚠️ Error: {e}")


@tree.command(
    name="rebuildleaderboard",
    description="Rebuild a specific month's leaderboard (admin use)"
)
@discord.app_commands.describe(year="Year (e.g. 2026)", month="Month number (1–12)")
@discord.app_commands.guilds(discord.Object(id=GUILD_ID))
async def rebuild_leaderboard(interaction: discord.Interaction, year: int, month: int):
    await interaction.response.defer(ephemeral=True)

    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("🚫 Admins only.")
        return

    if month < 1 or month > 12:
        await interaction.followup.send("❌ Month must be between 1 and 12.")
        return

    try:
        get_raw_records_cached(force=True)
        update_single_leaderboard(year, month)
        month_name = datetime(year, month, 1).strftime("%B")
        await interaction.followup.send(f"✅ Rebuilt leaderboard for **{month_name} {year}**")
    except Exception as e:
        await interaction.followup.send(f"⚠️ Error rebuilding leaderboard: {e}")


@tree.command(
    name="setryo",
    description="Set Ryo payout for a specific month (admin only)"
)
@discord.app_commands.describe(
    year="Year (e.g. 2026)",
    month="Month (1–12)",
    amount="Total Ryo for that month"
)
@discord.app_commands.guilds(discord.Object(id=GUILD_ID))
async def set_ryo(interaction: discord.Interaction, year: int, month: int, amount: int):
    await interaction.response.defer(ephemeral=True)

    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send("🚫 Admins only.")
        return

    if month < 1 or month > 12:
        await interaction.followup.send("❌ Month must be 1–12.")
        return

    if amount <= 0:
        await interaction.followup.send("❌ Ryo must be positive.")
        return

    key = get_month_key(year, month)
    monthly_ryo[key] = amount
    save_monthly_ryo()

    month_name = datetime(year, month, 1).strftime("%B")
    await interaction.followup.send(
        f"💰 Ryo for **{month_name} {year}** set to **{amount:,} Ryo**"
    )


@tree.command(name="leaderboard", description="Show this month's medic leaderboard")
@discord.app_commands.guilds(discord.Object(id=GUILD_ID))
async def leaderboard_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)
    try:
        sorted_data, jobs_by_medic = update_leaderboard()

        if not sorted_data:
            await interaction.followup.send("📋 No medic data found for this month.")
            return

        lines = []
        for i, (medic, points) in enumerate(sorted_data[:10], start=1):
            job_count = jobs_by_medic.get(medic, 0)
            lines.append(f"**{i}. {medic}** — {points} pts ({job_count} jobs)")

        embed = discord.Embed(
            title=f"🏆 Medic Leaderboard — {datetime.now().strftime('%B %Y')}",
            description="\n".join(lines),
            color=0xFFD700,
        )
        embed.set_footer(text="Data pulled from Google Sheets")
        await interaction.followup.send(embed=embed)

    except Exception as e:
        await interaction.followup.send(f"⚠️ Error loading leaderboard: {e}")


@tree.command(name="medicstats", description="View lifetime stats for a specific medic")
@discord.app_commands.describe(name="The medic's name")
@discord.app_commands.guilds(discord.Object(id=GUILD_ID))
async def medicstats(interaction: discord.Interaction, name: str):
    await interaction.response.defer(ephemeral=False)

    try:
        master = SS.worksheet("Leaf Master Medical Log")
        records = get_master_records_cached(master)

        if not records:
            await interaction.followup.send("⚠️ No lifetime data found.")
            return

        target = None
        for row in records:
            medic_name = row.get("Medic", "")
            if name.lower() in str(medic_name).lower():
                target = row
                break

        if not target:
            await interaction.followup.send(f"❌ No medic found matching: **{name}**")
            return

        medic = target.get("Medic", "Unknown")
        rank = target.get("Rank", "Unranked")
        jobs = target.get("Total Jobs", 0)
        raw = target.get("Total Raw Points", 0)
        adj = target.get("Total Adjusted Points", 0)
        hours = target.get("Total Hours", 0)

        raid_h = target.get("Raid", 0)
        lmpf_h = target.get("LMPF", 0)
        heal_h = target.get("Healing", 0)
        rev_h = target.get("Rev/Spar", 0)
        escort_h = target.get("Escort", 0)
        boss_h = target.get("World Boss", 0)
        arc_h = target.get("Arc", 0)
        mission_h = target.get("Mission", 0)
        event_h = target.get("Hosted Event", 0)
        host_training_h = target.get("Host Training Event", 0)
        participate_training_h = target.get("Participate In Training Event", 0)

        embed = discord.Embed(title=f"💠 Lifetime Stats — {medic}", color=0x3498DB)
        embed.add_field(name="Rank", value=rank, inline=True)
        embed.add_field(name="Total Jobs", value=jobs, inline=True)
        embed.add_field(name="Total Raw Points", value=raw, inline=True)
        embed.add_field(name="Total Adjusted Points", value=adj, inline=True)
        embed.add_field(name="Total Hours", value=hours, inline=True)
        embed.add_field(
            name="Hours Breakdown",
            value=(
                f"• **Raid:** {raid_h}\n"
                f"• **LMPF:** {lmpf_h}\n"
                f"• **Healing:** {heal_h}\n"
                f"• **Rev/Spar:** {rev_h}\n"
                f"• **Escort:** {escort_h}\n"
                f"• **World Boss:** {boss_h}\n"
                f"• **Arc:** {arc_h}\n"
                f"• **Mission:** {mission_h}\n"
                f"• **Hosted Event:** {event_h}\n"
                f"• **Host Training Event:** {host_training_h}\n"
                f"• **Participate In Training Event:** {participate_training_h}"
            ),
            inline=False,
        )
        embed.set_footer(text="Lifetime stats from the Master Medical Log")
        await interaction.followup.send(embed=embed)

    except Exception as e:
        await interaction.followup.send(f"⚠️ Error: {e}")



@tree.command(name="myreports", description="Show your recent medic reports that can be edited")
@discord.app_commands.guilds(discord.Object(id=GUILD_ID))
async def myreports(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    try:
        reports = get_recent_reports_for_user(interaction.user.id, limit=10)

        if not reports:
            await interaction.followup.send(
                "I could not find any editable reports for you. Only new reports submitted after this update will show here.",
                ephemeral=True,
            )
            return

        lines = []
        for sheet_row, row in reports:
            message_id = clean_sheet_id(row.get("Message ID", ""))
            report_date = str(row.get("Report Date", "")).strip()
            job_name = str(row.get("Job Name", "")).strip()
            medics = str(row.get("Medics", "")).strip()
            points = str(row.get("Points", "")).strip()

            lines.append(
                f"**Report ID:** `{message_id}`\n"
                f"• **Date:** {report_date}\n"
                f"• **Job:** {job_name}\n"
                f"• **Medics:** {medics}\n"
                f"• **Points:** {points}\n"
            )

        await interaction.followup.send(
            "Use `/editreport report_id:` with one of these Report IDs:\n\n" + "\n".join(lines),
            ephemeral=True,
        )

    except Exception as e:
        await interaction.followup.send(f"⚠️ Error loading your reports: {e}", ephemeral=True)


@tree.command(name="editreport", description="Edit one of your medic reports by Report ID")
@discord.app_commands.describe(report_id="Use /myreports to find the Report ID")
@discord.app_commands.guilds(discord.Object(id=GUILD_ID))
async def editreport(interaction: discord.Interaction, report_id: str):
    row_number, existing_row = find_report_row_by_message_id(report_id)

    if row_number is None or existing_row is None:
        await interaction.response.send_message(
            "❌ I could not find that report. Use `/myreports` to get the correct Report ID.",
            ephemeral=True,
        )
        return

    if not user_can_edit_report(interaction, existing_row):
        await interaction.response.send_message(
            "🚫 You can only edit reports you submitted. Admins can edit any report.",
            ephemeral=True,
        )
        return

    current_job = str(existing_row.get("Job Name", "")).strip() or "Raid / Defend"

    async def open_edit_modal(trigger_interaction: discord.Interaction, selected_job_type: str):
        """Open the edit modal using either the current job or a newly selected job."""

        class EditReportModal(discord.ui.Modal, title="Edit Medic Report"):
            medics = discord.ui.TextInput(
                label="Medic Names (Separate by ,)",
                default=str(existing_row.get("Medics", ""))[:4000],
            )
            report_date = discord.ui.TextInput(
                label="Report Date (MM/DD/YYYY)",
                default=str(existing_row.get("Report Date", ""))[:4000],
            )
            duration = discord.ui.TextInput(
                label="Duration in minutes",
                default=str(parse_report_duration_to_minutes(existing_row.get("Duration", ""))),
            )
            clients = discord.ui.TextInput(
                label="Clients (names or number)",
                default=str(existing_row.get("Participant Names", "") or existing_row.get("Clients", ""))[:4000],
            )
            description = discord.ui.TextInput(
                label="Description",
                style=discord.TextStyle.long,
                default=str(existing_row.get("Description", ""))[:4000],
            )

            async def on_submit(self, modal_interaction: discord.Interaction):
                try:
                    await modal_interaction.response.defer(ephemeral=True)

                    name_map = load_medic_normalization()
                    medic_list = [
                        normalize_medic_name(m.strip(), name_map)
                        for m in split_names(self.medics.value)
                    ]

                    if not medic_list:
                        await modal_interaction.followup.send(
                            "⚠️ You need at least one medic name.",
                            ephemeral=True,
                        )
                        return

                    date_obj = parse_report_date_value(self.report_date.value)
                    if not date_obj:
                        await modal_interaction.followup.send(
                            "⚠️ Invalid date format. Use `MM/DD/YYYY` or `YYYY-MM-DD`.",
                            ephemeral=True,
                        )
                        return

                    try:
                        duration_minutes = int(str(self.duration.value).strip())
                    except ValueError:
                        await modal_interaction.followup.send(
                            "⚠️ Duration must be a number of minutes.",
                            ephemeral=True,
                        )
                        return

                    if duration_minutes < 0:
                        await modal_interaction.followup.send(
                            "⚠️ Duration cannot be negative.",
                            ephemeral=True,
                        )
                        return

                    clients_text = str(self.clients.value).strip()

                    if clients_text.isdigit():
                        clients_count = int(clients_text)
                        participant_names = clients_text
                    else:
                        client_list = split_names(clients_text)
                        clients_count = len(client_list)
                        participant_names = ", ".join(client_list)

                    job_type = selected_job_type
                    points = calculate_points(job_type, duration_minutes, clients_count)
                    desc = str(self.description.value).strip()

                    old_link = str(existing_row.get("Message Link", "")).strip()
                    old_reporter_id = clean_sheet_id(existing_row.get("Reporter ID", "")).strip()
                    old_reporter_name = str(existing_row.get("Reporter Name", "")).strip()
                    old_message_id = clean_sheet_id(existing_row.get("Message ID", "")).strip()
                    old_channel_id = clean_sheet_id(existing_row.get("Channel ID", "")).strip()

                    updated_row = [
                        str(existing_row.get("Timestamp", "")).strip() or datetime.now().strftime("%m/%d/%Y %H:%M"),
                        ", ".join(medic_list),
                        job_type,
                        f"{duration_minutes} min",
                        points,
                        clients_count,
                        participant_names,
                        desc,
                        date_obj.strftime("%m/%d/%Y"),
                        old_link,
                        f"'{old_reporter_id}",
                        old_reporter_name,
                        f"'{old_message_id}",
                        f"'{old_channel_id}",
                    ]

                    if len(updated_row) != len(REPORT_HEADERS):
                        raise ValueError(
                            f"Edited report row has {len(updated_row)} columns, expected {len(REPORT_HEADERS)}"
                        )

                    SHEET.update(
                        f"A{row_number}:N{row_number}",
                        [updated_row],
                        value_input_option="USER_ENTERED",
                    )
                    SHEET.resize(cols=len(REPORT_HEADERS))
                    invalidate_raw_cache()

                    # Try to update the original public Discord embed.
                    embed_edit_note = ""
                    try:
                        channel = bot.get_channel(int(old_channel_id)) or await bot.fetch_channel(int(old_channel_id))
                        msg = await channel.fetch_message(int(old_message_id))
                        embed = build_report_embed(
                            job_type,
                            desc,
                            date_obj,
                            medic_list,
                            duration_minutes,
                            clients_count,
                            points,
                        )
                        await msg.edit(embed=embed)
                    except Exception as embed_error:
                        embed_edit_note = "\n⚠️ Sheet updated, but I could not edit the original Discord embed."
                        print(f"⚠️ Sheet updated, but could not edit Discord embed: {embed_error}")

                    await rebuild_after_report_change()

                    await modal_interaction.followup.send(
                        f"✅ Report `{old_message_id}` updated. Job: **{job_type}**. New points: **{points}**.{embed_edit_note}",
                        ephemeral=True,
                    )

                except Exception as e:
                    await modal_interaction.followup.send(f"⚠️ Error editing report: {e}", ephemeral=True)

        await trigger_interaction.response.send_modal(EditReportModal())

    class EditJobSelect(discord.ui.Select):
        def __init__(self):
            options = [
                discord.SelectOption(
                    label=label,
                    value=value,
                    default=(value == current_job),
                )
                for label, value in JOB_OPTIONS
            ]

            super().__init__(
                placeholder="Optional: choose a different job type...",
                options=options,
            )

        async def callback(self, select_interaction: discord.Interaction):
            if select_interaction.user.id != interaction.user.id:
                await select_interaction.response.send_message(
                    "🚫 This edit menu is not for you.",
                    ephemeral=True,
                )
                return

            await open_edit_modal(select_interaction, self.values[0])

    class EditReportView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=120)
            self.add_item(EditJobSelect())

        @discord.ui.button(label="Keep current job and edit details", style=discord.ButtonStyle.primary)
        async def keep_current_job(self, button_interaction: discord.Interaction, button: discord.ui.Button):
            if button_interaction.user.id != interaction.user.id:
                await button_interaction.response.send_message(
                    "🚫 This edit menu is not for you.",
                    ephemeral=True,
                )
                return

            await open_edit_modal(button_interaction, current_job)

    await interaction.response.send_message(
        f"Current job type: **{current_job}**\n\n"
        "Click **Keep current job and edit details** to edit minutes/clients/etc. without changing the job.\n"
        "Or use the dropdown only if you need to change the job type.",
        view=EditReportView(),
        ephemeral=True,
    )


@tree.command(name="report", description="Submit a medic report")
@discord.app_commands.guilds(discord.Object(id=GUILD_ID))
async def report(interaction: discord.Interaction):

    class JobSelect(discord.ui.Select):
        def __init__(self):
            options = [
                discord.SelectOption(label=label, value=value)
                for label, value in JOB_OPTIONS
            ]
            super().__init__(placeholder="Choose Job Type...", options=options)

        async def callback(self, select_interaction: discord.Interaction):
            job_type = self.values[0]

            class ReportModal(discord.ui.Modal, title="Medic Job Report"):
                medics = discord.ui.TextInput(
                    label="Medic Names(Separate by ,)",
                    placeholder="Example: Leumas, LeaKiara, Ragnor Reaper",
                )
                date = discord.ui.TextInput(
                    label="Date (blank = today, MM/DD/YYYY)",
                    required=False,
                    placeholder="01/15/2025",
                )
                time_range = discord.ui.TextInput(
                    label="Time Range (HH:MM or H:MM AM/PM)",
                    placeholder="5:00 pm - 6:00 pm",
                )
                clients = discord.ui.TextInput(
                    label="Clients(Separate by ,)",
                    placeholder="Example: Leumas, LeaKiara, Ragnor Reaper",
                )
                description = discord.ui.TextInput(
                    label="Description", style=discord.TextStyle.long
                )

                def parse_time(self, t):
                    for fmt in ("%H:%M", "%I:%M %p"):
                        try:
                            return datetime.strptime(t.strip(), fmt)
                        except ValueError:
                            pass
                    return None

                def parse_date(self, d):
                    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
                        try:
                            return datetime.strptime(d.strip(), fmt).date()
                        except ValueError:
                            pass
                    return None

                async def on_submit(self, modal_interaction: discord.Interaction):
                    try:
                        await modal_interaction.response.defer(ephemeral=True)

                        name_map = load_medic_normalization()
                        medic_list = [
                            normalize_medic_name(m.strip(), name_map)
                            for m in split_names(self.medics.value)
                        ]

                        clients_list = [
                            p.strip()
                            for p in split_names(self.clients.value)
                        ]

                        date_obj = (
                            self.parse_date(self.date.value)
                            if self.date.value.strip()
                            else datetime.now().date()
                        )

                        if not date_obj:
                            await modal_interaction.followup.send(
                                "⚠️ Invalid date format. Use `MM/DD/YYYY`, `YYYY-MM-DD`, or leave it blank for today.",
                                ephemeral=True,
                            )
                            return

                        t = re.split(r"-|to", self.time_range.value)
                        if len(t) < 2:
                            await modal_interaction.followup.send(
                                "⚠️ Invalid time range. Use something like `5:00 pm - 6:00 pm`.",
                                ephemeral=True,
                            )
                            return

                        start = self.parse_time(t[0])
                        end = self.parse_time(t[1])

                        if not start or not end:
                            await modal_interaction.followup.send(
                                "⚠️ Invalid time format. Use `HH:MM` or `H:MM AM/PM` with `-` or `to`.",
                                ephemeral=True,
                            )
                            return

                        start_dt = datetime.combine(date_obj, start.time())
                        end_dt = datetime.combine(date_obj, end.time())
                        if end_dt < start_dt:
                            end_dt += timedelta(days=1)

                        duration = int((end_dt - start_dt).total_seconds() // 60)
                        points = calculate_points(job_type, duration, len(clients_list))
                        desc = self.description.value.strip()

                        embed = build_report_embed(
                            job_type,
                            desc,
                            date_obj,
                            medic_list,
                            duration,
                            len(clients_list),
                            points,
                        )

                        msg = await modal_interaction.channel.send(embed=embed)

                        link = f"https://discord.com/channels/{modal_interaction.guild.id}/{modal_interaction.channel.id}/{msg.id}"
                        hyperlink = f'=HYPERLINK("{link}", "View Report")'

                        report_row = [
                            datetime.now().strftime("%m/%d/%Y %H:%M"),
                            ", ".join(medic_list),
                            job_type,
                            f"{duration} min",
                            points,
                            len(clients_list),
                            ", ".join(clients_list),
                            desc,
                            date_obj.strftime("%m/%d/%Y"),
                            hyperlink,
                            f"'{modal_interaction.user.id}",
                            str(modal_interaction.user),
                            f"'{msg.id}",
                            f"'{modal_interaction.channel.id}",
                        ]

                        if len(report_row) != len(REPORT_HEADERS):
                            raise ValueError(
                                f"Report row has {len(report_row)} columns, expected {len(REPORT_HEADERS)}"
                            )

                        SHEET.append_row(
                            report_row,
                            value_input_option="USER_ENTERED",
                            table_range="A1:N1",
                        )
                        SHEET.resize(cols=len(REPORT_HEADERS))

                        # New row exists, so cached records are stale
                        invalidate_raw_cache()

                        # Rebuild derived sheets, but throttle to prevent quota spikes
                        await rebuild_after_report_change()

                        await modal_interaction.followup.send(
                            "✅ Report logged and sheets queued for update (throttled).",
                            ephemeral=True,
                        )

                    except Exception as e:
                        await modal_interaction.followup.send(f"⚠️ Error: {e}", ephemeral=True)

            await select_interaction.response.send_modal(ReportModal())

    class JobSelectView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=60)
            self.add_item(JobSelect())

    await interaction.response.send_message(
        "Choose your **Job Type** to begin your report:",
        view=JobSelectView(),
        ephemeral=True,
    )


@bot.event
async def on_ready():
    try:
        ensure_reports_sheet_shape()
        ensure_master_sheet_shape()
    except Exception as e:
        print(f"⚠️ Could not lock sheet shapes: {e}")

    synced = await tree.sync(guild=discord.Object(id=GUILD_ID))
    print(f"Synced {len(synced)} commands to guild {GUILD_ID}")
    print(f"Logged in as {bot.user}")


bot.run(DISCORD_TOKEN)
