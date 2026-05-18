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
SHEET = SS.worksheet("Reports")  # first worksheet with raw logs

# Expected header row in the first sheet:
# Timestamp | Medics | Job Name | Duration | Points | Clients | Participant Names | Description | Report Date | Message Link
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
]

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

        records = SHEET.get_all_records(
            expected_headers=REPORT_HEADERS
        )

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

        recs = master_ws.get_all_records()
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

    # Hosted Event — 30 points, must be at least 60 min and 5+ clients
    if "hosted event" in job_name:
        if duration >= 60 and clients >= 5:
            return 30
        return 0

    if "raid" in job_name or "defend" in job_name:
        return 3 + 2 * (duration // 15)
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
    if "arc" in job_name:
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
        leaderboard_sheet = SS.add_worksheet(title=sheet_title, rows="200", cols="10")
        leaderboard_sheet.update([[
            "Rank", "Medic", "Raw Points", "Jobs Logged",
            "Rank Title", "Bonus Multiplier",
            "Adjusted Points", "Total Pay", "Total Ryo"
        ]])

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

    output = [[
        "Rank", "Medic", "Raw Points", "Jobs Logged",
        "Rank Title", "Bonus Multiplier",
        "Adjusted Points", "Total Pay", "Total Ryo"
    ]]

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
            BANK_RYO if i == 1 else ""
        ])

    leaderboard_sheet.clear()
    leaderboard_sheet.update(output)

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
        leaderboard_sheet = SS.add_worksheet(sheet_title, rows=200, cols=10)

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

    output = [[
        "Rank", "Medic", "Raw Points", "Jobs Logged",
        "Rank Title", "Bonus Multiplier",
        "Adjusted Points", "Total Pay", "Total Ryo"
    ]]

    sorted_medics = sorted(adjusted.items(), key=lambda x: x[1], reverse=True)

    for i, (medic, adj_pts) in enumerate(sorted_medics, start=1):
        raw_pts = points_by_medic[medic]
        jobs = jobs_by_medic[medic]
        rank_title = rank_by_medic.get(medic, "Unranked")
        mult = bonus_from_rank(rank_title)
        share = adj_pts / total_adj if total_adj else 0
        pay = round(share * BANK_RYO, 2)

        output.append([
            i, medic, raw_pts, jobs, rank_title, mult,
            round(adj_pts, 2), pay,
            BANK_RYO if i == 1 else ""
        ])

    leaderboard_sheet.clear()
    leaderboard_sheet.update(output)

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
        master = SS.add_worksheet(title="Leaf Master Medical Log", rows="300", cols="20")
        existing_ranks = {}
        master.update([[
            "Medic", "Rank", "Total Jobs", "Total Raw Points",
            "Total Adjusted Points", "Total Hours", "Raid",
            "LMPF", "Healing", "Rev/Spar",
            "Escort", "World Boss", "Arc",
            "Mission", "Hosted Event"
        ]])
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

    output = [[
        "Medic", "Rank", "Total Jobs", "Total Raw Points",
        "Total Adjusted Points", "Total Hours", "Raid",
        "LMPF", "Healing", "Rev/Spar",
        "Escort", "World Boss", "Arc",
        "Mission", "Hosted Event"
    ]]

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
        ])

    if len(output) <= 1:
        print("🚫 Master log rebuild aborted — no parsed data")
        return

    # IMPORTANT: only rebuild if ranks exist; otherwise you'd wipe ranks
    if len(existing_ranks) > 0:
        master.clear()
        master.update(output)
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
        invalidate_master_cache()
        return f"Added **{medic}** with rank **{rank}**."
    else:
        master.update_cell(target_row, 2, rank)
        invalidate_master_cache()
        return f"Updated **{medic}** rank to **{rank}**."


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
                f"• **Hosted Event:** {event_h}"
            ),
            inline=False,
        )
        embed.set_footer(text="Lifetime stats from the Master Medical Log")
        await interaction.followup.send(embed=embed)

    except Exception as e:
        await interaction.followup.send(f"⚠️ Error: {e}")


@tree.command(name="report", description="Submit a medic report")
@discord.app_commands.guilds(discord.Object(id=GUILD_ID))
async def report(interaction: discord.Interaction):

    class JobSelect(discord.ui.Select):
        def __init__(self):
            options = [
                discord.SelectOption(label="Raid / Defend", value="Raid / Defend"),
                discord.SelectOption(label="Duty with LMPF", value="LMPF"),
                discord.SelectOption(label="Healing Lowbies", value="Healing Lowbies"),
                discord.SelectOption(label="Rev Spar", value="Rev Spar"),
                discord.SelectOption(label="Escort", value="Escort"),
                discord.SelectOption(label="World Boss", value="World Boss"),
                discord.SelectOption(label="Arc", value="Arc"),
                discord.SelectOption(label="Mission", value="Daily Mission"),
                discord.SelectOption(label="Run An Event", value="Hosted Event"),
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
                            for m in re.split(r",|\band\b", self.medics.value)
                            if m.strip()
                        ]

                        clients_list = [
                            p.strip()
                            for p in re.split(r",|\band\b", self.clients.value)
                            if p.strip()
                        ]

                        date_obj = (
                            self.parse_date(self.date.value)
                            if self.date.value.strip()
                            else datetime.now().date()
                        )

                        t = re.split(r"-|to", self.time_range.value)
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

                        embed = discord.Embed(
                            title=f"Medic Report — {job_type}",
                            description=desc,
                            color=0x00FFAA,
                        )
                        embed.add_field(name="Date", value=date_obj.strftime("%B %d, %Y"))
                        embed.add_field(name="Medics", value=", ".join(medic_list), inline=False)
                        embed.add_field(name="Duration", value=f"{duration} min")
                        embed.add_field(name="Clients", value=str(len(clients_list)))
                        embed.add_field(name="Points", value=str(points))
                        embed.timestamp = datetime.now()

                        msg = await modal_interaction.channel.send(embed=embed)

                        link = f"https://discord.com/channels/{modal_interaction.guild.id}/{modal_interaction.channel.id}/{msg.id}"
                        hyperlink = f'=HYPERLINK("{link}", "View Report")'

                        SHEET.append_row(
                            [
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
                            ],
                            value_input_option="USER_ENTERED",
                        )

                        # New row exists, so cached records are stale
                        invalidate_raw_cache()

                        # Rebuild derived sheets, but throttle to prevent quota spikes
                        if should_run("master"):
                            update_master_log()
                        if should_run("leaderboard"):
                            update_leaderboard()

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
    synced = await tree.sync(guild=discord.Object(id=GUILD_ID))
    print(f"Synced {len(synced)} commands to guild {GUILD_ID}")
    print(f"Logged in as {bot.user}")


bot.run(DISCORD_TOKEN)